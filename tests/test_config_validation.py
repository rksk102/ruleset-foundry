import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")


def child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = SCRIPTS
    env["PYTHONIOENCODING"] = "utf-8"
    return env


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


def write_cfg(work_dir, body):
    (work_dir / "config.yaml").write_text(body, encoding="utf-8")
    return work_dir


def run_child(cwd):
    child = (
        "import json\n"
        "from config_loader import load_config, ConfigError\n"
        "try:\n"
        "    cfg = load_config()\n"
        "    print('OK', json.dumps({'merges': len(cfg.get('merges') or []),"
        " 'allow_partial': cfg['behavior']['allow_partial'],"
        " 'timeout': cfg['network']['timeout_seconds']}))\n"
        "except ConfigError as e:\n"
        "    print('CONFIG_ERROR', str(e)[:120])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=child_env(), timeout=60,
    )
    return (proc.stdout or "").strip() or f"<no stdout; stderr={proc.stderr.strip()[:200]}>"


class TestConfigValidation:
    def test_quoted_int_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace("max_source_bytes: 67108864",
                                         'max_source_bytes: "67108864"'))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "max_source_bytes" in out

    def test_quoted_bool_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace("allow_partial: false", 'allow_partial: "false"'))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "allow_partial" in out

    def test_negative_int_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace("timeout_seconds: 15", "timeout_seconds: -1"))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "正整数" in out

    def test_zero_max_source_bytes_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace("max_source_bytes: 67108864",
                                         "max_source_bytes: 0"))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")

    def test_merges_as_string_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + 'merges: "oops"\n')
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "merges" in out

    def test_merges_missing_required_field_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + "merges:\n  - strategy: block\n    filename: a.txt\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "缺少必需字段" in out

    def test_merges_empty_inputs_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: a.txt\n    inputs: []\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")

    def test_bad_conflict_policy_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + '  conflict_policy: "explode"\n')
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "conflict_policy" in out

    def test_yaml_syntax_error_is_rejected(self, work_dir):
        write_cfg(work_dir, "merges\n  - strategy: block\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "YAML" in out

    def test_valid_config_loads(self, work_dir):
        write_cfg(work_dir, BASE)
        out = run_child(work_dir)
        assert out.startswith("OK")

    def test_empty_rulesets_dir_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace('sources_file: "sources.urls"',
                                         'sources_file: "sources.urls"\n  rulesets_dir: ""'))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "rulesets_dir" in out

    def test_empty_sources_file_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE.replace('sources_file: "sources.urls"',
                                         'sources_file: "  "'))
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "sources_file" in out

    def test_merges_empty_list_is_allowed(self, work_dir):
        write_cfg(work_dir, BASE + "merges: []\n")
        out = run_child(work_dir)
        assert out.startswith("OK")

    def test_valid_merges_loads(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: a.txt\n    inputs: ['x.txt']\n")
        out = run_child(work_dir)
        assert out.startswith("OK")
        assert '"merges": 1' in out

    def test_int_for_float_is_range_checked(self, work_dir):
        write_cfg(work_dir, BASE + "  unrecognized_warn_ratio: 5\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "unrecognized_warn_ratio" in out

    def test_negative_int_ratio_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + "  min_source_success_ratio: -1\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")

    def test_huge_int_ratio_is_config_error_not_traceback(self, work_dir):
        write_cfg(work_dir, BASE + "  unrecognized_warn_ratio: 1" + "0" * 400 + "\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "float" in out

    def test_unknown_section_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + "behaviour:\n  release_keep_days: 3\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "behaviour" in out

    def test_unknown_key_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + "  allow_particals: true\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "allow_particals" in out

    def test_unknown_empty_section_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE + "foo: {}\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "未知配置节" in out

    def test_merges_traversal_field_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: '../../outside'\n    type: domain\n"
                    "    owner: o\n    filename: a.txt\n    inputs: ['x.txt']\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "strategy" in out

    def test_merges_filename_with_separator_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: '../PWNED.txt'\n    inputs: ['x.txt']\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "filename" in out

    def test_merges_absolute_input_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: a.txt\n    inputs: ['/etc/hosts']\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")
        assert "相对路径" in out

    def test_merges_parent_ref_input_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: a.txt\n    inputs: ['../../../../etc/passwd']\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")

    def test_merges_non_string_input_is_rejected(self, work_dir):
        write_cfg(work_dir, BASE
                  + "merges:\n  - strategy: block\n    type: domain\n    owner: o\n"
                    "    filename: a.txt\n    inputs: [123]\n")
        out = run_child(work_dir)
        assert out.startswith("CONFIG_ERROR")


class TestPyYamlMissing:
    def test_missing_pyyaml_is_hard_error(self, work_dir):
        write_cfg(work_dir, BASE)
        child = (
            "import sys\n"
            "import config_loader as C\n"
            "C._HAS_YAML = False\n"
            "try:\n"
            "    C.load_config()\n"
            "    print('NO_ERROR')\n"
            "except C.ConfigError as e:\n"
            "    print('CONFIG_ERROR', str(e)[:80])\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(work_dir), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        assert proc.stdout.strip().startswith("CONFIG_ERROR")


