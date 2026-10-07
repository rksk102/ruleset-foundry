import config_loader
import pytest


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


class TestMergeDict:
    def test_scalar_override(self):
        base = {"a": 1}
        config_loader._merge_dict(base, {"a": 2})
        assert base == {"a": 2}

    def test_new_key_added(self):
        base = {"a": 1}
        config_loader._merge_dict(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_nested_merged_not_replaced(self):
        base = {"net": {"timeout": 15, "retries": 2}}
        config_loader._merge_dict(base, {"net": {"timeout": 30}})
        assert base == {"net": {"timeout": 30, "retries": 2}}

    def test_deep_nesting(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        config_loader._merge_dict(base, {"a": {"b": {"c": 9}}})
        assert base == {"a": {"b": {"c": 9, "d": 2}}}

    def test_dict_replaced_by_scalar(self):
        base = {"a": {"b": 1}}
        config_loader._merge_dict(base, {"a": 5})
        assert base == {"a": 5}

    def test_scalar_replaced_by_dict(self):
        base = {"a": 1}
        config_loader._merge_dict(base, {"a": {"b": 2}})
        assert base == {"a": {"b": 2}}

    def test_empty_override_keeps_base(self):
        base = {"a": 1}
        config_loader._merge_dict(base, {})
        assert base == {"a": 1}


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
