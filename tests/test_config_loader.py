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
    '  sources_file: "sources.urls"\n'
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

    def test_https_repo_api_is_accepted(self, work_dir):
        use_config(work_dir, BASE + 'mihomo:\n  repo_api: "https://api.github.com/repos/X/Y/releases"\n')
        try:
            assert config_loader.get("mihomo", "repo_api").startswith("https://")
        finally:
            reset_config()

    def test_valid_kernel_pin_formats_are_accepted(self, work_dir):
        use_config(
            work_dir,
            BASE
            + (
                "mihomo:\n"
                '  pinned_version: "v1.19.32-rc1"\n'
                f'  kernel_sha256: "{"a" * 64}"\n'
                f'  kernel_sha256_arm64: "{"b" * 64}"\n'
            ),
        )
        try:
            cfg = config_loader.load_config()
            assert cfg["mihomo"]["pinned_version"] == "v1.19.32-rc1"
            assert cfg["mihomo"]["kernel_sha256"] == "a" * 64
        finally:
            reset_config()

    @pytest.mark.parametrize(
        "repo_api",
        [
            "http://api.github.com/repos/X/Y/releases",
            "https://",
            "not-a-url",
        ],
    )
    def test_non_https_repo_api_is_rejected(self, work_dir, repo_api):
        use_config(work_dir, BASE + f'mihomo:\n  repo_api: "{repo_api}"\n')
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
        use_config(work_dir, "behavior:\n  release_keep_days: 91\n")
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
        use_config(work_dir, f'paths:\n  rulesets_dir: "{value}"\n')
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()


class TestPathValidationHardening:
    @pytest.mark.parametrize(
        "value",
        ["./.", ".//.", "scripts", "Scripts", ".git/config", "docs", "tests/../scripts"],
    )
    def test_dot_and_protected_paths_are_rejected(self, work_dir, value):
        use_config(work_dir, f'paths:\n  merged_output_dir: "{value}"\n')
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

    def test_path_value_is_stripped(self):
        assert config_loader._validate_path_value("x", "  rulesets  ") == "rulesets"

    def test_stripped_path_is_written_back(self, work_dir):
        use_config(work_dir, 'paths:\n  merged_output_dir: "  merged-rules  "\n')
        try:
            cfg = config_loader.load_config()
            assert cfg["paths"]["merged_output_dir"] == "merged-rules"
        finally:
            reset_config()

    def test_merge_input_is_stripped_and_written_back(self, work_dir):
        body = (
            "merges:\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt\n"
            '    inputs: ["  block/domain/A/a.txt  "]\n'
        )
        use_config(work_dir, body)
        try:
            cfg = config_loader.load_config()
            assert cfg["merges"][0]["inputs"][0] == "block/domain/A/a.txt"
        finally:
            reset_config()


def path_config(paths=None, mihomo=None):
    def yaml_value(value):
        return value.replace("\\", "\\\\")

    lines = []
    if paths:
        lines.append("paths:")
        lines.extend(f'  {key}: "{yaml_value(value)}"' for key, value in paths.items())
    if mihomo:
        lines.append("mihomo:")
        lines.extend(f'  {key}: "{yaml_value(value)}"' for key, value in mihomo.items())
    return "\n".join(lines) + "\n"


class TestPathRelationshipValidation:
    @pytest.mark.parametrize(
        "body",
        [
            path_config({"rulesets_dir": "shared", "merged_output_dir": "shared"}),
            path_config({"rulesets_dir": "Shared", "merged_output_dir": "shared/txt"}),
            path_config({"merged_output_dir": "shared/logs", "log_dir": "shared"}),
            path_config({"rulesets_dir": "shared", "mrs_output_dir": "shared\\mrs"}),
            path_config({"log_dir": "shared"}, {"kernel_cache_path": "shared"}),
        ],
    )
    def test_directories_must_not_equal_or_nest(self, work_dir, body):
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="路径冲突"):
                config_loader.load_config()
        finally:
            reset_config()

    @pytest.mark.parametrize(
        "body",
        [
            path_config({"rulesets_dir": "shared/rulesets", "sources_file": "shared/rulesets/sources.urls"}),
            path_config({"merged_output_dir": "shared/merged", "sources_file": "shared/merged/sources.urls"}),
            path_config({"mrs_output_dir": "shared/mrs", "sources_file": "shared/mrs/sources.urls"}),
            path_config({"log_dir": "shared/logs", "sources_file": "shared/logs/sources.urls"}),
            path_config({"sources_file": "shared/kernel/sources.urls"}, {"kernel_cache_path": "shared/kernel"}),
        ],
    )
    def test_sources_file_must_not_live_under_directory(self, work_dir, body):
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="路径冲突"):
                config_loader.load_config()
        finally:
            reset_config()

    @pytest.mark.parametrize(
        "body",
        [
            path_config({"rulesets_dir": "shared/rulesets", "sources_file": "shared/rulesets"}),
            path_config({"sources_file": "shared/kernel"}, {"kernel_cache_path": "shared/kernel"}),
        ],
    )
    def test_sources_file_must_not_equal_directory(self, work_dir, body):
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="路径冲突"):
                config_loader.load_config()
        finally:
            reset_config()

    def test_repo_default_config_paths_are_accepted(self, work_dir):
        config_loader._CONFIG = None
        config_loader._CONFIG_FILE = config_loader._REPO_CONFIG_FILE
        try:
            cfg = config_loader.load_config()
            assert cfg["paths"]["rulesets_dir"] == "rulesets"
            assert cfg["paths"]["merged_output_dir"] == "merged-rules"
            assert cfg["paths"]["mrs_output_dir"] == "merged-rules-mrs"
        finally:
            reset_config()


class TestMergeFieldWhitelist:
    def merge_body(self, extra):
        return (
            "merges:\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt\n"
            "    inputs: ['x.txt']\n" + extra
        )

    @pytest.mark.parametrize(
        ("extra", "pattern"),
        [
            ("    descripton: 拼写错误\n", "未知字段"),
            ("    unexpected: 1\n", "未知字段"),
            ("    description: 123\n", "description 期望 str"),
            ("    description: [a, b]\n", "description 期望 str"),
        ],
    )
    def test_invalid_merge_fields_are_rejected(self, work_dir, extra, pattern):
        use_config(work_dir, self.merge_body(extra))
        try:
            with pytest.raises(config_loader.ConfigError, match=pattern):
                config_loader.load_config()
        finally:
            reset_config()

    def test_valid_description_is_accepted(self, work_dir):
        use_config(work_dir, self.merge_body("    description: 合并广告列表\n"))
        try:
            cfg = config_loader.load_config()
            assert cfg["merges"][0]["description"] == "合并广告列表"
        finally:
            reset_config()


class TestMergeOutputUniqueness:
    def test_duplicate_output_is_rejected(self, work_dir):
        body = (
            "merges:\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt\n"
            "    inputs: ['x.txt']\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt\n"
            "    inputs: ['y.txt']\n"
        )
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="输出同一路径"):
                config_loader.load_config()
        finally:
            reset_config()

    def test_duplicate_output_case_insensitive_is_rejected(self, work_dir):
        body = (
            "merges:\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt\n"
            "    inputs: ['x.txt']\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: O\n"
            "    filename: a.txt\n"
            "    inputs: ['y.txt']\n"
        )
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="输出同一路径"):
                config_loader.load_config()
        finally:
            reset_config()

    def test_trailing_dot_output_is_rejected(self, work_dir):
        body = (
            "merges:\n"
            "  - strategy: block\n"
            "    type: domain\n"
            "    owner: o\n"
            "    filename: a.txt.\n"
            "    inputs: ['x.txt']\n"
        )
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError, match="不能以点结尾"):
                config_loader.load_config()
        finally:
            reset_config()


class TestValidationMatrix:
    @pytest.mark.parametrize(
        "body",
        [
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
            BASE + "  unrecognized_warn_ratio: 5\n",
            BASE + "  min_source_success_ratio: -1\n",
            BASE + "  unrecognized_warn_ratio: 1" + "0" * 400 + "\n",
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
            BASE + "merges:\n  - strategy: block\n    type: classical\n    owner: o\n"
            "    filename: a.txt\n    inputs: ['x.txt']\n",
            BASE + 'mihomo:\n  kernel_sha256: "00"\n',
            BASE + 'mihomo:\n  kernel_sha256_arm64: "zz"\n',
            BASE + 'mihomo:\n  pinned_version: "../v1"\n',
        ],
    )
    def test_invalid_config_is_rejected(self, work_dir, body):
        use_config(work_dir, body)
        try:
            with pytest.raises(config_loader.ConfigError):
                config_loader.load_config()
        finally:
            reset_config()

    @pytest.mark.parametrize(
        "body",
        [
            BASE,
            BASE + "merges: []\n",
            BASE + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
            "    filename: a.txt\n    inputs: ['x.txt']\n",
        ],
    )
    def test_supported_config_variants_load(self, work_dir, body):
        use_config(work_dir, body)
        try:
            config_loader.load_config()
        finally:
            reset_config()


class TestResolveConfigFile:
    def test_cwd_config_wins_over_repo_root(self, work_dir, monkeypatch):
        (work_dir / "config.yaml").write_text("behavior:\n  conflict_policy: fail\n", encoding="utf-8")
        monkeypatch.chdir(work_dir)
        config_loader._CONFIG_FILE = Path("config.yaml")
        try:
            assert Path(config_loader.resolve_config_file()) == Path("config.yaml")
        finally:
            reset_config()

    def test_falls_back_to_repo_root_config(self, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)
        config_loader._CONFIG_FILE = Path("config.yaml")
        try:
            resolved = Path(config_loader.resolve_config_file())
            assert resolved == config_loader._REPO_CONFIG_FILE
            assert resolved.exists()
        finally:
            reset_config()


class TestEntryPointConfigErrors:
    @pytest.mark.parametrize(
        "module",
        [
            "merger",
            "convert_mrs",
            "release_handler",
            "gen_readme",
        ],
    )
    def test_entry_scripts_report_config_error(self, work_dir, module):
        (work_dir / "config.yaml").write_text("behavior: [broken\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        assert proc.returncode == 1, proc.stderr
        assert "::error::配置校验失败" in proc.stdout


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
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        assert proc.stdout.strip().startswith("CONFIG_ERROR")
