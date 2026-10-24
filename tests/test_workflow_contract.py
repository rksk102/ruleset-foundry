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


def test_setup_python_env_pins_exact_patch_version():
    action_path = REPO / ".github" / "actions" / "setup-python-env" / "action.yml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    setup = next(
        step
        for step in action["runs"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", setup["with"]["python-version"])
