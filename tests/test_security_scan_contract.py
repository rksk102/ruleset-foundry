"""安全扫描工作流与 OSV 例外策略的契约。"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "security-scan.yml"
SETUP_PYTHON_ENV = REPO / ".github" / "actions" / "setup-python-env" / "action.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OSV_IMAGE = (
    "docker://ghcr.io/google/osv-scanner-action"
    "@sha256:3fa25659351c9a5527b30d73b6f3c2ee0bcac106d6220948211d87379d08f243"
)
def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def scan_job():
    return load_workflow()["jobs"]["osv-scanner"]


def scan_step(name):
    return next(step for step in scan_job()["steps"] if step.get("name") == name)


def args_lines(step):
    return [line.strip() for line in step["with"]["args"].splitlines() if line.strip()]


def test_triggers_include_weekly_schedule_and_dispatch():
    workflow = load_workflow()
    trigger = workflow.get("on") or workflow.get(True)
    crons = [entry["cron"] for entry in trigger["schedule"]]
    assert crons, "security-scan.yml 缺少 schedule 触发"
    assert all(len(cron.split()) == 5 for cron in crons), crons
    assert "workflow_dispatch" in trigger


def test_setup_uv_pins_exact_approved_version():
    action = yaml.safe_load(SETUP_PYTHON_ENV.read_text(encoding="utf-8"))
    setup = next(
        step
        for step in action["runs"]["steps"]
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    )
    assert SHA_RE.fullmatch(setup["uses"].rsplit("@", 1)[1])
    assert setup["with"]["version"] == "0.13.9"


def test_scanner_and_reporter_use_pinned_oci_digest():
    job = scan_job()
    assert job["runs-on"] == "ubuntu-latest"
    assert "uses" not in job
    for name in ("Run scanner", "Run osv-scanner-reporter"):
        assert scan_step(name)["uses"] == OSV_IMAGE


def test_all_github_actions_use_full_commit_shas():
    uses = {
        step["uses"]
        for step in scan_job()["steps"]
        if step.get("uses") and not step["uses"].startswith("docker://")
    }
    assert all(SHA_RE.fullmatch(ref.rsplit("@", 1)[1]) for ref in uses)


def test_scan_args_preserve_lockfile_and_output_contract():
    scanner = scan_step("Run scanner")
    assert args_lines(scanner) == [
        "--output=results.json",
        "--format=json",
        "--lockfile=uv.lock",
        "--config=osv-scanner.toml",
    ]
    assert scanner["env"]["GOTOOLCHAIN"] == "auto"
    assert scanner["continue-on-error"] is True


def test_result_gate_runs_after_scanner_before_reporter():
    steps = scan_job()["steps"]
    check = scan_step("Check that the scan completed")
    assert steps.index(scan_step("Run scanner")) < steps.index(check)
    assert steps.index(check) < steps.index(scan_step("Run osv-scanner-reporter"))
    assert "if" not in check
    assert "if" not in scan_step("Run osv-scanner-reporter")


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (None, 1),
        ("", 1),
        ('{"results":[', 1),
        ("[]", 1),
        ("{}", 1),
        ('{"results":{}}', 1),
        ('{"results":[NaN]}', 1),
        ('{"results":[]}', 0),
        ('{"results":[{"source":{"path":"uv.lock"},"packages":[]}]}', 0),
    ],
    ids=[
        "missing", "empty", "truncated", "not-object", "no-results",
        "not-array", "non-json-number", "clean", "populated",
    ],
)
def test_result_gate_rejects_unusable_scanner_output(work_dir, content, expected_code):
    check = scan_step("Check that the scan completed")
    if content is not None:
        (work_dir / "results.json").write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["bash", "-e", "-c", check["run"]], cwd=work_dir,
        env={**os.environ, **check.get("env", {})}, capture_output=True, text=True, check=False,
    )

    assert result.returncode == expected_code, result.stdout + result.stderr
    if expected_code:
        assert "::error::" in result.stdout


def test_reporter_preserves_workspace_paths_and_vulnerability_gate():
    reporter = scan_step("Run osv-scanner-reporter")
    assert reporter["with"]["entrypoint"] == "/root/osv-reporter"
    assert args_lines(reporter) == [
        "--output=results.sarif",
        "--new=results.json",
        "--gh-annotations=false",
        "--fail-on-vuln=true",
    ]
    assert all(not line.startswith(("--output=/", "--new=/")) for line in args_lines(reporter))


def test_sarif_upload_and_artifact_failure_semantics_are_preserved():
    artifact = scan_step("Upload artifact")
    assert artifact["id"] == "upload_artifact"
    assert artifact["if"] == "${{ !cancelled() }}"
    assert artifact["with"]["name"] == "OSV Scanner SARIF file"
    assert artifact["with"]["path"] == "results.sarif"
    assert artifact["with"]["retention-days"] == 5

    upload = scan_step("Upload to code-scanning")
    assert upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["sarif_file"] == "results.sarif"

    troubleshooter = scan_step("Error troubleshooter")
    assert troubleshooter["if"] == "${{ always() && steps.upload_artifact.outcome == 'failure' }}"
    assert "exit 1" in troubleshooter["run"]


def test_permissions_grant_only_existing_security_events_write():
    workflow = load_workflow()
    job = scan_job()
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}


def test_ignored_vulns_have_reason_and_ignore_until():
    config = tomllib.loads((REPO / "osv-scanner.toml").read_text(encoding="utf-8"))
    ignored = config.get("IgnoredVulns", [])
    assert isinstance(ignored, list)
    for entry in ignored:
        assert entry.get("reason"), f"IgnoredVulns 条目缺少 reason: {entry!r}"
        assert entry.get("ignoreUntil"), f"IgnoredVulns 条目缺少 ignoreUntil: {entry!r}"
