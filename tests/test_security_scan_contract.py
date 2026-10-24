"""安全扫描工作流与 OSV 例外策略的契约。"""

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "security-scan.yml"
OSV_REUSABLE = (
    "google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def scan_job():
    workflow = load_workflow()
    job = workflow["jobs"]["osv-scanner"]
    assert job["uses"].startswith(f"{OSV_REUSABLE}@")
    return job


def test_triggers_include_weekly_schedule_and_dispatch():
    workflow = load_workflow()
    trigger = workflow.get("on") or workflow.get(True)
    crons = [entry["cron"] for entry in trigger["schedule"]]
    assert crons, "security-scan.yml 缺少 schedule 触发"
    assert all(len(cron.split()) == 5 for cron in crons), crons
    assert "workflow_dispatch" in trigger


def test_reusable_workflow_is_pinned_to_40_char_sha():
    ref = scan_job()["uses"].split("@", 1)[1]
    assert SHA_RE.fullmatch(ref), f"reusable workflow 未钉扎 40 位 SHA: {ref!r}"


def test_scan_args_cover_lockfile_and_config():
    with_args = scan_job()["with"]
    args = with_args["scan-args"]
    lines = [line.strip() for line in args.splitlines() if line.strip()]
    assert "--lockfile=uv.lock" in lines
    assert "--config=osv-scanner.toml" in lines
    assert with_args["fail-on-vuln"] is True
    assert with_args["upload-sarif"] is True


def test_permissions_grant_security_events_write():
    workflow = load_workflow()
    assert workflow["permissions"]["security-events"] == "write"
    assert workflow["permissions"]["actions"] == "read"
    assert workflow["permissions"]["contents"] == "read"


def test_ignored_vulns_have_reason_and_ignore_until():
    config = tomllib.loads((REPO / "osv-scanner.toml").read_text(encoding="utf-8"))
    ignored = config.get("IgnoredVulns", [])
    assert isinstance(ignored, list)
    for entry in ignored:
        assert entry.get("reason"), f"IgnoredVulns 条目缺少 reason: {entry!r}"
        assert entry.get("ignoreUntil"), f"IgnoredVulns 条目缺少 ignoreUntil: {entry!r}"
