import argparse
import json
import os
import re
import subprocess
import sys

import manifest
from utils import anchor_cwd_to_repo_root

if __name__ == "__main__":
    anchor_cwd_to_repo_root()

ARTIFACTS_REF = "refs/heads/artifacts"
GENERATION_TRAILER = "Ruleset-Publish-Generation:"
_GENERATION = re.compile(r"([1-9][0-9]{0,19})/([1-9][0-9]{0,19})")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class PublishGuardError(Exception):
    pass


def _git(*args, allowed=(0,)):
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False, timeout=45)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise PublishGuardError(f"无法核对发布状态：git {args[0]} 失败（{type(exc).__name__}）") from exc
    if result.returncode not in allowed:
        raise PublishGuardError(f"无法核对发布状态：git {args[0]} 退出码 {result.returncode}")
    return result


def _gh(args):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=45)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise PublishGuardError(f"无法读取制品元数据：{type(exc).__name__}") from exc
    if result.returncode:
        raise PublishGuardError("无法读取制品元数据，请确认制品存在且未过期")
    return result.stdout


def verify_artifacts():
    run_id_text = os.getenv("GITHUB_RUN_ID", "")
    build_sha = os.getenv("GITHUB_SHA", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id_text) or not _SHA.fullmatch(build_sha):
        raise PublishGuardError("缺少可核验的 run ID 或构建 SHA")
    _run_number, attempt = _current_generation()
    run_id = int(run_id_text)
    names = {"RULESETS": "rulesets", "TXT": "merged-rules", "MRS": "merged-rules-mrs"}
    ids = {key: os.getenv(f"PUBLISH_{key}_ID", "") for key in names}
    if any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in ids.values()):
        raise PublishGuardError("缺少成功构建的制品 ID，请重新运行完整流水线")
    if len(set(ids.values())) != len(ids):
        raise PublishGuardError("三个制品 ID 必须不同")
    for key, name in names.items():
        oid = ids[key]
        raw = _gh(["api", f"repos/{{owner}}/{{repo}}/actions/artifacts/{oid}"])
        try:
            artifact = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublishGuardError("无法解析制品元数据，请重新运行完整流水线") from exc
        run = artifact.get("workflow_run") if isinstance(artifact, dict) else None
        if (
            not isinstance(run, dict)
            or run.get("id") != run_id
            or run.get("head_sha") != build_sha
            or artifact.get("id") != int(oid)
            or artifact.get("name") != f"{name}-{attempt}"
            or artifact.get("expired") is not False
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest")))
        ):
            raise PublishGuardError("制品元数据与所选成功构建不符或已过期，请重新运行完整流水线")


def _read_refs():
    refs = {}
    default_ref = None
    for line in _git("ls-remote", "--symref", "origin", "HEAD", ARTIFACTS_REF).stdout.splitlines():
        value, sep, ref = line.partition("\t")
        if not sep:
            raise PublishGuardError("无法解析远端引用")
        if ref == "HEAD" and value.startswith("ref: refs/heads/") and default_ref is None:
            default_ref = value.removeprefix("ref: ")
        elif ref in {"HEAD", ARTIFACTS_REF} and _SHA.fullmatch(value) and ref not in refs:
            refs[ref] = value
        else:
            raise PublishGuardError("无法解析远端引用")
    if default_ref is None or "HEAD" not in refs:
        raise PublishGuardError("无法确认远端默认分支")
    return default_ref, refs["HEAD"], refs.get(ARTIFACTS_REF, "")


def _fetch_commit(oid):
    if _git("cat-file", "-e", f"{oid}^{{commit}}", allowed=(0, 1, 128)).returncode:
        _git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", oid)
        _git("cat-file", "-e", f"{oid}^{{commit}}")


def _current_generation():
    try:
        built = manifest.expected_publish_generation()
    except manifest.ManifestError as exc:
        raise PublishGuardError(str(exc)) from exc
    if not built or not os.getenv("PUBLISH_BUILD_GENERATION"):
        raise PublishGuardError("发布缺少成功构建的 generation")
    current = _GENERATION.fullmatch(f"{os.getenv('GITHUB_RUN_NUMBER', '')}/{built.rsplit('-', 1)[-1]}")
    if current is None:
        raise PublishGuardError("发布代次格式无效")
    return int(current[1]), int(current[2])


def _published_generation(oid):
    _fetch_commit(oid)
    message = _git("show", "-s", "--format=%B", oid).stdout
    entries = [line.removeprefix(GENERATION_TRAILER).strip() for line in message.splitlines()
               if line.startswith(GENERATION_TRAILER)]
    if not entries:
        return None
    match = _GENERATION.fullmatch(entries[0])
    if match is None or len(entries) != 1:
        raise PublishGuardError("artifacts 分支发布代次无法验证")
    return int(match[1]), int(match[2])


def ensure_current(*, required=False):
    if os.getenv("PUBLISH_GUARD") != "true" and not required:
        return None
    verify_artifacts()
    generation = _current_generation()
    build_sha = os.getenv("GITHUB_SHA", "")
    if not _SHA.fullmatch(build_sha):
        raise PublishGuardError("无法确认构建提交")
    default_ref, current_sha, artifacts_oid = _read_refs()
    if default_ref != os.getenv("GITHUB_REF"):
        raise PublishGuardError("构建引用不再是远端默认分支")
    expected_main = os.getenv("PUBLISH_EXPECTED_MAIN_OID")
    if expected_main is not None and current_sha != expected_main:
        raise PublishGuardError("默认分支已偏离本轮拉取的提交，保留本地发布状态")
    _fetch_commit(current_sha)
    if _git(
        "diff", "--quiet", build_sha, current_sha, "--", ".",
        ":(exclude,top)README.md", ":(exclude,top)state/release.sha256", allowed=(0, 1),
    ).returncode:
        raise PublishGuardError("默认分支的构建输入已变化，请从当前输入重新构建")
    if artifacts_oid:
        published = _published_generation(artifacts_oid)
        if published is not None and published > generation:
            raise PublishGuardError("artifacts 分支已有已发布代次更新的产物，拒绝发布旧构建")
    expected = os.getenv("PUBLISH_EXPECTED_ARTIFACTS_OID")
    if expected is not None and artifacts_oid != expected:
        raise PublishGuardError("artifacts 分支已偏离本轮核验的发布基线，拒绝覆盖未知内容")
    return artifacts_oid


def record_unchanged():
    oid = os.getenv("PUBLISH_EXPECTED_ARTIFACTS_OID", "")
    if not _SHA.fullmatch(oid):
        raise PublishGuardError("缺少无变化发布已对账的 artifacts 提交 ID")
    generation = _current_generation()
    if _published_generation(oid) == generation:
        ensure_current(required=True)
        return

    tree = _git("rev-parse", f"{oid}^{{tree}}").stdout.strip()
    stamp = _git(
        "-c", "user.name=github-actions[bot]",
        "-c", "user.email=github-actions[bot]@users.noreply.github.com",
        "commit-tree", tree, "-m", "chore: 确认发布代次",
        "-m", f"{GENERATION_TRAILER} {generation[0]}/{generation[1]}",
    ).stdout.strip()
    ensure_current(required=True)
    _git("push", f"--force-with-lease={ARTIFACTS_REF}:{oid}", "origin", f"{stamp}:{ARTIFACTS_REF}")


def main():
    parser = argparse.ArgumentParser(description="公开写入前复核实时构建输入与发布代次")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--branch-oid", action="store_true", help="输出用于 Git lease 的远端对象 ID")
    commands.add_argument("--record-unchanged", action="store_true", help="为已确认无变化的发布记录代次")
    args = parser.parse_args()
    try:
        if args.record_unchanged:
            record_unchanged()
            return 0
        oid = ensure_current(required=True)
    except PublishGuardError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if args.branch_oid:
        print(oid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
