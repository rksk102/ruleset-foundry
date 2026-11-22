import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import manifest
import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BRANCH_GATE = (
    "(github.event_name == 'schedule' || "
    "github.ref == format('refs/heads/{0}', github.event.repository.default_branch))"
)


def load_workflow(name):
    return yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_product_state_step_has_github_token():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_artifacts_branch_marker_mismatch_triggers_repush():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    script = step["run"]
    assert step["env"]["ARTIFACTS_HASH_MARKER"] == ".product-sha256"
    assert 'git show "FETCH_HEAD:${ARTIFACTS_HASH_MARKER}"' in script
    assert re.search(
        r'elif \[ "\$ARTIFACTS_HASH" != "\$CURRENT" \]; then\n'
        r'.*\n\s*echo "changed=true" >> "\$GITHUB_OUTPUT"',
        script,
    )

    publish = next(s for s in job["steps"] if s.get("name") == "发布规则产物到 artifacts 分支")
    assert publish["env"]["CURRENT_HASH"] == "${{ steps.product-state.outputs.current_hash }}"
    assert 'printf \'%s\\n\' "$CURRENT_HASH" > "${ARTIFACTS_HASH_MARKER}"' in publish["run"]
    assert '"${ARTIFACTS_HASH_MARKER}"' in publish["run"].split("git add -f", 1)[1]


def test_artifacts_branch_marker_match_reconciles_actual_tree():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    script = step["run"]
    assert step["env"]["TXT_DIR"] == "${{ steps.product-paths.outputs.txt }}"
    assert step["env"]["MRS_DIR"] == "${{ steps.product-paths.outputs.mrs }}"

    marker_equal = script.split('elif [ "$ARTIFACTS_HASH" != "$CURRENT" ]; then', 1)[1].split("fi", 1)[0]
    assert 'echo "changed=true" >> "$GITHUB_OUTPUT"' in marker_equal
    reconcile = marker_equal.split("else", 1)[1]
    assert re.search(
        r'python scripts/artifact_tree\.py --ref FETCH_HEAD \\\n'
        r'\s*--txt-dir "\$TXT_DIR" --mrs-dir "\$MRS_DIR" --marker "\$ARTIFACTS_HASH_MARKER"',
        reconcile,
    )
    assert 'if [ "$TREE_RC" -eq 0 ]; then' in reconcile
    assert 'echo "changed=false" >> "$GITHUB_OUTPUT"' in reconcile.split('if [ "$TREE_RC" -eq 0 ]; then', 1)[1]
    mismatch = reconcile.split('elif [ "$TREE_RC" -eq 10 ]; then', 1)[1]
    assert 'echo "changed=true" >> "$GITHUB_OUTPUT"' in mismatch.split("else", 1)[0]
    assert "exit 1" in mismatch.split("else", 1)[1]


def test_publish_verify_only_requires_baseline():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("name") == "产物一致性预检")
    assert step["run"].strip() == "python scripts/release_handler.py --verify-only --require-baseline"


def test_artifacts_branch_missing_or_untrusted_marker_triggers_repush():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    script = step["run"]
    conditions = (
        'if [ -z "$MARKER_ENTRY" ]; then',
        'elif [[ "$MARKER_ENTRY" != ',
        r'if [[ ! "$ARTIFACTS_HASH" =~ ^[0-9a-f]{64}\|[0-9a-f]{64}\|[0-9]+\|[0-9]+$ ]]; then',
    )
    for condition in conditions:
        start = script.index(condition)
        assert 'echo "changed=true" >> "$GITHUB_OUTPUT"' in script[start:].split("fi", 1)[0]


def test_artifacts_marker_validation_matches_combined_product_hash_format():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    match = re.search(r'if \[\[ ! "\$ARTIFACTS_HASH" =~ (.+) \]\]; then', step["run"])
    assert match
    pattern = match.group(1)
    assert re.fullmatch(pattern, f"{'a' * 64}|{'b' * 64}|42|42")
    assert not re.fullmatch(pattern, "a" * 64)
    assert not re.fullmatch(pattern, f"{'a' * 64}|{'b' * 64}|x|42")


def test_artifacts_branch_marker_read_failures_are_fatal():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    script = step["run"]
    commands = (
        'git fetch --no-tags --depth=1 origin "${ARTIFACTS_BRANCH}"',
        'MARKER_ENTRY=$(git ls-tree FETCH_HEAD -- "${ARTIFACTS_HASH_MARKER}")',
        'ARTIFACTS_HASH=$(git show "FETCH_HEAD:${ARTIFACTS_HASH_MARKER}")',
    )
    for command in commands:
        guarded = f"if ! {command}; then"
        start = script.index(guarded)
        assert "exit 1" in script[start:].split("fi", 1)[0]


def test_notify_requires_release_success():
    workflow = load_workflow("pipeline.yml")
    assert workflow["jobs"]["publish"]["outputs"]["release_outcome"] == "${{ steps.release.outcome }}"
    notify = workflow["jobs"]["notify-on-failure"]["steps"][0]
    assert notify["env"]["R_RELEASE"] == "${{ needs.publish.outputs.release_outcome }}"


def test_write_jobs_gate_on_default_branch():
    workflow = load_workflow("pipeline.yml")
    assert workflow["jobs"]["publish"]["if"] == DEFAULT_BRANCH_GATE
    assert workflow["jobs"]["notify-on-failure"]["if"] == f"always() && {DEFAULT_BRANCH_GATE}"


def test_kernel_write_and_notify_gate_on_default_branch():
    workflow = load_workflow("kernel-bump.yml")
    commit_if = workflow["jobs"]["commit"]["if"]
    assert commit_if.startswith(
        "needs.prepare.outputs.changed == 'true' && needs.validate_arm64.result == 'success' && "
    )
    assert commit_if.endswith(DEFAULT_BRANCH_GATE)
    assert workflow["jobs"]["notify-on-failure"]["if"] == f"always() && {DEFAULT_BRANCH_GATE}"


def test_kernel_bump_commit_prepares_python_environment():
    job = load_workflow("kernel-bump.yml")["jobs"]["commit"]
    steps = job["steps"]
    setup_index = next(
        index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/setup-python-env"
    )
    apply_index = next(
        index for index, step in enumerate(steps) if "kernel_pin.py --apply" in step.get("run", "")
    )
    assert setup_index < apply_index


def test_ci_push_only_on_main():
    workflow = load_workflow("ci.yml")
    trigger = workflow.get("on") or workflow.get(True)
    assert trigger["push"]["branches"] == ["main"]


def test_cache_path_comes_from_config():
    workflow = load_workflow("pipeline.yml")
    steps = workflow["jobs"]["build"]["steps"]
    cache_steps = [s for s in steps if s.get("uses", "").startswith("actions/cache/")]
    assert cache_steps
    assert all(s["with"]["path"] == "${{ steps.kernel-key.outputs.path }}" for s in cache_steps)


def test_ci_kernel_smoke_reuses_pipeline_kernel_cache():
    job = load_workflow("ci.yml")["jobs"]["kernel-smoke"]
    assert job["runs-on"] == "ubuntu-latest"
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "--verify-current-kernel" in runs
    assert "convert-ruleset" in runs
    assert "test -s" in runs

    pipeline_steps = load_workflow("pipeline.yml")["jobs"]["build"]["steps"]
    expected = next(
        step for step in pipeline_steps if step.get("uses", "").startswith("actions/cache/restore@")
    )
    restored = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/cache/restore@")
    ]
    assert len(restored) == 1, "kernel-smoke 应只 restore 内核缓存一次"
    actual = restored[0]
    assert actual["uses"] == expected["uses"]
    assert actual["with"]["path"] == expected["with"]["path"]
    assert actual["with"]["key"] == expected["with"]["key"]
    assert actual["with"]["restore-keys"].splitlines() == expected["with"]["restore-keys"].splitlines()
    assert not [step for step in job["steps"] if step.get("uses", "").startswith("actions/cache/save@")]


def test_ci_kernel_smoke_uses_arch_path_and_versioned_cache_layout():
    ci = load_workflow("ci.yml")
    pipeline = load_workflow("pipeline.yml")
    smoke = next(
        step for step in ci["jobs"]["kernel-smoke"]["steps"] if step.get("name") == "用缓存内核转换最小 ipcidr 输入"
    )
    assert smoke["env"]["KERNEL"] == "${{ steps.kernel-key.outputs.path }}/${{ steps.kernel-key.outputs.arch }}/mihomo"
    key_step = next(step for step in ci["jobs"]["kernel-smoke"]["steps"] if step.get("id") == "kernel-key")
    assert 'print("arch=" + current_kernel_arch())' in key_step["run"]

    for workflow, job_name in ((ci, "kernel-smoke"), (pipeline, "build")):
        cache_steps = [step for step in workflow["jobs"][job_name]["steps"] if "actions/cache/" in step.get("uses", "")]
        for step in cache_steps:
            if "key" in step["with"]:
                assert step["with"]["key"].startswith("mihomo-v2-")
            if "restore-keys" in step["with"]:
                assert "mihomo-${{ runner.os }}-${{ runner.arch }}-${{ steps.kernel-key.outputs.key }}" in step[
                    "with"
                ]["restore-keys"]


def test_pipeline_uses_configured_output_dirs_for_artifacts():
    workflow = load_workflow("pipeline.yml")
    build = workflow["jobs"]["build"]
    publish = workflow["jobs"]["publish"]

    for job in (build, publish):
        path_step = next(step for step in job["steps"] if step.get("id") == "product-paths")
        assert "merged_output_dir" in path_step["run"]
        assert "mrs_output_dir" in path_step["run"]

    build_uploads = {
        step["name"]: step["with"]["path"]
        for step in build["steps"]
        if step.get("name", "").startswith("上传 merged-rules")
    }
    assert build_uploads["上传 merged-rules 产物"] == "${{ steps.product-paths.outputs.txt }}/"
    assert build_uploads["上传 merged-rules-mrs 产物"] == "${{ steps.product-paths.outputs.mrs }}/"

    clean = next(step for step in publish["steps"] if step.get("name") == "清空旧产物")
    assert clean["run"] == 'rm -rf "$TXT_DIR" "$MRS_DIR"'

    downloads = {
        step["name"]: step["with"]["path"]
        for step in publish["steps"]
        if step.get("name", "").startswith("下载 merged-rules")
    }
    assert downloads["下载 merged-rules 产物"] == "${{ steps.product-paths.outputs.txt }}/"
    assert downloads["下载 merged-rules-mrs 产物"] == "${{ steps.product-paths.outputs.mrs }}/"

    artifacts = next(
        step for step in publish["steps"] if step.get("name") == "发布规则产物到 artifacts 分支"
    )
    assert artifacts["env"]["TXT_DIR"] == "${{ steps.product-paths.outputs.txt }}"
    assert artifacts["env"]["MRS_DIR"] == "${{ steps.product-paths.outputs.mrs }}"
    assert 'mkdir -p "$(dirname "$TXT_DIR")" "$(dirname "$MRS_DIR")"' in artifacts["run"]
    assert 'cp -r "${GITHUB_WORKSPACE}/${TXT_DIR}" "$TXT_DIR"' in artifacts["run"]
    assert 'cp -r "${GITHUB_WORKSPACE}/${MRS_DIR}" "$MRS_DIR"' in artifacts["run"]
    assert 'printf \'%s/\\n%s/\\n\' "$TXT_DIR" "$MRS_DIR" > .gitignore' in artifacts["run"]
    assert 'git add -f "$TXT_DIR" "$MRS_DIR" .gitignore' in artifacts["run"]


def test_setup_python_env_pins_exact_patch_version():
    action_path = REPO / ".github" / "actions" / "setup-python-env" / "action.yml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    setup = next(
        step
        for step in action["runs"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", setup["with"]["python-version"])
    assert setup["with"]["python-version"] == "3.14.10"


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def _heredoc_body(script):
    body = script.split("<<'PY'", 1)[1].split("\n", 1)[1]
    return body.split("\nPY", 1)[0]


def _run_python_body(body, cwd=REPO):
    return subprocess.run(
        [sys.executable, "-c", body],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "scripts")},
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _run_ci_gate_step(work_dir, name, env=None):
    job = load_workflow("ci.yml")["jobs"]["no-generated-artifacts"]
    return subprocess.run(
        ["bash", "-c", _step(job, name)["run"]],
        cwd=work_dir,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(work_dir / "github-output.txt"),
            **(env or {}),
        },
    )


def _init_git_repo(path):
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")


def _dependency_policy():
    return yaml.safe_load((REPO / ".github" / "dependency-merge.yml").read_text(encoding="utf-8"))


def test_dependency_merge_group_whitelist_is_subset_of_non_major_dependabot_groups():
    allowed = set(_dependency_policy()["allowed_groups"])
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    non_major = set()
    major = set()
    for entry in config["updates"]:
        for name, group in (entry.get("groups") or {}).items():
            types = set(group.get("update-types") or [])
            assert types, f"分组 {name} 必须显式声明 update-types"
            if "major" in types:
                major.add(name)
            else:
                non_major.add(name)
    assert allowed <= non_major
    assert "github-actions" not in allowed
    assert not allowed & major


def test_dependency_merge_required_checks_cover_all_ci_jobs():
    workflow = load_workflow("ci.yml")
    expected = {job.get("name", job_name) for job_name, job in workflow["jobs"].items()}
    assert set(_dependency_policy()["required_checks"]) == expected


def test_dependency_merge_codeql_allows_documented_skip_states():
    optional = {entry["name"]: entry for entry in _dependency_policy()["optional_checks"]}
    assert set(optional["CodeQL"]["allow_conclusions"]) == {"SKIPPED", "NEUTRAL"}


def test_dependency_merge_app_permissions_match_registration_manifest():
    workflow = load_workflow("dependency-merge.yml")
    token_step = next(step for step in workflow["jobs"]["merge"]["steps"] if step.get("id") == "app-token")
    action, sha = token_step["uses"].split("@", 1)
    assert action == "actions/create-github-app-token"
    assert re.fullmatch(r"[0-9a-f]{40}", sha)

    token_permissions = {
        key.removeprefix("permission-").replace("_", "-"): value
        for key, value in token_step["with"].items()
        if key.startswith("permission-")
    }
    registration = json.loads((REPO / ".github" / "dependency-merge-app.json").read_text(encoding="utf-8"))
    manifest_permissions = registration["default_permissions"]
    assert manifest_permissions["metadata"] == "read"
    normalized_manifest_permissions = {
        key.replace("_", "-"): value for key, value in manifest_permissions.items() if key != "metadata"
    }
    assert token_permissions == normalized_manifest_permissions
    assert set(token_permissions) == {"contents", "pull-requests", "checks", "actions"}
    assert registration["public"] is False
    assert registration["default_events"] == []
    assert registration["redirect_url"] == "https://github.com/rksk102/ruleset-foundry"


def test_dependency_merge_has_single_writer_and_main_only_execution():
    assert not (REPO / ".github" / "workflows" / "dependabot-auto-merge.yml").exists()
    workflow = load_workflow("dependency-merge.yml")
    events = workflow[True]
    assert events["workflow_run"]["workflows"] == ["CI", "CodeQL"]
    assert events["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert "schedule" in events
    assert workflow["concurrency"] == {"group": "dependency-merge", "cancel-in-progress": False}

    checkout = workflow["jobs"]["merge"]["steps"][0]
    assert checkout["with"] == {"ref": "main", "persist-credentials": False}
    run = workflow["jobs"]["merge"]["steps"][-1]["run"]
    assert "--auto" not in run
    assert "--admin" not in run


def _run_dependency_merge_step(
    work_dir,
    pr_number,
    head_branch,
    head_sha="a" * 40,
    event_name="workflow_run",
    reconcile="false",
    dry_run="true",
):
    script = load_workflow("dependency-merge.yml")["jobs"]["merge"]["steps"][-1]["run"]
    script = script.replace("${{ github.event_name }}", event_name)
    bin_dir = work_dir / "bin"
    bin_dir.mkdir()
    log = work_dir / "uv.log"
    stub = bin_dir / "uv"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$UV_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "UV_LOG": str(log),
            "GH_TOKEN": "token",
            "PR_NUMBER": pr_number,
            "HEAD_BRANCH": head_branch,
            "HEAD_SHA": head_sha,
            "DRY_RUN": dry_run,
            "RECONCILE": reconcile,
        },
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize(
    ("pr_number", "head_branch", "expected", "forbidden"),
    [
        ("40", "dependabot/uv/python-dependencies", "--pr-number 40", "--head-branch"),
        ("", "dependabot/uv/python-dependencies", "--head-branch dependabot/uv/python-dependencies", "--pr-number"),
        ("40", "", "--pr-number 40", "--head-branch"),
    ],
)
def test_dependency_merge_step_selects_one_pr_locator(work_dir, pr_number, head_branch, expected, forbidden):
    result, log = _run_dependency_merge_step(work_dir, pr_number, head_branch)
    assert result.returncode == 0, result.stderr
    assert expected in log
    assert forbidden not in log


def test_dependency_merge_step_omits_empty_head_sha(work_dir):
    result, log = _run_dependency_merge_step(work_dir, "40", "", head_sha="", event_name="workflow_dispatch")
    assert result.returncode == 0, result.stderr
    assert "--pr-number 40" in log
    assert "--head-sha" not in log


def test_dependency_merge_step_rejects_missing_pr_locator(work_dir):
    result, log = _run_dependency_merge_step(work_dir, "", "")
    assert result.returncode == 1
    assert log == ""
    assert "未提供 PR 编号或 head 分支" in result.stdout


def test_dependency_merge_step_rejects_workflow_run_empty_head_sha(work_dir):
    result, log = _run_dependency_merge_step(work_dir, "40", "dependabot/uv/python-dependencies", head_sha="")
    assert result.returncode == 1
    assert log == ""
    assert "workflow_run.head_sha 为空" in result.stdout


@pytest.mark.parametrize("dry_run", ["true", "false"])
def test_dependency_merge_step_reconcile_passes_arguments(work_dir, dry_run):
    result, log = _run_dependency_merge_step(
        work_dir,
        "",
        "",
        event_name="schedule",
        head_sha="",
        reconcile="true",
        dry_run=dry_run,
    )
    assert result.returncode == 0, result.stderr
    assert "--reconcile" in log
    assert f"--dry-run {dry_run}" in log
    assert "--head-sha" not in log


def test_dependency_merge_step_rejects_reconcile_head_sha(work_dir):
    result, log = _run_dependency_merge_step(
        work_dir,
        "",
        "",
        event_name="workflow_dispatch",
        reconcile="true",
        dry_run="false",
    )
    assert result.returncode == 1
    assert log == ""
    assert "提供 head_sha 时必须指定 pr_number" in result.stdout


def test_ci_generated_artifact_gate_derives_paths_from_config():
    job = load_workflow("ci.yml")["jobs"]["no-generated-artifacts"]
    names = [step.get("name") for step in job["steps"]]
    assert names.index("安装 Python 环境") < names.index("检查生成产物是否被跟踪")
    step = _step(job, "检查生成产物是否被跟踪")
    script = step["run"]
    for key in ("rulesets_dir", "merged_output_dir", "mrs_output_dir", "log_dir", "kernel_cache_path"):
        assert f'"{key}"' in script
    assert 'git ls-files -- "${GENERATED_DIRS[@]/%//*}"' in script
    assert "'merged-rules/*'" not in script
    assert "'rulesets/*'" not in script
    assert step["env"]["PYTHONPATH"] == "${{ github.workspace }}/scripts"

    config = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    result = _run_python_body(_heredoc_body(script))
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        config["paths"]["rulesets_dir"],
        config["paths"]["merged_output_dir"],
        config["paths"]["mrs_output_dir"],
        config["paths"]["log_dir"],
        config["mihomo"]["kernel_cache_path"],
    ]


def test_ci_ignored_file_gate_rejects_force_added_files(work_dir):
    _init_git_repo(work_dir)
    (work_dir / ".gitignore").write_text("artifact.bin\n", encoding="utf-8")
    (work_dir / "artifact.bin").write_text("generated", encoding="utf-8")
    _git(work_dir, "add", ".gitignore")

    result = _run_ci_gate_step(work_dir, "检查被忽略的跟踪文件")
    assert result.returncode == 0, result.stderr

    _git(work_dir, "add", "-f", "artifact.bin")
    result = _run_ci_gate_step(work_dir, "检查被忽略的跟踪文件")
    assert result.returncode == 1
    assert "artifact.bin" in result.stdout


def test_ci_history_ignored_gate_rejects_added_and_removed_files(work_dir):
    _init_git_repo(work_dir)
    (work_dir / ".gitignore").write_text("artifact.bin\n", encoding="utf-8")
    _git(work_dir, "add", ".gitignore")
    _git(work_dir, "commit", "-qm", "base")
    base = _git(work_dir, "rev-parse", "HEAD")

    (work_dir / "artifact.bin").write_text("small generated file", encoding="utf-8")
    _git(work_dir, "add", "-f", "artifact.bin")
    _git(work_dir, "commit", "-qm", "add ignored")
    _git(work_dir, "rm", "-q", "artifact.bin")
    _git(work_dir, "commit", "-qm", "remove ignored")
    head = _git(work_dir, "rev-parse", "HEAD")

    result = _run_ci_gate_step(
        work_dir, "检查新增提交中的被忽略文件", {"BASE_SHA": base, "HEAD_SHA": head}
    )
    assert result.returncode == 1
    assert "artifact.bin" in result.stdout


def test_ci_large_blob_gate_checks_added_and_removed_versions(work_dir):
    _init_git_repo(work_dir)
    (work_dir / "base.txt").write_text("base\n", encoding="utf-8")
    _git(work_dir, "add", "base.txt")
    _git(work_dir, "commit", "-qm", "base")
    base = _git(work_dir, "rev-parse", "HEAD")

    large = work_dir / "large.bin"
    large.write_bytes(b"x" * 1048577)
    _git(work_dir, "add", "large.bin")
    _git(work_dir, "commit", "-qm", "add large")
    _git(work_dir, "rm", "-q", "large.bin")
    _git(work_dir, "commit", "-qm", "remove large")
    head = _git(work_dir, "rev-parse", "HEAD")

    result = _run_ci_gate_step(
        work_dir, "检查新增提交中的大文件", {"BASE_SHA": base, "HEAD_SHA": head}
    )
    assert result.returncode == 1
    assert "1048577" in result.stdout


def test_ci_large_blob_gate_ignores_existing_and_allows_boundary(work_dir):
    _init_git_repo(work_dir)
    large = work_dir / "large.bin"
    large.write_bytes(b"old" * 400000)
    _git(work_dir, "add", "large.bin")
    _git(work_dir, "commit", "-qm", "existing large")
    base = _git(work_dir, "rev-parse", "HEAD")

    large.write_bytes(b"x" * 1048576)
    _git(work_dir, "add", "large.bin")
    _git(work_dir, "commit", "-qm", "boundary")
    head = _git(work_dir, "rev-parse", "HEAD")

    result = _run_ci_gate_step(
        work_dir, "检查新增提交中的大文件", {"BASE_SHA": base, "HEAD_SHA": head}
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_fetch_range_handles_advanced_pr_base(work_dir):
    source = work_dir / "source"
    source.mkdir()
    _init_git_repo(source)
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-qm", "base")
    original_base = _git(source, "rev-parse", "HEAD")

    origin = work_dir / "origin.git"
    _git(work_dir, "init", "--bare", "-q", str(origin))
    _git(source, "remote", "add", "origin", str(origin))
    _git(source, "push", "-q", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(source, "switch", "-qc", "feature")
    large = source / "large.bin"
    large.write_bytes(b"x" * 1048577)
    _git(source, "add", "large.bin")
    _git(source, "commit", "-qm", "add large")
    _git(source, "rm", "-q", "large.bin")
    (source / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(source, "add", "feature.txt")
    _git(source, "commit", "-qm", "remove large and add feature")
    head = _git(source, "rev-parse", "HEAD")

    _git(source, "switch", "-q", "main")
    for index in range(65):
        (source / "main.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(source, "add", "main.txt")
        _git(source, "commit", "-qm", f"advance base {index}")
    base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "main", "feature:refs/pull/7/head")

    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    result = _run_ci_gate_step(
        checkout,
        "获取变更提交范围",
        {
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "PR_NUMBER": "7",
            "GITHUB_REF": "refs/pull/7/merge",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / "github-output.txt").read_text(encoding="utf-8") == f"base={base}\n"
    assert _git(checkout, "merge-base", base, head) == original_base
    assert _git(checkout, "rev-list", "--count", f"{base}..{head}") == "2"

    result = _run_ci_gate_step(
        checkout, "检查新增提交中的大文件", {"BASE_SHA": base, "HEAD_SHA": head}
    )
    assert result.returncode == 1
    assert "1048577" in result.stdout


@pytest.mark.parametrize("forced", [False, True], ids=["unrelated-pr", "forced-rewrite"])
def test_ci_fetch_range_unrelated_histories(work_dir, forced):
    source = work_dir / "source"
    source.mkdir()
    _init_git_repo(source)
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-qm", "base")
    base = _git(source, "rev-parse", "HEAD")

    _git(source, "checkout", "-q", "--orphan", "unrelated")
    _git(source, "read-tree", "--empty")
    (source / ".gitignore").write_text("artifact.bin\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-qm", "unrelated base")
    (source / "large.bin").write_bytes(b"x" * 1048577)
    (source / "artifact.bin").write_text("generated", encoding="utf-8")
    _git(source, "add", "large.bin")
    _git(source, "add", "-f", "artifact.bin")
    _git(source, "commit", "-qm", "add forbidden files")
    _git(source, "rm", "-q", "large.bin", "artifact.bin")
    (source / "other.txt").write_text("other\n", encoding="utf-8")
    _git(source, "add", "other.txt")
    _git(source, "commit", "-qm", "remove forbidden files")
    head = _git(source, "rev-parse", "HEAD")

    origin = work_dir / "origin.git"
    _git(work_dir, "init", "--bare", "-q", str(origin))
    _git(source, "remote", "add", "origin", str(origin))
    _git(source, "push", "-q", "origin", "main", "unrelated")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    result = _run_ci_gate_step(
        checkout,
        "获取变更提交范围",
        {
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "PR_NUMBER": "",
            "GITHUB_REF": "refs/heads/unrelated",
            "FORCED": str(forced).lower(),
        },
    )
    if not forced:
        assert result.returncode == 1
        assert "两个提交没有共同祖先" in result.stdout
        assert "1024" not in result.stdout
        return

    assert result.returncode == 0, result.stdout + result.stderr
    _git(checkout, "checkout", "-q", "--detach", head)
    assert (checkout / "github-output.txt").read_text(encoding="utf-8") == "base=\n"
    result = _run_ci_gate_step(
        checkout, "检查新增提交中的被忽略文件", {"BASE_SHA": "", "HEAD_SHA": head}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "当前 HEAD" in result.stdout
    result = _run_ci_gate_step(
        checkout, "检查新增提交中的大文件", {"BASE_SHA": "", "HEAD_SHA": head}
    )
    assert result.returncode == 1
    assert "1048577" in result.stdout


def test_pipeline_product_paths_derive_rulesets_from_config():
    workflow = load_workflow("pipeline.yml")
    config = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    expected = [
        f"rulesets={config['paths']['rulesets_dir']}",
        f"txt={config['paths']['merged_output_dir']}",
        f"mrs={config['paths']['mrs_output_dir']}",
    ]
    for job_name in ("build", "publish"):
        step = next(s for s in workflow["jobs"][job_name]["steps"] if s.get("id") == "product-paths")
        assert 'get("paths", "rulesets_dir"' in step["run"]
        result = _run_python_body(_heredoc_body(step["run"]))
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == expected


def test_rulesets_artifact_upload_and_download_use_configured_path():
    workflow = load_workflow("pipeline.yml")
    upload = _step(workflow["jobs"]["build"], "上传 rulesets 清单（供发布校验）")
    download = _step(workflow["jobs"]["publish"], "下载 rulesets 产物（提供产物清单基线）")
    assert upload["with"]["path"] == "${{ steps.product-paths.outputs.rulesets }}/products.manifest"
    assert download["with"]["path"] == "${{ steps.product-paths.outputs.rulesets }}/"
    for step in (upload, download):
        assert "rulesets/" not in step["with"]["path"]


def test_public_artifacts_branch_stages_without_product_manifest():
    workflow = load_workflow("pipeline.yml")
    script = _step(workflow["jobs"]["publish"], "发布规则产物到 artifacts 分支")["run"]
    removal = f'rm -f "${{TXT_DIR}}/{manifest.MANIFEST_NAME}"'
    assert removal in script
    # 只删除 worktree 暂存副本：主工作区源文件必须保留给发布校验与 Release ZIP
    assert f"${{GITHUB_WORKSPACE}}/${{TXT_DIR}}/{manifest.MANIFEST_NAME}" not in script
    assert script.index('cp -r "${GITHUB_WORKSPACE}/${TXT_DIR}" "$TXT_DIR"') < script.index(removal)
    assert script.index(removal) < script.index('git add -f')
    assert script.index('cd "$WT"') < script.index(removal) < script.index('git worktree remove')


@pytest.mark.parametrize("newer_input", [True, False], ids=["different-build-input", "newer-published-run"])
def test_publish_guard_rejects_old_products_before_writing(work_dir, newer_input, monkeypatch):
    origin = work_dir / "origin"
    origin.mkdir()
    _init_git_repo(origin)
    (origin / "sources.urls").write_text("first\n", encoding="utf-8")
    _git(origin, "add", "sources.urls")
    _git(origin, "commit", "-qm", "build input")
    built = _git(origin, "rev-parse", "HEAD")
    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))

    if newer_input:
        (origin / "sources.urls").write_text("second\n", encoding="utf-8")
        _git(origin, "commit", "-qam", "new sources")
    else:
        _git(origin, "switch", "-qc", "artifacts")
        _git(origin, "commit", "--allow-empty", "-qm", "publish\n\nRuleset-Publish-Generation: 14/1")
        _git(origin, "switch", "-q", "main")

    result = _run_publish_guard(checkout, built, "13", monkeypatch)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ("构建输入已变化" if newer_input else "已发布代次更新") in result.stderr


def _publish_guard_env(sha, run_number):
    return {
        "PUBLISH_GUARD": "true",
        "PUBLISH_BUILD_GENERATION": f"github-{run_number}-attempt-1",
        "GITHUB_RUN_ID": run_number,
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_NUMBER": run_number,
        "GITHUB_SHA": sha,
        "GITHUB_REF": "refs/heads/main",
        "PUBLISH_RULESETS_ID": "1",
        "PUBLISH_TXT_ID": "2",
        "PUBLISH_MRS_ID": "3",
    }


def _run_publish_guard(checkout, sha, run_number, monkeypatch):
    import publish_guard

    with monkeypatch.context() as patch:
        patch.setattr(publish_guard, "verify_artifacts", lambda: None)
        for key, value in _publish_guard_env(sha, run_number).items():
            patch.setenv(key, value)
        patch.chdir(checkout)
        try:
            oid = publish_guard.ensure_current(required=True)
        except publish_guard.PublishGuardError as exc:
            return subprocess.CompletedProcess([], 1, "", str(exc))
    return subprocess.CompletedProcess([], 0, oid + "\n", "")


@pytest.fixture
def generation_checkout(work_dir):
    origin = work_dir / "origin"
    origin.mkdir()
    _init_git_repo(origin)
    (origin / "scripts").mkdir()
    for name in ("publish_guard.py", "manifest.py", "utils.py"):
        shutil.copyfile(REPO / "scripts" / name, origin / "scripts" / name)
    _git(origin, "add", "scripts")
    _git(origin, "commit", "-qm", "build input")
    built = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-qc", "artifacts")
    (origin / "product.txt").write_text("product A\n", encoding="utf-8")
    _git(origin, "add", "product.txt")
    _git(origin, "commit", "-qm", "publish\n\nRuleset-Publish-Generation: 12/1")
    reconciled = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-q", "main")
    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    _git(checkout, "fetch", "-q", "--no-tags", "--depth=1", "origin", "artifacts")

    bin_dir = work_dir / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/bash\n'
        'case "$2" in\n'
        '  */artifacts/1) name=rulesets ;;\n'
        '  */artifacts/2) name=merged-rules ;;\n'
        '  */artifacts/3) name=merged-rules-mrs ;;\n'
        '  *) exit 1 ;;\n'
        'esac\n'
        'printf \'{"id":%s,"name":"%s-1","digest":"sha256:%064d",'
        '"expired":false,"workflow_run":{"id":%s,"head_sha":"%s"}}\\n\' '
        '"${2##*/}" "$name" 0 "$GITHUB_RUN_ID" "$GITHUB_SHA"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return origin, checkout, built, reconciled, bin_dir


def _complete_unchanged_round(checkout, built, reconciled, bin_dir):
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = _step(job, "推进已确认发布代次")
    assert step["env"]["PUBLISH_EXPECTED_ARTIFACTS_OID"] == "${{ steps.publish-guard.outputs.oid }}"
    assert "steps.product-state.outputs.changed == 'false'" in step["if"]
    assert "steps.release.outcome == 'success' || inputs.skip_release == true" in step["if"]
    assert job["steps"].index(step) > job["steps"].index(_step(job, "提交 README 与发布状态"))
    return subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            **_publish_guard_env(built, "14"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PUBLISH_EXPECTED_ARTIFACTS_OID": reconciled,
        },
    )


def test_unchanged_success_advances_generation_before_an_old_publish_retry(generation_checkout, monkeypatch):
    origin, checkout, built, reconciled, bin_dir = generation_checkout

    # Run 13 had a successful build of B and failed before publishing. Run 14 is still pending.
    retry = _run_publish_guard(checkout, built, "13", monkeypatch)
    assert retry.returncode == 0, retry.stderr
    assert retry.stdout.strip() == reconciled

    result = _complete_unchanged_round(checkout, built, reconciled, bin_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(origin, "show", "artifacts:product.txt") == "product A"
    assert _git(origin, "rev-parse", "artifacts^{tree}") == _git(origin, "rev-parse", f"{reconciled}^{{tree}}")

    stale = _run_publish_guard(checkout, built, "13", monkeypatch)
    assert stale.returncode == 1
    assert "已发布代次更新" in stale.stderr


def test_generation_stamp_rejects_a_reconciled_snapshot_that_has_moved(generation_checkout):
    origin, checkout, built, reconciled, bin_dir = generation_checkout
    _git(origin, "switch", "-q", "artifacts")
    (origin / "product.txt").write_text("other writer\n", encoding="utf-8")
    _git(origin, "commit", "-qam", "publish by other writer")
    other = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-q", "main")

    result = _complete_unchanged_round(checkout, built, reconciled, bin_dir)
    assert result.returncode == 1
    assert "偏离本轮核验的发布基线" in result.stderr
    assert _git(origin, "rev-parse", "artifacts") == other


def test_empty_artifacts_baseline_still_rejects_a_new_branch(work_dir, monkeypatch):
    origin = work_dir / "origin"
    origin.mkdir()
    _init_git_repo(origin)
    (origin / "config.yaml").write_text("same input\n", encoding="utf-8")
    _git(origin, "add", "config.yaml")
    _git(origin, "commit", "-qm", "build input")
    built = _git(origin, "rev-parse", "HEAD")
    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    _git(origin, "switch", "-qc", "artifacts")
    _git(origin, "commit", "--allow-empty", "-qm", "other writer")
    _git(origin, "switch", "-q", "main")
    _git(checkout, "remote", "remove", "origin")
    _git(checkout, "remote", "add", "origin", str(origin))

    import publish_guard

    with monkeypatch.context() as patch:
        patch.setattr(publish_guard, "verify_artifacts", lambda: None)
        for key, value in _publish_guard_env(built, "13").items():
            patch.setenv(key, value)
        patch.setenv("PUBLISH_EXPECTED_ARTIFACTS_OID", "")
        patch.chdir(checkout)
        with pytest.raises(publish_guard.PublishGuardError, match="偏离本轮核验"):
            publish_guard.ensure_current(required=True)


def test_publish_guard_allows_generated_state_and_returns_leased_ref(work_dir, monkeypatch):
    origin = work_dir / "origin"
    origin.mkdir()
    _init_git_repo(origin)
    (origin / "config.yaml").write_text("config\n", encoding="utf-8")
    _git(origin, "add", "config.yaml")
    _git(origin, "commit", "-qm", "build input")
    built = _git(origin, "rev-parse", "HEAD")
    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    (origin / "README.md").write_text("generated\n", encoding="utf-8")
    (origin / "state").mkdir()
    (origin / "state" / "release.sha256").write_text("published\n", encoding="utf-8")
    _git(origin, "add", "README.md", "state")
    _git(origin, "commit", "-qm", "generated outputs")
    _git(origin, "branch", "artifacts", built)

    result = _run_publish_guard(checkout, built, "14", monkeypatch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == built


@pytest.mark.parametrize(
    "field", [None, "id", "name", "workflow_run", "expired", "digest"], ids=lambda value: value or "valid"
)
def test_download_ids_must_resolve_to_expected_build(monkeypatch, field):
    import publish_guard

    artifacts = {}
    for name, oid in (("rulesets-1", 800), ("merged-rules-1", 801), ("merged-rules-mrs-1", 802)):
        artifacts[oid] = {
            "id": oid, "name": name, "digest": "sha256:" + "a" * 64,
            "expired": False, "workflow_run": {"id": 123, "head_sha": "a" * 40},
        }
    wrong = {"id": 999, "name": "merged-rules-2", "workflow_run": {"id": 124}, "expired": True,
             "digest": "sha256:unknown"}
    if field is not None:
        artifacts[801][field] = wrong[field]
    called = []

    def gh(args):
        oid = int(args[1].rsplit("/", 1)[-1])
        called.append(oid)
        return json.dumps(artifacts[oid])

    monkeypatch.setattr(publish_guard, "_gh", gh)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "1")
    monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-123-attempt-1")
    for key, oid in (("RULESETS", "800"), ("TXT", "801"), ("MRS", "802")):
        monkeypatch.setenv(f"PUBLISH_{key}_ID", oid)

    if field is None:
        assert publish_guard.verify_artifacts() is None
        assert called == [800, 801, 802]
    else:
        with pytest.raises(publish_guard.PublishGuardError, match="成功构建"):
            publish_guard.verify_artifacts()
        assert called == [800, 801]


def test_rebase_uses_main_oid_verified_by_the_original_checkout_guard():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    script = _step(job, "提交 README 与发布状态")["run"]
    assert "git pull --rebase" not in script
    fetch = script.index('git fetch --no-tags origin "${REF_NAME}"')
    pinned = script.index('BASE=$(git rev-parse --verify FETCH_HEAD)', fetch)
    guard = script.index('PUBLISH_EXPECTED_MAIN_OID="$BASE" python scripts/publish_guard.py', pinned)
    rebase = script.index('git rebase --onto "$BASE" "$GITHUB_SHA"', guard)
    assert rebase < script.index('git push origin "HEAD:refs/heads/${REF_NAME}"', rebase)


def test_publish_state_step_replays_only_generated_commit_in_shallow_clone(work_dir):
    origin = work_dir / "origin"
    origin.mkdir()
    _init_git_repo(origin)
    (origin / "README.md").write_text("build readme\n", encoding="utf-8")
    (origin / "scripts").mkdir()
    (origin / "scripts" / "publish_guard.py").write_text(
        'import os\nassert os.environ["PUBLISH_EXPECTED_MAIN_OID"] == os.environ["EXPECTED_BASE"]\n',
        encoding="utf-8",
    )
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "build inputs")
    built = _git(origin, "rev-parse", "HEAD")
    checkout = work_dir / "checkout"
    _git(work_dir, "clone", "--depth=1", f"file://{origin}", str(checkout))
    (origin / "README.md").write_text("newer readme\n", encoding="utf-8")
    _git(origin, "commit", "-qam", "newer generated readme")
    base = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "--detach")
    _git(checkout, "fetch", "--no-tags", "--depth=1", "origin", base)
    assert {
        built, base,
    } <= set((checkout / ".git" / "shallow").read_text(encoding="utf-8").splitlines())
    (checkout / "state").mkdir()
    (checkout / "state" / "release.sha256").write_text("new state\n", encoding="utf-8")

    step = _step(load_workflow("pipeline.yml")["jobs"]["publish"], "提交 README 与发布状态")
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=checkout, capture_output=True, text=True,
        env={**os.environ, "GITHUB_SHA": built, "REF_NAME": "main", "EXPECTED_BASE": base},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(origin, "rev-parse", "main^") == base
    assert _git(origin, "show", "main:README.md") == "newer readme"
    assert _git(origin, "show", "main:state/release.sha256") == "new state"


def test_publish_force_push_uses_current_guard_lease():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    script = _step(job, "发布规则产物到 artifacts 分支")["run"]
    lease = 'LEASE=$(python "$GITHUB_WORKSPACE/scripts/publish_guard.py" --branch-oid)'
    assert lease in script
    assert script.index(lease) < script.index('--force-with-lease="refs/heads/${ARTIFACTS_BRANCH}:$LEASE"')
    assert "git push --force origin" not in script


def test_publish_downloads_all_three_outputs_from_selected_build():
    jobs = load_workflow("pipeline.yml")["jobs"]
    publish_env = jobs["publish"]["env"]
    assert publish_env["PUBLISH_BUILD_GENERATION"] == "${{ needs.build.outputs.generation }}"
    for key in ("rulesets", "txt", "mrs"):
        assert publish_env[f"PUBLISH_{key.upper()}_ID"] == "${{ needs.build.outputs." + key + "_id }}"
    names = {
        "rulesets": ("上传 rulesets 清单（供发布校验）", "下载 rulesets 产物（提供产物清单基线）"),
        "txt": ("上传 merged-rules 产物", "下载 merged-rules 产物"),
        "mrs": ("上传 merged-rules-mrs 产物", "下载 merged-rules-mrs 产物"),
    }
    for key, (upload_name, download_name) in names.items():
        upload = _step(jobs["build"], upload_name)
        download = _step(jobs["publish"], download_name)["with"]
        assert jobs["build"]["outputs"][f"{key}_id"] == (
            "${{ steps." + upload["id"] + ".outputs.artifact-id }}"
        )
        assert "github.run_attempt" in upload["with"]["name"]
        assert download["artifact-ids"] == "${{ needs.build.outputs." + key + "_id }}"
        assert download["digest-mismatch"] == "error"
        assert "name" not in download and "run-id" not in download and "github-token" not in download


def test_build_checks_the_complete_product_set_before_uploading():
    job = load_workflow("pipeline.yml")["jobs"]["build"]
    steps = job["steps"]
    gate = _step(job, "检查同代制品集合与内容")
    assert steps.index(gate) > steps.index(_step(job, "转换规则集"))
    assert gate["run"] == "python scripts/release_handler.py --verify-only --require-baseline"
    for name in ("上传 rulesets 清单（供发布校验）", "上传 merged-rules 产物", "上传 merged-rules-mrs 产物"):
        assert steps.index(gate) < steps.index(_step(job, name))


def test_kernel_bump_amd64_validation_rechecks_live_latest():
    job = load_workflow("kernel-bump.yml")["jobs"]["validate_amd64"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["needs"] == "prepare"
    assert job["if"] == "needs.prepare.outputs.changed == 'true'"

    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "kernel_pin.py --apply" in runs
    assert '--tag "$TAG"' in runs
    assert '--kernel-sha256 "$KERNEL_SHA256"' in runs
    assert '--kernel-sha256-arm64 "$KERNEL_SHA256_ARM64"' in runs
    assert "convert_mrs.py --verify-current-kernel" in runs

    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("记录当前钉扎版本") < names.index("在干净检出中重建配置")
    assert names.index("在干净检出中重建配置") < names.index("独立复核实时 latest 与版本单调性")

    current = steps[names.index("记录当前钉扎版本")]
    assert "read_metadata" in current["run"]
    assert 'print("tag=" + read_metadata()["pinned_version"])' in current["run"]

    recheck = steps[names.index("独立复核实时 latest 与版本单调性")]
    assert recheck["env"]["CANDIDATE_TAG"] == "${{ needs.prepare.outputs.tag }}"
    assert recheck["env"]["CURRENT_TAG"] == "${{ steps.current.outputs.tag }}"
    script = recheck["run"]
    assert '"repo_api"' in script
    assert "fetch_release(None)" in script
    assert '"published_at"' in script
    assert 'if latest.get("tag_name") != candidate:' in script
    assert "if candidate_published < current_published:" in script
    assert "::error::" in script
    assert "sys.exit(1)" in script


def test_kernel_bump_commit_requires_both_architecture_validations():
    workflow = load_workflow("kernel-bump.yml")
    commit = workflow["jobs"]["commit"]
    assert commit["needs"] == ["prepare", "validate_arm64", "validate_amd64"]
    assert "needs.validate_arm64.result == 'success'" in commit["if"]
    assert "needs.validate_amd64.result == 'success'" in commit["if"]
    assert commit["if"].endswith(DEFAULT_BRANCH_GATE)


def test_kernel_bump_notify_tracks_amd64_result():
    workflow = load_workflow("kernel-bump.yml")
    notify = workflow["jobs"]["notify-on-failure"]
    assert notify["needs"] == ["prepare", "validate_arm64", "validate_amd64", "commit"]
    step = notify["steps"][0]
    assert step["env"]["R_VALIDATE_AMD64"] == "${{ needs.validate_amd64.result }}"
    failed_gate, rest = step["run"].split("FAILED=1", 1)
    assert "$R_VALIDATE_AMD64" in failed_gate
    assert '[ "$R_VALIDATE_AMD64" != "success" ]' in rest
