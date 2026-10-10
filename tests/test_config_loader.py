import os
import subprocess
import sys
from pathlib import Path

import config_loader
import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
BASE = (
    "network:\n"
    "  timeout_seconds: 15\n"
    "  max_retries: 2\n"
    "  max_source_bytes: 67108864\n"
    "paths:\n"
    "  sources_file: \"sources.urls\"\n"
    "behavior:\n"
    "  allow_partial: false\n"
)


def use_config(work_dir, content=None):
    config_loader._CONFIG = None
    path = work_dir / "config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    config_loader._CONFIG_FILE = path
    return path


def reset_config():
    import pathlib

    config_loader._CONFIG = None
    config_loader._CONFIG_FILE = pathlib.Path("config.yaml")


class TestLoadConfig:
    def test_missing_file_uses_defaults(self, work_dir):
        use_config(work_dir)
        try:
            cfg = config_loader.load_config()
            assert cfg["network"]["timeout_seconds"] == 15
            assert cfg["network"]["max_concurrency"] == 6
            assert cfg["network"]["max_per_host"] == 2
            assert cfg["network"]["max_retry_after_seconds"] == 60
            assert cfg["paths"]["merged_output_dir"] == "merged-rules"
            assert cfg["paths"]["mrs_output_dir"] == "merged-rules-mrs"
            assert cfg["behavior"]["conflict_policy"] == "warn"
            assert cfg["behavior"]["unrecognized_warn_ratio"] == 0.10
            assert cfg["mihomo"]["kernel_sha256"] == ""
            assert cfg["mihomo"]["kernel_sha256_arm64"] == ""
            assert cfg["merges"] == []
        finally:
            reset_config()

    def test_result_is_cached(self, work_dir):
        use_config(work_dir)
        try:
            assert config_loader.load_config() is config_loader.load_config()
        finally:
            reset_config()

    def test_user_file_overrides_only_given_keys(self, work_dir):
        use_config(work_dir, "behavior:\n  conflict_policy: fail\n")
        try:
            cfg = config_loader.load_config()
            assert cfg["behavior"]["conflict_policy"] == "fail"
            assert cfg["behavior"]["release_keep_days"] == 3
            assert cfg["behavior"]["release_change_detection"] is True
        finally:
            reset_config()

    def test_nested_merge_keeps_sibling_defaults(self, work_dir):
        use_config(work_dir, "network:\n  timeout_seconds: 30\n")
        try:
            cfg = config_loader.load_config()
            assert cfg["network"]["timeout_seconds"] == 30
            assert cfg["network"]["max_retries"] == 2
            assert cfg["network"]["max_concurrency"] == 6
        finally:
            reset_config()

    def test_empty_file_falls_back_to_defaults(self, work_dir):
        use_config(work_dir, "")
        try:
            assert config_loader.load_config()["behavior"]["conflict_policy"] == "warn"
        finally:
            reset_config()

    def test_malformed_yaml_is_rejected(self, work_dir):
        use_config(work_dir, "a: [1, 2\n")
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

class TestGet:
    def test_single_key(self, work_dir):
        use_config(work_dir)
        try:
            assert config_loader.get("behavior")["conflict_policy"] == "warn"
        finally:
            reset_config()

    def test_nested_keys(self, work_dir):
        use_config(work_dir)
        try:
            assert config_loader.get("network", "timeout_seconds") == 15
        finally:
            reset_config()

    def test_missing_returns_default(self, work_dir):
        use_config(work_dir)
        try:
            assert config_loader.get("nope") is None
            assert config_loader.get("nope", default=7) == 7
            assert config_loader.get("network", "nope", default="x") == "x"
        finally:
            reset_config()

    def test_traversing_through_scalar_returns_default(self, work_dir):
        use_config(work_dir)
        try:
            assert config_loader.get("network", "timeout_seconds", "deeper", default=9) == 9
        finally:
            reset_config()

    def test_zero_release_keep_days_is_rejected(self, work_dir):
        use_config(work_dir, "behavior:\n  release_keep_days: 0\n")
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

    def test_release_keep_days_over_list_capacity_is_rejected(self, work_dir):
        use_config(work_dir, "behavior:\n  release_keep_days: 26\n")
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

    def test_zero_unrecognized_ratio_is_not_treated_as_missing(self, work_dir):
        use_config(work_dir, "behavior:\n  unrecognized_warn_ratio: 0\n")
        try:
            assert config_loader.get("behavior", "unrecognized_warn_ratio", default=99) == 0
        finally:
            reset_config()

    def test_false_is_not_treated_as_missing(self, work_dir):
        use_config(work_dir, "behavior:\n  allow_partial: false\n")
        try:
            assert config_loader.get("behavior", "allow_partial", default=True) is False
        finally:
            reset_config()


class TestWindowsPathValues:
    @pytest.mark.parametrize("value", ["foo:bar", "NUL", "NUL.txt", "CON", "COM1.txt"])
    def test_windows_unsafe_path_values_are_rejected(self, work_dir, value):
        use_config(work_dir, f"paths:\n  rulesets_dir: \"{value}\"\n")
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()


class TestValidationMatrix:
    @pytest.mark.parametrize("body", [
        BASE.replace("max_source_bytes: 67108864", 'max_source_bytes: "67108864"'),
        BASE.replace("allow_partial: false", 'allow_partial: "false"'),
        BASE.replace("timeout_seconds: 15", "timeout_seconds: -1"),
        BASE.replace("max_source_bytes: 67108864", "max_source_bytes: 0"),
        BASE + 'merges: "oops"\n',
        BASE + "merges:\n  - strategy: block\n    filename: a.txt\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: a.txt\n    inputs: []\n",
        BASE + '  conflict_policy: "explode"\n',
        "merges\n  - strategy: block\n",
        BASE.replace('sources_file: "sources.urls"', 'sources_file: "sources.urls"\n  rulesets_dir: ""'),
        BASE.replace('sources_file: "sources.urls"', 'sources_file: "  "'),
        BASE + '  unrecognized_warn_ratio: 5\n',
        BASE + '  min_source_success_ratio: -1\n',
        BASE + '  unrecognized_warn_ratio: 1' + '0' * 400 + '\n',
        BASE + "behaviour:\n  release_keep_days: 3\n",
        BASE + "  allow_particals: true\n",
        BASE + "foo: {}\n",
        BASE + "merges:\n  - strategy: '../../outside'\n    type: domain\n"
               "    owner: o\n    filename: a.txt\n    inputs: ['x.txt']\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: '../PWNED.txt'\n    inputs: ['x.txt']\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: a.txt\n    inputs: ['/etc/hosts']\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: a.txt\n    inputs: ['../../../../etc/passwd']\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: a.txt\n    inputs: [123]\n",
    ])
    def test_invalid_config_is_rejected(self, work_dir, body):
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

    @pytest.mark.parametrize("body", [
        BASE,
        BASE + "merges: []\n",
        BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
               "    filename: a.txt\n    inputs: ['x.txt']\n",
    ])
    def test_supported_config_variants_load(self, work_dir, body):
        use_config(work_dir, body)
        try:
            config_loader.load_config()
        finally:
            reset_config()


class TestPyYamlMissing:
    def test_missing_pyyaml_is_hard_error(self, work_dir, monkeypatch):
        (work_dir / "config.yaml").write_text(BASE, encoding="utf-8")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        child = (
            "import config_loader as C\n"
            "C._HAS_YAML = False\n"
            "try:\n"
            "    C.load_config()\n"
            "    print('NO_ERROR')\n"
            "except C.ConfigError as e:\n"
            "    print('CONFIG_ERROR', str(e)[:80])\n"
        )
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(work_dir), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, env=env,
        )
        assert proc.stdout.strip().startswith("CONFIG_ERROR")
