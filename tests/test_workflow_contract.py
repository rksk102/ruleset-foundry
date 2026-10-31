import re
from pathlib import Path

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


def test_dependabot_auto_merge_requires_bot_head_actor():
    job = load_workflow("dependabot-auto-merge.yml")["jobs"]["auto-merge"]
    assert "github.event.workflow_run.actor.login == 'dependabot[bot]'" in job["if"]


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


def test_artifacts_branch_marker_match_skips_repush():
    job = load_workflow("pipeline.yml")["jobs"]["publish"]
    step = next(s for s in job["steps"] if s.get("id") == "product-state")
    assert re.search(
        r'elif \[ "\$ARTIFACTS_HASH" != "\$CURRENT" \]; then\n'
        r'.*\n\s*echo "changed=true" >> "\$GITHUB_OUTPUT"\n'
        r'\s*else\n\s*echo "changed=false" >> "\$GITHUB_OUTPUT"',
        step["run"],
    )


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
