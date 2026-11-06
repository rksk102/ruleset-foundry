import json
import os
import re
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


def _dependency_policy():
    return yaml.safe_load((REPO / ".github" / "dependency-merge.yml").read_text(encoding="utf-8"))


def test_dependency_merge_group_whitelist_matches_dependabot_config():
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
    assert allowed == non_major
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
    assert set(token_permissions) == {"contents", "pull-requests", "checks"}
    assert registration["public"] is False
    assert registration["default_events"] == []


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


def _run_dependency_merge_step(work_dir, pr_number, head_branch, head_sha="a" * 40, event_name="workflow_run"):
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
            "DRY_RUN": "true",
            "RECONCILE": "false",
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
