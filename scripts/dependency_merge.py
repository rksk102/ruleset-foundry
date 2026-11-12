#!/usr/bin/env python3
"""Dependabot PR 直接合并（不使用 Dependabot 原生 auto-merge）。

判定与执行边界：

- 只读查询使用 ``gh pr view`` / ``gh pr list``，合并使用
  ``gh pr merge --merge --match-head-commit <sha>``，绝不使用 ``--auto`` / ``--admin``；
- 所有 gh 调用都经过可替换 runner，测试注入假 runner，不访问网络；
- ``statusCheckRollup`` 用于观察状态；必需 CI 另经 REST 核验 Actions App、精确 head 与同一 CI run，
  ``--match-head-commit`` 负责兜底读取与合并之间的 head 漂移；
- 已 MERGED 视为幂等成功，不再合并/评论；合并后回读确认 MERGED，评论失败只告警。

退出码：0 = merge/wait/block；1 = 配置、API 或合并错误（reconcile 中单个 PR 出错
会继续处理其余 PR，最后仍以 1 退出）。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from logger import error, gh_error, info, success, warning

GH_TIMEOUT = 120
DEFAULT_CONFIG_PATH = ".github/dependency-merge.yml"
LIST_LIMIT = 100
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
BRANCH_RE = re.compile(r"^dependabot/[^/]+(/[^/]+)+$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMENT_MARKER = "<!-- dependency-merge:head-sha={sha} -->"
MERGE_KIND = "merge"
WAIT_KIND = "wait"
BLOCK_KIND = "block"
ERROR_KIND = "error"
PASS_KIND = "pass"
PR_VIEW_FIELDS = (
    "number",
    "state",
    "isDraft",
    "baseRefName",
    "author",
    "headRepository",
    "headRepositoryOwner",
    "headRefName",
    "headRefOid",
    "autoMergeRequest",
    "mergeable",
    "reviewDecision",
    "statusCheckRollup",
    "url",
)
# AI 审查只提供参考意见，不参与判定或摘要；显式配置时仍按配置处理。
IGNORED_AI_CHECKS = frozenset(
    {
        "coderabbit",
        "coderabbitai",
        "coderabbit.ai",
        "greptile",
        "greptile-apps",
        "greptile review",
        "sourcery-ai",
        "sourcery",
        "gemini-code-assist",
        "qodo-merge-pro",
        "copilot-pull-request-reviewer",
    }
)
# 同名检查出现多条时取最严重状态，避免旧结果掩盖失败。
CHECK_SEVERITY = {"success": 0, "neutral": 1, "skipped": 2, "pending": 3, "failure": 4}
GITHUB_ACTIONS_APP_ID = 15368
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
DECISION_LABELS = {MERGE_KIND: "合并", WAIT_KIND: "等待", BLOCK_KIND: "阻塞", ERROR_KIND: "错误"}


class GhError(RuntimeError):
    """gh CLI 调用失败或输出不可解析。"""


class MergeConfigError(ValueError):
    """dependency-merge 配置缺失或格式错误。"""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = key_node.value
            if key in seen:
                raise MergeConfigError(f"配置包含重复键 {key!r}（第 {key_node.start_mark.line + 1} 行）")
            seen.add(key)
        return super().construct_mapping(node, deep)


@dataclass(frozen=True)
class OptionalCheck:
    name: str
    allow_conclusions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MergeConfig:
    repository: str
    base_branch: str
    allowed_authors: frozenset[str]
    allowed_groups: tuple[str, ...]
    required_checks: tuple[str, ...]
    optional_checks: tuple[OptionalCheck, ...]

    @property
    def configured_check_names(self):
        names = {name.casefold() for name in self.required_checks}
        names.update(check.name.casefold() for check in self.optional_checks)
        return names


@dataclass
class Decision:
    kind: str
    reason: str
    pr_number: int | None = None
    head_sha: str | None = None
    checks: dict[str, str] = field(default_factory=dict)
    already_merged: bool = False
    merged: bool = False


def _config_str(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MergeConfigError(f"配置项 {key} 必须是非空字符串")
    return value.strip()


def _config_str_list(data, key):
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise MergeConfigError(f"配置项 {key} 必须是非空字符串列表")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MergeConfigError(f"配置项 {key} 只能包含非空字符串")
        items.append(item.strip())
    if len(set(items)) != len(items):
        raise MergeConfigError(f"配置项 {key} 存在重复值")
    return tuple(items)


def _optional_checks(raw):
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MergeConfigError("配置项 optional_checks 必须是列表")
    checks = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            checks.append(OptionalCheck(item.strip()))
            continue
        if not isinstance(item, dict):
            raise MergeConfigError("optional_checks 的每一项必须是映射或字符串")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MergeConfigError("optional_checks 每一项必须有非空 name")
        conclusions = item.get("allow_conclusions")
        if conclusions is None:
            conclusions = []
        if not isinstance(conclusions, list) or not all(
            isinstance(value, str) and value.strip() for value in conclusions
        ):
            raise MergeConfigError("optional_checks.allow_conclusions 必须是字符串列表")
        checks.append(
            OptionalCheck(
                name.strip(),
                frozenset(value.strip().casefold() for value in conclusions),
            )
        )
    return tuple(checks)


def parse_config(data):
    """校验并规范化配置映射；未知键忽略，缺失/类型错误全部拒绝。"""
    if not isinstance(data, dict):
        raise MergeConfigError("配置文件顶层必须是映射")
    repository = _config_str(data, "repository")
    if not REPOSITORY_RE.fullmatch(repository):
        raise MergeConfigError(f"repository 必须是 owner/repo 形式: {repository}")
    return MergeConfig(
        repository=repository,
        base_branch=_config_str(data, "base_branch"),
        allowed_authors=frozenset(_config_str_list(data, "allowed_authors")),
        allowed_groups=_config_str_list(data, "allowed_groups"),
        required_checks=_config_str_list(data, "required_checks"),
        optional_checks=_optional_checks(data.get("optional_checks")),
    )


def load_config(path=DEFAULT_CONFIG_PATH):
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MergeConfigError(f"无法读取配置 {config_path}: {exc}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise MergeConfigError(f"配置 {config_path} 不是合法 YAML: {exc}") from exc
    return parse_config(data)


def subprocess_runner(cmd_list):
    """生产 runner：调用 gh CLI。GH_TOKEN 只从环境读取，不写入日志。"""
    if not os.environ.get("GH_TOKEN", "").strip():
        raise GhError("GH_TOKEN 未设置，拒绝在没有令牌的情况下调用 gh")
    try:
        result = subprocess.run(
            ["gh", *cmd_list],
            capture_output=True,
            text=True,
            check=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise GhError(f"gh CLI 失败: {' '.join(cmd_list)}: {detail}") from exc
    except UnicodeError as exc:
        raise GhError(f"gh CLI 输出解码失败: {' '.join(cmd_list)}: {exc}") from exc
    except OSError as exc:
        raise GhError(f"无法执行 gh CLI: {exc}") from exc
    return result.stdout.strip()


class Gh:
    """把判定需要的 gh 调用集中到可替换 runner 之后。"""

    def __init__(self, repository, runner=None):
        self.repository = repository
        self.runner = runner or subprocess_runner

    def _run(self, args):
        return self.runner(list(args))

    def _json(self, args):
        raw = self._run(args)
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise GhError(f"gh 输出不是合法 JSON: {raw[:200]}") from exc

    def view_pr(self, number):
        return self._json(
            ["pr", "view", str(number), "--repo", self.repository, "--json", ",".join(PR_VIEW_FIELDS)]
        )

    def list_open_prs(self, head=None):
        cmd = [
            "pr",
            "list",
            "--repo",
            self.repository,
            "--state",
            "open",
            "--json",
            ",".join(PR_VIEW_FIELDS),
            "--limit",
            str(LIST_LIMIT),
        ]
        if head:
            cmd.extend(["--head", head])
        data = self._json(cmd)
        if not isinstance(data, list):
            raise GhError("gh pr list 输出不是列表")
        return [pr for pr in data if isinstance(pr, dict)]

    def merge_pr(self, number, sha):
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise GhError(f"拒绝合并 PR #{number}：head SHA 非法（{sha!r}）")
        self._run(
            [
                "pr",
                "merge",
                str(number),
                "--repo",
                self.repository,
                "--merge",
                "--match-head-commit",
                sha,
            ]
        )

    def list_comments(self, number):
        data = self._json(["pr", "view", str(number), "--repo", self.repository, "--json", "comments"])
        comments = data.get("comments") if isinstance(data, dict) else None
        return [str(item.get("body") or "") for item in comments or [] if isinstance(item, dict)]

    def trusted_check_runs(self, head_sha):
        data = self._json(
            [
                "api",
                f"repos/{self.repository}/commits/{head_sha}/check-runs",
                "--method",
                "GET",
                "-f",
                "per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        if not isinstance(data, list):
            raise GhError("Checks API 分页输出不是页列表")
        checks = []
        for page in data:
            entries = page.get("check_runs") if isinstance(page, dict) else None
            if not isinstance(entries, list):
                raise GhError("Checks API 分页输出缺少 check_runs 列表")
            checks.extend(entry for entry in entries if isinstance(entry, dict))
        return checks

    def workflow_run(self, run_id):
        data = self._json(["api", f"repos/{self.repository}/actions/runs/{run_id}"])
        return data if isinstance(data, dict) else {}

    def comment_pr(self, number, body):
        self._run(["pr", "comment", str(number), "--repo", self.repository, "--body", body])


def _entry_name(entry):
    if not isinstance(entry, dict):
        return None
    name = entry.get("name") or entry.get("context")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def _entry_state(entry):
    """把 CheckRun / StatusContext 归一为 success|neutral|skipped|pending|failure。"""
    state = entry.get("state")
    if state:
        if state == "SUCCESS":
            return "success"
        if state in ("PENDING", "EXPECTED"):
            return "pending"
        if state in ("FAILURE", "ERROR"):
            return "failure"
        return "pending"
    status = entry.get("status")
    conclusion = entry.get("conclusion")
    if isinstance(status, str):
        status = status.upper()
    if isinstance(conclusion, str):
        conclusion = conclusion.upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion == "SUCCESS":
        return "success"
    if conclusion == "SKIPPED":
        return "skipped"
    if conclusion == "NEUTRAL":
        return "neutral"
    if not conclusion:
        return "pending"
    return "failure"


def rollup_states(rollup, config):
    """收集检查状态；未配置的检查与忽略的 AI 审查检查不参与判定。"""
    configured = config.configured_check_names
    states = {}
    for entry in rollup or []:
        name = _entry_name(entry)
        if name is None:
            continue
        folded = name.casefold()
        if folded in IGNORED_AI_CHECKS and folded not in configured:
            continue
        state = _entry_state(entry)
        previous = states.get(name)
        if previous is None or CHECK_SEVERITY[state] > CHECK_SEVERITY[previous]:
            states[name] = state
    return states


def evaluate_checks(states, config, optional_states=None):
    """返回 (kind, reason)；required 缺失/pending 为 wait，失败为 block。"""
    if optional_states is None:
        optional_states = states
    waiting = []
    failing = []
    for name in config.required_checks:
        state = states.get(name)
        if state is None:
            waiting.append(f"{name} 缺失")
        elif state == "success":
            continue
        elif state == "pending":
            waiting.append(f"{name} pending")
        else:
            failing.append(f"{name} {state}")
    for check in config.optional_checks:
        state = optional_states.get(check.name)
        if state is None:
            continue  # 可选项缺席不阻塞
        if state == "success" or state in check.allow_conclusions:
            continue
        if state == "pending":
            waiting.append(f"{check.name} pending")
        else:
            failing.append(f"{check.name} {state}")
    if failing:
        return BLOCK_KIND, "检查未通过: " + ", ".join(failing)
    if waiting:
        return WAIT_KIND, "等待检查结果: " + ", ".join(waiting)
    return PASS_KIND, ""


def allowed_group(head_branch, allowed_groups):
    """返回 (命中的分组 | None, 原因)。只接受非 major 的明确分组分支。"""
    if not isinstance(head_branch, str) or not BRANCH_RE.fullmatch(head_branch):
        return None, f"分支 {head_branch!r} 不是 dependabot/<ecosystem>/<path>/<group> 形态"
    if "-major" in head_branch:
        return None, f"分支 {head_branch} 属于 major 更新分组"
    leaf = head_branch.rsplit("/", 1)[-1]
    for group in allowed_groups:
        if leaf == group or re.fullmatch(re.escape(group) + r"-[0-9a-f]{4,40}", leaf):
            return group, ""
    return None, f"分支叶段 {leaf} 不属于允许的非 major 分组"


def _same_repository(pr, repository):
    owner, name = repository.split("/", 1)
    owner_data = pr.get("headRepositoryOwner") or {}
    repo_data = pr.get("headRepository") or {}
    head_owner = owner_data.get("login") if isinstance(owner_data, dict) else None
    head_name = repo_data.get("name") if isinstance(repo_data, dict) else None
    if not head_name:
        name_with_owner = repo_data.get("nameWithOwner") if isinstance(repo_data, dict) else ""
        if isinstance(name_with_owner, str) and "/" in name_with_owner:
            head_name = name_with_owner.split("/", 1)[1]
    if not head_owner or not head_name:
        return False, "head 仓库不可识别（可能是已删除的 fork）"
    if head_owner.casefold() != owner.casefold() or head_name.casefold() != name.casefold():
        return False, f"head 仓库 {head_owner}/{head_name} 不是 {repository}（fork PR 不自动合并）"
    return True, ""


def _actions_run_id(entry, repository):
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*",
        entry.get("details_url") or "",
    )
    return int(match.group(1)) if match else None


def _trusted_ci_run(gh, rollup, config, head_sha):
    """选择同一 head 上包含全部成功必需检查的最新可信 CI run。"""
    runs = {}
    reasons = set()
    for entry in rollup:
        name = _entry_name(entry)
        if name not in config.required_checks:
            continue
        if entry.get("head_sha") != head_sha:
            reasons.add(f"{name} 不属于当前 head")
            continue
        app = entry.get("app")
        if (
            entry.get("__typename") == "StatusContext"
            or not isinstance(app, dict)
            or app.get("id") != GITHUB_ACTIONS_APP_ID
        ):
            reasons.add(f"{name} 来源不可信")
            continue
        run_id = _actions_run_id(entry, config.repository)
        if run_id is None:
            reasons.add(f"{name} 缺少 Actions run 绑定")
            continue
        runs.setdefault(run_id, []).append(entry)
    if not runs:
        return None, sorted(reasons)[0] if reasons else "必需检查缺失"
    run_id = max(runs)
    run = gh.workflow_run(run_id)
    if run.get("id") != run_id or run.get("head_sha") != head_sha:
        return None, "CI run 与当前 head 不匹配"
    if run.get("path") != CI_WORKFLOW_PATH or run.get("name") != "CI":
        return None, "必需检查不属于预期 CI 工作流"
    if _entry_state_check(run_id, runs[run_id], config) != PASS_KIND:
        if reasons:
            return None, sorted(reasons)[0]
        if len(runs) > 1 or any(
            _entry_state_check(candidate, entries, config) == PASS_KIND
            for candidate, entries in runs.items()
        ):
            return None, "必需检查不属于同一 CI run"
        return None, "最新可信 CI run 的必需检查未全部通过"
    return run_id, ""


def _entry_state_check(run_id, entries, config):
    states = {}
    for entry in entries:
        name = _entry_name(entry)
        state = _entry_state(entry)
        if name in states and CHECK_SEVERITY[state] < CHECK_SEVERITY[states[name]]:
            continue
        states[name] = state
    return evaluate_checks(states, config, optional_states={})[0]


def evaluate_pr(pr, config, expected_sha=None, rollup=None, workflow_verifier=None):
    """纯判定接受已核实的检查来源；真实 API 读取由 process_pr 注入。"""
    if not isinstance(pr, dict):
        raise GhError("gh 返回的 PR 数据不是对象")
    number = pr.get("number")
    head_sha = pr.get("headRefOid")
    observed_states = rollup_states(pr.get("statusCheckRollup"), config)

    def decide(kind, reason, **kwargs):
        return Decision(
            kind=kind,
            reason=reason,
            pr_number=number,
            head_sha=head_sha,
            checks=dict(observed_states),
            **kwargs,
        )

    state = pr.get("state")
    if state == "MERGED":
        return decide(MERGE_KIND, "PR 已合并，幂等成功（不重复合并/评论）", already_merged=True)
    if state != "OPEN":
        return decide(BLOCK_KIND, f"PR 状态为 {state}，非 OPEN")

    author = (pr.get("author") or {}).get("login")
    if author not in config.allowed_authors:
        return decide(BLOCK_KIND, f"作者 {author} 不在允许列表")

    base = pr.get("baseRefName")
    if base != config.base_branch:
        return decide(BLOCK_KIND, f"base 分支 {base} 不是 {config.base_branch}")

    if pr.get("isDraft"):
        return decide(BLOCK_KIND, "PR 仍为 Draft")

    same_repo, repo_reason = _same_repository(pr, config.repository)
    if not same_repo:
        return decide(BLOCK_KIND, repo_reason)

    if pr.get("autoMergeRequest") is not None:
        return decide(BLOCK_KIND, "已存在原生 auto-merge 请求，拒绝重复自动合并")

    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return decide(BLOCK_KIND, "审查状态为 CHANGES_REQUESTED")

    branch = pr.get("headRefName")
    group, branch_reason = allowed_group(branch, config.allowed_groups)
    if group is None:
        return decide(BLOCK_KIND, branch_reason)

    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return decide(BLOCK_KIND, f"无法确认有效 head SHA（{head_sha!r}），拒绝无绑定合并")
    if expected_sha is not None and head_sha != expected_sha:
        return decide(BLOCK_KIND, f"head SHA 已变化: 期望 {expected_sha}，当前 {head_sha}")

    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        return decide(BLOCK_KIND, "mergeable=CONFLICTING，存在冲突")
    if mergeable != "MERGEABLE":
        if mergeable == "UNKNOWN":
            return decide(WAIT_KIND, "mergeable=UNKNOWN，等待 GitHub 计算合并状态")
        return decide(WAIT_KIND, f"mergeable={mergeable}，暂不能直接合并")

    verified_states = rollup_states(rollup, config) if rollup is not None else {}
    check_kind, check_reason = evaluate_checks(verified_states, config, observed_states)
    if check_kind == PASS_KIND and workflow_verifier is not None:
        source_reason = workflow_verifier()
        if source_reason:
            return decide(WAIT_KIND, source_reason)
    if check_kind != PASS_KIND:
        return decide(check_kind, check_reason)
    return decide(MERGE_KIND, f"满足合并条件（分支 {branch}，分组 {group}，head {head_sha}）")


def merge_and_confirm(gh, decision):
    gh.merge_pr(decision.pr_number, decision.head_sha)
    fresh = gh.view_pr(decision.pr_number)
    if fresh.get("state") != "MERGED":
        raise GhError(f"PR #{decision.pr_number} 合并后状态为 {fresh.get('state')}，未能确认 MERGED")
    decision.merged = True


def comment_merged(gh, decision):
    """合并后的通知评论；按 head SHA 标记去重，失败只警告。"""
    marker = COMMENT_MARKER.format(sha=decision.head_sha)
    try:
        if any(marker in body for body in gh.list_comments(decision.pr_number)):
            info(f"PR #{decision.pr_number} 已存在 head {decision.head_sha} 的合并评论，跳过")
            return
        body = f"{marker}\n已由 dependency-merge 按 head `{decision.head_sha}` 直接合并。"
        gh.comment_pr(decision.pr_number, body)
    except GhError as exc:
        warning(f"PR #{decision.pr_number} 已合并，但写入评论失败（忽略）: {exc}")


def process_pr(gh, config, pr, expected_sha=None, dry_run=False):
    if not isinstance(pr, dict):
        raise GhError("gh 返回的 PR 数据不是对象")
    if pr.get("state") == "MERGED":
        return evaluate_pr(pr, config, expected_sha=expected_sha)
    head_sha = pr.get("headRefOid")
    rollup = None
    if isinstance(head_sha, str) and SHA_RE.fullmatch(head_sha):
        rollup = gh.trusted_check_runs(head_sha)
        run_id, source_reason = _trusted_ci_run(gh, rollup, config, head_sha)
        if run_id is not None:
            rollup = [entry for entry in rollup if _actions_run_id(entry, config.repository) == run_id]
    else:
        source_reason = "head SHA 非法，无法读取可信检查"
    decision = evaluate_pr(
        pr, config, expected_sha=expected_sha, rollup=rollup, workflow_verifier=lambda: source_reason
    )
    if decision.kind != MERGE_KIND or decision.already_merged:
        return decision
    if dry_run:
        decision.reason = f"{decision.reason}；dry-run 不执行合并/评论"
        return decision
    try:
        merge_and_confirm(gh, decision)
    except GhError as exc:
        raise GhError(f"PR #{decision.pr_number} head={decision.head_sha}: {exc}") from exc
    comment_merged(gh, decision)
    return decision


def run_single(gh, config, pr_number=None, head_branch=None, head_sha=None, dry_run=False):
    if pr_number is not None:
        pr = gh.view_pr(pr_number)
    elif head_branch:
        prs = gh.list_open_prs(head=head_branch)
        if len(prs) != 1:
            raise GhError(f"按 head 分支 {head_branch} 找到 {len(prs)} 个开放 PR，无法唯一确定")
        pr = prs[0]
    else:
        raise GhError("必须提供 PR 编号或 head 分支")
    return process_pr(gh, config, pr, expected_sha=head_sha, dry_run=dry_run)


def run_reconcile(gh, config, dry_run=False):
    """扫描所有开放 PR；单个 PR 的 gh 错误不阻断其余 PR。"""
    decisions = []
    for pr in gh.list_open_prs():
        try:
            decisions.append(process_pr(gh, config, pr, expected_sha=None, dry_run=dry_run))
        except Exception as exc:
            decisions.append(
                Decision(
                    kind=ERROR_KIND,
                    reason=str(exc),
                    pr_number=pr.get("number"),
                    head_sha=pr.get("headRefOid"),
                )
            )
    return decisions


def report_decision(decision):
    label = DECISION_LABELS.get(decision.kind, decision.kind)
    number = f"#{decision.pr_number}" if decision.pr_number is not None else "#?"
    line = f"PR {number} head={decision.head_sha or '未知'} 决策={label} 原因={decision.reason}"
    if decision.kind == ERROR_KIND:
        error(line)
    else:
        info(line)
    for name in sorted(decision.checks):
        info(f"  检查 {name}: {decision.checks[name]}")
    if decision.merged:
        success(f"PR {number} 合并后已确认 MERGED（head={decision.head_sha}）")


def _md_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def append_step_summary(decisions):
    """把本轮判定追加到 GITHUB_STEP_SUMMARY；不写任何令牌。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    lines = [
        "## Dependabot 自动合并评估",
        "",
        "| PR | Head SHA | 决策 | 原因 |",
        "| --- | --- | --- | --- |",
    ]
    for decision in decisions:
        number = f"#{decision.pr_number}" if decision.pr_number is not None else "-"
        sha = f"`{decision.head_sha}`" if decision.head_sha else "-"
        label = DECISION_LABELS.get(decision.kind, decision.kind)
        lines.append(f"| {number} | {sha} | {_md_cell(label)} | {_md_cell(decision.reason)} |")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        warning(f"写入 GITHUB_STEP_SUMMARY 失败（忽略）: {exc}")


def _parse_bool(value):
    lowered = value.strip().casefold()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"需要 true 或 false，得到 {value!r}")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="按配置判定并直接合并 Dependabot PR")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="合并配置路径")
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--pr-number", type=int, help="只评估指定 PR 编号")
    targets.add_argument("--head-branch", help="按 head 分支唯一查找开放 PR")
    targets.add_argument("--reconcile", action="store_true", help="扫描所有开放 PR 并逐个评估")
    parser.add_argument("--head-sha", help="期望的 head SHA；workflow_run 调用方必须提供")
    parser.add_argument(
        "--dry-run",
        type=_parse_bool,
        default=False,
        metavar="true|false",
        help="只判定，不合并、不评论",
    )
    args = parser.parse_args(argv)
    args.head_sha = args.head_sha or None
    if args.reconcile and args.head_sha:
        parser.error("--reconcile 不能与 --head-sha 同时使用")
    if args.head_sha and not SHA_RE.fullmatch(args.head_sha):
        parser.error("--head-sha 必须是 40-64 位小写十六进制提交 SHA")
    return args


def main(argv=None, runner=None):
    """runner 仅供测试注入；生产从环境构造真实 subprocess runner。"""
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except MergeConfigError as exc:
        error(str(exc))
        gh_error(f"Dependabot 合并配置错误: {exc}")
        return 1

    gh = Gh(config.repository, runner=runner)
    try:
        if args.reconcile:
            decisions = run_reconcile(gh, config, dry_run=args.dry_run)
        else:
            decisions = [
                run_single(
                    gh,
                    config,
                    pr_number=args.pr_number,
                    head_branch=args.head_branch,
                    head_sha=args.head_sha,
                    dry_run=args.dry_run,
                )
            ]
    except GhError as exc:
        decisions = [
            Decision(
                kind=ERROR_KIND,
                reason=str(exc),
                pr_number=args.pr_number,
                head_sha=args.head_sha,
            )
        ]

    for decision in decisions:
        report_decision(decision)
    append_step_summary(decisions)
    return 1 if any(decision.kind == ERROR_KIND for decision in decisions) else 0


if __name__ == "__main__":
    from utils import anchor_cwd_to_repo_root

    anchor_cwd_to_repo_root()
    sys.exit(main())
