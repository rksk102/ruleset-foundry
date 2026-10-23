from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


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
    for job_id in ("publish", "notify-on-failure"):
        assert "github.event.repository.default_branch" in workflow["jobs"][job_id]["if"]


def test_kernel_notify_gates_on_default_branch():
    workflow = load_workflow("kernel-bump.yml")
    assert "github.event.repository.default_branch" in workflow["jobs"]["notify-on-failure"]["if"]
