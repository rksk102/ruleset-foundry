import os
import shutil
import subprocess
import sys
from pathlib import Path

import manifest
import pytest
from support import domain_mrs_bytes
from utils import combined_products_hash

REPO = Path(__file__).resolve().parent.parent
ENTRIES = ["main.py", "merger.py", "convert_mrs.py", "gen_readme.py", "release_handler.py"]


def configuration(prefix):
    return (
        "paths:\n"
        f"  sources_file: {prefix}-inputs.urls\n"
        f"  rulesets_dir: {prefix}-rules\n"
        f"  merged_output_dir: {prefix}-text\n"
        f"  mrs_output_dir: {prefix}-mrs\n"
        f"  log_dir: {prefix}-logs\n"
    )


@pytest.fixture
def cli_workspace(work_dir):
    root = work_dir / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for source in (REPO / "scripts").glob("*.py"):
        shutil.copyfile(source, scripts / source.name)
    (root / "config.yaml").write_text(configuration("repo"), encoding="utf-8")
    (root / "README.md").write_text("previous readme\n", encoding="utf-8")
    caller = work_dir / "caller"
    caller.mkdir()
    return root, caller


def run_cli(root, caller, entry):
    env = dict(
        os.environ,
        PYTHONPATH=str(root / "scripts"),
        PYTHONDONTWRITEBYTECODE="1",
        GITHUB_REPOSITORY="example/rules",
        GITHUB_STEP_SUMMARY=str(root / "step-summary.md"),
    )
    args = ["--print-hash"] if entry == "release_handler.py" else []
    return subprocess.run(
        [sys.executable, str(root / "scripts" / entry), *args],
        cwd=caller, env=env, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("caller_config", [None, "valid", "invalid"])
def test_cli_consumes_repo_config_before_entry_side_effects(cli_workspace, entry, caller_config):
    root, caller = cli_workspace
    if caller_config is not None:
        content = configuration("caller") if caller_config == "valid" else "unknown: true\n"
        (caller / "config.yaml").write_text(content, encoding="utf-8")
    before = {p.name: p.read_bytes() for p in caller.iterdir()}
    if entry == "release_handler.py":
        for prefix in ("repo", "caller"):
            txt = root / f"{prefix}-text" / "block/domain/Owner/x.txt"
            mrs = root / f"{prefix}-mrs" / "block/domain/Owner/x.mrs"
            txt.parent.mkdir(parents=True)
            mrs.parent.mkdir(parents=True)
            rule = f"{prefix}.example"
            txt.write_text(rule + "\n", encoding="utf-8")
            mrs.write_bytes(domain_mrs_bytes([rule]))

    result = run_cli(root, caller, entry)

    output = result.stdout + result.stderr
    assert "配置校验失败" not in output, output
    if entry == "release_handler.py":
        expected = combined_products_hash(root / "repo-text", root / "repo-mrs", format_tag=manifest.MANIFEST_MAGIC)[0]
        assert result.returncode == 0, output
        assert result.stdout.strip() == expected
    else:
        assert result.returncode == 1, output
        expected = {
            "main.py": "文件 repo-inputs.urls 未找到",
            "merger.py": "源目录 'repo-rules' 不存在",
            "convert_mrs.py": "源目录 repo-text 不存在",
            "gen_readme.py": f"产物目录缺失或为空: {root / 'repo-text'}",
        }[entry]
        assert expected in output, output
        assert (root / "repo-logs").is_dir()
    assert not (root / "caller-logs").exists()
    assert not (root / "caller-rules").exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "previous readme\n"
    assert {p.name: p.read_bytes() for p in caller.iterdir()} == before


@pytest.mark.parametrize("entry", ENTRIES)
def test_invalid_repo_configuration_cannot_be_hidden_by_caller_config(cli_workspace, entry):
    root, caller = cli_workspace
    (root / "config.yaml").write_text("network:\n  timeout_seconds: wrong\n", encoding="utf-8")
    (caller / "config.yaml").write_text(configuration("caller"), encoding="utf-8")

    result = run_cli(root, caller, entry)

    assert result.returncode == 1
    assert "::error::配置校验失败" in result.stdout
    assert "network.timeout_seconds" in result.stdout
    assert not (root / "repo-logs").exists()
    assert not (root / "caller-logs").exists()
    assert sorted(p.name for p in caller.iterdir()) == ["config.yaml"]
