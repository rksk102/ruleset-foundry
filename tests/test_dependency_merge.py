import copy
import json

import dependency_merge as dm
import pytest
import yaml

REPO = "rksk102/ruleset-foundry"
SHA = "a" * 40
OTHER_SHA = "b" * 40
NESTED_BRANCH = "dependabot/uv/project/python-dependencies-1234abcd"
MAJOR_BRANCH = "dependabot/uv/project/python-dependencies-major-1234abcd"

CONFIG_DATA = {
    "repository": REPO,
    "base_branch": "main",
    "allowed_authors": ["app/dependabot", "dependabot[bot]"],
    "allowed_groups": ["python-dependencies"],
    "required_checks": [
        "lint",
        "test",
        "依赖审查",
        "内核冒烟",
        "工作流静态检查",
        "禁止提交生成产物",
    ],
    "optional_checks": [
        {"name": "Analyze (python)"},
        {"name": "Analyze (actions)"},
        {"name": "CodeQL", "allow_conclusions": ["SKIPPED", "NEUTRAL"]},
    ],
}
CONFIG = dm.parse_config(CONFIG_DATA)


def check(name, conclusion="SUCCESS", status="COMPLETED", app_id=15368, run_id=101, job_id=1001):
    if job_id == 1001 and run_id is not None:
        job_id += (run_id - 101) * 100
    entry = {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": SHA,
        "app": {"id": app_id} if app_id is not None else None,
    }
    if run_id is not None and job_id is not None:
        entry["details_url"] = f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}"
    return entry


def status_context(name, state):
    return {"__typename": "StatusContext", "context": name, "state": state}


def default_checks():
    entries = [check(name) for name in CONFIG_DATA["required_checks"]]
    entries.append(check("Analyze (python)"))
    entries.append(check("Analyze (actions)"))
    entries.append(check("CodeQL", conclusion="SKIPPED"))
    # 审查类机器人失败不应影响合并。
    entries.append(check("CodeRabbit", conclusion="FAILURE"))
    return entries


def make_pr(number=40, **overrides):
    pr = {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "author": {"login": "dependabot[bot]"},
        "headRepository": {"name": "ruleset-foundry"},
        "headRepositoryOwner": {"login": "rksk102"},
        "headRefName": NESTED_BRANCH,
        "headRefOid": SHA,
        "autoMergeRequest": None,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "statusCheckRollup": [
            {
                **check(
                    item["name"],
                    conclusion=item.get("conclusion"),
                    status=item.get("status"),
                    run_id=100 + number,
                    job_id=1000 + number * 100 + index,
                ),
                "head_sha": overrides.get("headRefOid", SHA),
            }
            for index, item in enumerate(default_checks())
        ],
        "url": f"https://github.com/{REPO}/pull/{number}",
    }
    pr.update(overrides)
    return pr


def replace_check(pr, name, entry):
    pr["statusCheckRollup"] = [
        item for item in pr["statusCheckRollup"] if (item.get("name") or item.get("context")) != name
    ]
    if entry is not None:
        pr["statusCheckRollup"].append(entry)


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class FakeGh:
    """gh 命令边界替身；只实现本模块用到的调用，不访问网络。"""

    def __init__(self, prs=(), comments=None):
        self.prs = {int(pr["number"]): copy.deepcopy(pr) for pr in prs}
        self.comments = {int(number): list(bodies) for number, bodies in (comments or {}).items()}
        self.calls = []
        self.failures = []

    def fail_when(self, predicate):
        self.failures.append(predicate)

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        for predicate in self.failures:
            if predicate(cmd):
                raise dm.GhError("注入的 gh 失败")
        if cmd[:2] == ["pr", "view"]:
            return self._view(cmd)
        if cmd[:2] == ["pr", "list"]:
            return self._list(cmd)
        if cmd[:2] == ["pr", "merge"]:
            return self._merge(cmd)
        if cmd[:2] == ["pr", "comment"]:
            return self._comment(cmd)
        if cmd[0] == "api":
            return self._api(cmd)
        raise AssertionError(f"未预期的 gh 调用: {cmd}")

    def _api(self, cmd):
        endpoint = cmd[1]
        if "/commits/" in endpoint and endpoint.endswith("/check-runs"):
            pr = next((pr for pr in self.prs.values() if f"/{pr['headRefOid']}/" in endpoint), None)
            checks = pr["statusCheckRollup"] if pr is not None else []
            return json.dumps({"total_count": len(checks), "check_runs": checks})
        if "/actions/runs/" in endpoint:
            run_id = int(endpoint.rsplit("/", 1)[1])
            pr = next(
                (pr for pr in self.prs.values() if f"actions/runs/{run_id}/" in str(pr["statusCheckRollup"])),
                None,
            )
            return json.dumps({
                "id": run_id,
                "head_sha": pr["headRefOid"] if pr is not None else SHA,
                "path": ".github/workflows/ci.yml",
                "name": "CI",
                "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            })
        raise AssertionError(f"未预期的 API 调用: {cmd}")

    def _view(self, cmd):
        number = int(cmd[2])
        fields = _flag(cmd, "--json").split(",")
        if fields == ["comments"]:
            bodies = self.comments.get(number, [])
            return json.dumps({"comments": [{"body": body} for body in bodies]})
        if number not in self.prs:
            raise dm.GhError(f"PR #{number} 不存在")
        return json.dumps(copy.deepcopy(self.prs[number]))

    def _list(self, cmd):
        head = _flag(cmd, "--head") if "--head" in cmd else None
        prs = [
            copy.deepcopy(pr)
            for pr in self.prs.values()
            if pr["state"] == "OPEN" and (head is None or pr["headRefName"] == head)
        ]
        return json.dumps(prs)

    def _merge(self, cmd):
        number = int(cmd[2])
        sha = _flag(cmd, "--match-head-commit")
        pr = self.prs[number]
        if pr["headRefOid"] != sha:
            raise dm.GhError(f"match-head-commit 不匹配: {sha} != {pr['headRefOid']}")
        pr["state"] = "MERGED"
        pr["mergeCommit"] = {"oid": "c" * 40}
        return ""

    def _comment(self, cmd):
        number = int(cmd[2])
        self.comments.setdefault(number, []).append(_flag(cmd, "--body"))
        return ""


def run_pr(pr, *, config=CONFIG, dry_run=True, expected_sha=None, use_branch=False):
    fake = FakeGh([pr])
    gh = dm.Gh(REPO, runner=fake)
    if use_branch:
        decision = dm.run_single(
            gh,
            config,
            head_branch=pr["headRefName"],
            head_sha=expected_sha,
            dry_run=dry_run,
        )
    else:
        decision = dm.run_single(
            gh,
            config,
            pr_number=pr["number"],
            head_sha=expected_sha,
            dry_run=dry_run,
        )
    return decision, fake


def merge_calls(fake):
    return [call for call in fake.calls if call[:2] == ["pr", "merge"]]


def comment_calls(fake):
    return [call for call in fake.calls if call[:2] == ["pr", "comment"]]


def write_config(work_dir, data=None):
    path = work_dir / "dependency-merge.yml"
    path.write_text(
        yaml.safe_dump(CONFIG_DATA if data is None else data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_pr40_like_eligible_pr_is_dry_run_merge():
    decision, fake = run_pr(make_pr())

    assert decision.kind == dm.MERGE_KIND
    assert decision.merged is False
    assert decision.head_sha == SHA
    assert decision.checks["CodeQL"] == "skipped"
    assert "CodeRabbit" not in decision.checks
    assert merge_calls(fake) == []
    assert comment_calls(fake) == []


def test_real_merge_uses_plain_merge_and_exact_head_sha():
    decision, fake = run_pr(make_pr(), dry_run=False)

    assert decision.kind == dm.MERGE_KIND
    assert decision.merged is True
    assert merge_calls(fake) == [
        ["pr", "merge", "40", "--repo", REPO, "--merge", "--match-head-commit", SHA]
    ]
    assert fake.prs[40]["state"] == "MERGED"


def test_successful_merge_comment_contains_head_marker():
    _decision, fake = run_pr(make_pr(), dry_run=False)

    bodies = fake.comments[40]
    assert len(bodies) == 1
    assert dm.COMMENT_MARKER.format(sha=SHA) in bodies[0]


def test_existing_head_marker_comment_is_deduplicated():
    marker_body = f"{dm.COMMENT_MARKER.format(sha=SHA)}\n旧评论"
    fake = FakeGh([make_pr()], comments={40: [marker_body]})
    gh = dm.Gh(REPO, runner=fake)

    decision = dm.run_single(gh, CONFIG, pr_number=40, dry_run=False)

    assert decision.merged is True
    assert fake.comments[40] == [marker_body]
    assert comment_calls(fake) == []


def test_comment_failure_only_warns_after_confirmed_merge():
    fake = FakeGh([make_pr()])
    fake.fail_when(lambda cmd: cmd[:2] == ["pr", "comment"])
    gh = dm.Gh(REPO, runner=fake)

    decision = dm.run_single(gh, CONFIG, pr_number=40, dry_run=False)

    assert decision.kind == dm.MERGE_KIND
    assert decision.merged is True
    assert fake.prs[40]["state"] == "MERGED"


def test_foreign_check_run_cannot_satisfy_required_check():
    pr = make_pr()
    replace_check(pr, "test", check("test", app_id=999))

    decision, fake = run_pr(pr)

    assert decision.kind == dm.WAIT_KIND
    assert "test 来源不可信" in decision.reason
    assert merge_calls(fake) == []


def test_checks_from_other_head_cannot_satisfy_current_pr():
    pr = make_pr()
    replace_check(pr, "test", {**check("test"), "head_sha": OTHER_SHA})

    decision, _fake = run_pr(pr)

    assert decision.kind == dm.WAIT_KIND
    assert "test 不属于当前 head" in decision.reason


def test_required_checks_must_belong_to_the_same_ci_run():
    pr = make_pr()
    replace_check(pr, "test", check("test", run_id=102, job_id=2002))

    decision, _fake = run_pr(pr)

    assert decision.kind == dm.WAIT_KIND
    assert "必需检查不属于同一 CI run" in decision.reason


def test_unexpected_workflow_cannot_satisfy_required_checks():
    pr = make_pr()
    fake = FakeGh([pr])
    gh = dm.Gh(REPO, runner=fake)
    original_api = fake._api
    run_id = 140

    def wrong_workflow(cmd):
        if "/actions/runs/" in cmd[1]:
            return json.dumps({"id": run_id, "head_sha": SHA, "path": ".github/workflows/other.yml", "name": "Other"})
        return original_api(cmd)

    fake._api = wrong_workflow
    decision = dm.run_single(gh, CONFIG, pr_number=pr["number"], dry_run=True)

    assert decision.kind == dm.WAIT_KIND
    assert "预期 CI 工作流" in decision.reason


def test_status_context_is_not_a_trusted_check_run_source():
    pr = make_pr()
    replace_check(pr, "test", {**status_context("test", "SUCCESS"), "name": "test", "head_sha": SHA})

    decision, _fake = run_pr(pr)

    assert decision.kind == dm.WAIT_KIND
    assert "test 来源不可信" in decision.reason


@pytest.mark.parametrize(
    ("replacement", "expected_kind", "needle"),
    [
        (None, dm.WAIT_KIND, "test 缺失"),
        (check("test", status="IN_PROGRESS", conclusion=None), dm.WAIT_KIND, "test pending"),
        (check("test", conclusion="FAILURE"), dm.BLOCK_KIND, "test failure"),
    ],
    ids=["missing", "pending", "failure"],
)
def test_required_check_boundaries(replacement, expected_kind, needle):
    pr = make_pr()
    replace_check(pr, "test", replacement)

    decision, fake = run_pr(pr)

    assert decision.kind == expected_kind
    assert needle in decision.reason
    assert merge_calls(fake) == []


@pytest.mark.parametrize(
    ("name", "replacement", "expected_kind", "needle"),
    [
        ("Analyze (python)", None, dm.MERGE_KIND, ""),
        (
            "Analyze (actions)",
            check("Analyze (actions)", status="IN_PROGRESS", conclusion=None),
            dm.WAIT_KIND,
            "Analyze (actions) pending",
        ),
        ("Analyze (python)", check("Analyze (python)", conclusion="SKIPPED"), dm.BLOCK_KIND, "skipped"),
        ("CodeQL", check("CodeQL", conclusion="NEUTRAL"), dm.MERGE_KIND, ""),
    ],
    ids=["absent", "pending", "unallowed-skipped", "codeql-neutral"],
)
def test_optional_check_boundaries(name, replacement, expected_kind, needle):
    pr = make_pr()
    replace_check(pr, name, replacement)

    decision, _fake = run_pr(pr, expected_sha=SHA)

    assert decision.kind == expected_kind
    if needle:
        assert needle in decision.reason


def test_all_optional_checks_absent_do_not_block():
    pr = make_pr()
    for name in ("Analyze (python)", "Analyze (actions)", "CodeQL"):
        replace_check(pr, name, None)

    decision, _fake = run_pr(pr)

    assert decision.kind == dm.MERGE_KIND


def test_status_context_cannot_replace_required_ci_check():
    for state, expected in (("SUCCESS", dm.WAIT_KIND), ("PENDING", dm.WAIT_KIND), ("FAILURE", dm.BLOCK_KIND)):
        pr = make_pr()
        replace_check(pr, "test", {**status_context("test", state), "name": "test"})
        decision, _fake = run_pr(pr)
        assert decision.kind == expected


def test_ignored_ai_checks_and_unconfigured_checks_do_not_participate():
    pr = make_pr()
    pr["statusCheckRollup"].extend(
        [
            check("coderabbitai", conclusion="FAILURE"),
            check("sourcery-ai", status="IN_PROGRESS", conclusion=None),
            check("gemini-code-assist", conclusion="FAILURE"),
            check("build", conclusion="FAILURE"),
        ]
    )

    decision, _fake = run_pr(pr)

    assert decision.kind == dm.MERGE_KIND
    assert "build" in decision.checks  # 观察到但不影响判定
    assert "coderabbitai" not in decision.checks


@pytest.mark.parametrize(
    ("override", "expected_kind", "needle"),
    [
        ({"state": "CLOSED"}, dm.BLOCK_KIND, "非 OPEN"),
        ({"author": {"login": "mallory"}}, dm.BLOCK_KIND, "mallory"),
        ({"baseRefName": "develop"}, dm.BLOCK_KIND, "develop"),
        ({"isDraft": True}, dm.BLOCK_KIND, "Draft"),
        ({"headRepositoryOwner": {"login": "someone-else"}}, dm.BLOCK_KIND, "fork"),
        ({"headRepository": {"name": "other-repo"}}, dm.BLOCK_KIND, "fork"),
        ({"headRepository": None}, dm.BLOCK_KIND, "fork"),
        ({"autoMergeRequest": {"enabledBy": {"login": "dependabot[bot]"}}}, dm.BLOCK_KIND, "auto-merge"),
        ({"reviewDecision": "CHANGES_REQUESTED"}, dm.BLOCK_KIND, "CHANGES_REQUESTED"),
        ({"headRefName": MAJOR_BRANCH}, dm.BLOCK_KIND, "major"),
        (
            {"headRefName": "dependabot/github_actions/project/github-actions-1234abcd"},
            dm.BLOCK_KIND,
            "github-actions",
        ),
        ({"headRefName": "dependabot/uv/requests/unknown-group"}, dm.BLOCK_KIND, "unknown-group"),
        ({"headRefName": "dependabot/uv/requests/security"}, dm.BLOCK_KIND, "security"),
        ({"mergeable": "UNKNOWN"}, dm.WAIT_KIND, "UNKNOWN"),
        ({"mergeable": "CONFLICTING"}, dm.BLOCK_KIND, "CONFLICTING"),
        ({"headRefOid": "not-a-sha"}, dm.BLOCK_KIND, "head SHA"),
    ],
    ids=[
        "closed",
        "author",
        "base",
        "draft",
        "fork-owner",
        "fork-repo",
        "deleted-fork",
        "native-auto-merge",
        "changes-requested",
        "major",
        "actions-group",
        "ungrouped",
        "security",
        "unknown-mergeable",
        "conflicting",
        "invalid-head-sha",
    ],
)
def test_metadata_gates(override, expected_kind, needle):
    decision, fake = run_pr(make_pr(**override))

    assert decision.kind == expected_kind
    assert needle in decision.reason
    assert merge_calls(fake) == []


def test_expected_head_mismatch_blocks():
    decision, fake = run_pr(make_pr(), expected_sha=OTHER_SHA)

    assert decision.kind == dm.BLOCK_KIND
    assert OTHER_SHA in decision.reason
    assert SHA in decision.reason
    assert merge_calls(fake) == []


@pytest.mark.parametrize(
    "branch",
    [
        "dependabot/uv/python-dependencies",
        "dependabot/uv/requirements/dev/python-dependencies-1a2b3c4d",
        "dependabot/uv/project/python-dependencies-1234abcd",
    ],
    ids=["two-segments", "nested-suffix", "nested-default"],
)
def test_nested_dependabot_branches_are_allowed(branch):
    decision, _fake = run_pr(make_pr(headRefName=branch))

    assert decision.kind == dm.MERGE_KIND


def test_trailing_major_suffix_is_rejected_even_for_allowed_group():
    branch = "dependabot/uv/requirements/dev/python-dependencies-major"
    decision, _fake = run_pr(make_pr(headRefName=branch))

    assert decision.kind == dm.BLOCK_KIND
    assert "major" in decision.reason


def test_already_merged_pr_is_idempotent_success():
    decision, fake = run_pr(make_pr(state="MERGED"))

    assert decision.kind == dm.MERGE_KIND
    assert decision.already_merged is True
    assert decision.merged is False
    assert merge_calls(fake) == []
    assert comment_calls(fake) == []


def test_head_branch_lookup_requires_unique_open_pr():
    fake = FakeGh([make_pr(40), make_pr(41)])
    gh = dm.Gh(REPO, runner=fake)

    with pytest.raises(dm.GhError, match="无法唯一确定"):
        dm.run_single(gh, CONFIG, head_branch=NESTED_BRANCH, head_sha=SHA, dry_run=True)


def test_head_branch_lookup_finds_single_pr():
    decision, fake = run_pr(make_pr(), use_branch=True, expected_sha=SHA)

    assert decision.kind == dm.MERGE_KIND
    assert merge_calls(fake) == []


def test_reconcile_uses_each_pr_current_head_sha():
    fake = FakeGh([make_pr(40), make_pr(41, headRefOid=OTHER_SHA)])
    gh = dm.Gh(REPO, runner=fake)

    decisions = dm.run_reconcile(gh, CONFIG, dry_run=False)

    assert [decision.kind for decision in decisions] == [dm.MERGE_KIND, dm.MERGE_KIND]
    assert merge_calls(fake) == [
        ["pr", "merge", "40", "--repo", REPO, "--merge", "--match-head-commit", SHA],
        ["pr", "merge", "41", "--repo", REPO, "--merge", "--match-head-commit", OTHER_SHA],
    ]


def test_reconcile_continues_after_one_pr_error():
    fake = FakeGh([make_pr(40), make_pr(41, author={"login": "mallory"}), make_pr(42)])
    fake.fail_when(lambda cmd: cmd[:2] == ["pr", "merge"] and cmd[2] == "42")
    gh = dm.Gh(REPO, runner=fake)

    decisions = dm.run_reconcile(gh, CONFIG, dry_run=False)

    assert [decision.kind for decision in decisions] == [
        dm.MERGE_KIND,
        dm.BLOCK_KIND,
        dm.ERROR_KIND,
    ]
    assert fake.prs[40]["state"] == "MERGED"
    assert fake.prs[41]["state"] == "OPEN"
    assert fake.prs[42]["state"] == "OPEN"


def test_load_config_from_work_dir(work_dir):
    path = write_config(work_dir)

    config = dm.load_config(path)

    assert config.repository == REPO
    assert config.base_branch == "main"
    assert "dependabot[bot]" in config.allowed_authors
    assert config.required_checks[0] == "lint"
    assert config.optional_checks[-1].allow_conclusions == frozenset({"skipped", "neutral"})


def test_config_rejects_missing_required_key(work_dir):
    data = dict(CONFIG_DATA)
    data.pop("required_checks")
    path = write_config(work_dir, data)

    with pytest.raises(dm.MergeConfigError, match="required_checks"):
        dm.load_config(path)


def test_config_rejects_bad_repository(work_dir):
    data = dict(CONFIG_DATA)
    data["repository"] = "not-a-repository"
    path = write_config(work_dir, data)

    with pytest.raises(dm.MergeConfigError, match="owner/repo"):
        dm.load_config(path)


def test_config_rejects_missing_file(work_dir):
    with pytest.raises(dm.MergeConfigError, match="无法读取配置"):
        dm.load_config(work_dir / "missing.yml")


def test_config_rejects_invalid_yaml(work_dir):
    path = work_dir / "broken.yml"
    path.write_text("repository: [\n", encoding="utf-8")

    with pytest.raises(dm.MergeConfigError, match="YAML"):
        dm.load_config(path)


@pytest.mark.parametrize("suffix", ["required_checks:\n  - lint\n", "  name: Analyze (python)\n"])
def test_config_rejects_duplicate_keys(work_dir, suffix):
    path = write_config(work_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + suffix)

    with pytest.raises(dm.MergeConfigError, match="重复键"):
        dm.load_config(path)


def test_subprocess_runner_requires_gh_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(dm.GhError, match="GH_TOKEN"):
        dm.subprocess_runner(["pr", "view", "40"])


def test_main_dry_run_true_does_not_merge(work_dir):
    fake = FakeGh([make_pr()])

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--pr-number", "40", "--dry-run", "true"],
        runner=fake,
    )

    assert code == 0
    assert merge_calls(fake) == []
    assert comment_calls(fake) == []


def test_main_empty_head_sha_uses_current_pr_head(work_dir):
    fake = FakeGh([make_pr()])

    code = dm.main(
        [
            "--config",
            str(write_config(work_dir)),
            "--pr-number",
            "40",
            "--head-sha",
            "",
            "--dry-run",
            "false",
        ],
        runner=fake,
    )

    assert code == 0
    assert merge_calls(fake)


def test_main_wait_is_exit_zero(work_dir):
    pr = make_pr()
    replace_check(pr, "test", None)
    fake = FakeGh([pr])

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--pr-number", "40", "--dry-run", "false"],
        runner=fake,
    )

    assert code == 0
    assert merge_calls(fake) == []


def test_main_block_is_exit_zero(work_dir):
    fake = FakeGh([make_pr(author={"login": "mallory"})])

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--pr-number", "40", "--dry-run", "false"],
        runner=fake,
    )

    assert code == 0
    assert merge_calls(fake) == []


def test_main_merge_error_exits_nonzero(work_dir):
    fake = FakeGh([make_pr()])
    fake.fail_when(lambda cmd: cmd[:2] == ["pr", "merge"])

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--pr-number", "40", "--dry-run", "false"],
        runner=fake,
    )

    assert code == 1
    assert merge_calls(fake)


def test_main_reconcile_error_continues_and_exits_nonzero(work_dir):
    fake = FakeGh([make_pr(40), make_pr(41)])
    fake.fail_when(lambda cmd: cmd[:2] == ["pr", "merge"] and cmd[2] == "41")

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--reconcile", "--dry-run", "false"],
        runner=fake,
    )

    assert code == 1
    assert fake.prs[40]["state"] == "MERGED"


def test_main_rejects_malformed_head_sha(work_dir):
    with pytest.raises(SystemExit):
        dm.main(
            [
                "--config",
                str(write_config(work_dir)),
                "--head-branch",
                NESTED_BRANCH,
                "--head-sha",
                "abc",
            ],
            runner=FakeGh([]),
        )


def test_main_rejects_reconcile_with_pr_number(work_dir):
    with pytest.raises(SystemExit):
        dm.main(
            ["--config", str(write_config(work_dir)), "--reconcile", "--pr-number", "40"],
            runner=FakeGh([]),
        )


def test_main_appends_step_summary_without_secrets(work_dir, monkeypatch):
    summary = work_dir / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GH_TOKEN", "super-secret-token")
    fake = FakeGh([make_pr()])

    code = dm.main(
        ["--config", str(write_config(work_dir)), "--pr-number", "40", "--dry-run", "true"],
        runner=fake,
    )

    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert "Dependabot" in text
    assert "#40" in text
    assert SHA in text
    assert "合并" in text
    assert "super-secret-token" not in text
