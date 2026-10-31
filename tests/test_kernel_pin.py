import importlib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = """network:
  timeout_seconds: 15
paths:
  rulesets_dir: rulesets
mihomo:
  repo_api: https://api.github.com/repos/MetaCubeX/mihomo/releases/latest
  pinned_version: "v1.0.0"
  kernel_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  kernel_sha256_arm64: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
behavior:
  allow_partial: false
"""


def kernel_pin():
    return importlib.import_module("kernel_pin")


def test_kernel_bump_passes_only_metadata_and_rebuilds_in_clean_jobs():
    workflow = yaml.safe_load((REPO / ".github/workflows/kernel-bump.yml").read_text(encoding="utf-8"))
    prepare = workflow["jobs"]["prepare"]
    assert prepare["outputs"]["tag"] == "${{ steps.metadata.outputs.tag }}"
    assert prepare["outputs"]["kernel_sha256"] == "${{ steps.metadata.outputs.kernel_sha256 }}"
    assert prepare["outputs"]["kernel_sha256_arm64"] == "${{ steps.metadata.outputs.kernel_sha256_arm64 }}"

    for job_name in ("prepare", "validate_arm64", "commit"):
        steps = workflow["jobs"][job_name]["steps"]
        uses = [step.get("uses", "") for step in steps]
        assert not any("upload-artifact" in action or "download-artifact" in action for action in uses)

    for job_name in ("validate_arm64", "commit"):
        runs = "\n".join(step.get("run", "") for step in workflow["jobs"][job_name]["steps"])
        assert "kernel_pin.py --apply" in runs


def test_apply_pin_changes_only_three_fields(work_dir):
    path = work_dir / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    tag = "v1.19.34"
    amd64 = "1" * 64
    arm64 = "2" * 64

    kernel_pin().apply_pin(path, tag, amd64, arm64)

    before = yaml.safe_load(CONFIG)
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["mihomo"]["pinned_version"] == tag
    assert after["mihomo"]["kernel_sha256"] == amd64
    assert after["mihomo"]["kernel_sha256_arm64"] == arm64
    for section, values in before.items():
        for key, value in values.items():
            if (section, key) not in {
                ("mihomo", "pinned_version"),
                ("mihomo", "kernel_sha256"),
                ("mihomo", "kernel_sha256_arm64"),
            }:
                assert after[section][key] == value


@pytest.mark.parametrize(
    ("tag", "amd64", "arm64"),
    [
        ("bad/tag", "1" * 64, "2" * 64),
        ("v1.19.34", "not-a-sha", "2" * 64),
        ("v1.19.34", "1" * 64, "2" * 63),
    ],
)
def test_apply_pin_rejects_invalid_metadata_before_writing(work_dir, tag, amd64, arm64):
    path = work_dir / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")

    with pytest.raises(ValueError):
        kernel_pin().apply_pin(path, tag, amd64, arm64)

    assert path.read_text(encoding="utf-8") == CONFIG


def test_emit_outputs_only_validated_pin_metadata(work_dir, monkeypatch):
    import config_loader

    (work_dir / "config.yaml").write_text(CONFIG, encoding="utf-8")
    output = work_dir / "github-output.txt"
    monkeypatch.chdir(work_dir)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(config_loader, "_CONFIG", None)
    try:
        kernel_pin().main(["--emit"])
    finally:
        monkeypatch.setattr(config_loader, "_CONFIG", None)

    assert output.read_text(encoding="utf-8").splitlines() == [
        "tag=v1.0.0",
        f"kernel_sha256={'a' * 64}",
        f"kernel_sha256_arm64={'b' * 64}",
    ]
