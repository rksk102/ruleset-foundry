import os


import config_loader


def use_config(tmp_path, content=None):
    """把模块级缓存与配置文件路径指向临时目录，必要时写入配置内容。"""
    config_loader._CONFIG = None
    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    config_loader._CONFIG_FILE = path
    return path


def reset_config():
    """还原模块级状态，避免测试之间互相污染。"""
    import pathlib

    config_loader._CONFIG = None
    config_loader._CONFIG_FILE = pathlib.Path("config.yaml")
    os.environ.pop("STRICT_MODE", None)


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
        """未提及的兄弟键必须保留，否则用户局部配置会清空整段默认值。"""
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
    def test_missing_file_uses_defaults(self, tmp_path):
        use_config(tmp_path)
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

    def test_result_is_cached(self, tmp_path):
        use_config(tmp_path)
        try:
            assert config_loader.load_config() is config_loader.load_config()
        finally:
            reset_config()

    def test_user_file_overrides_only_given_keys(self, tmp_path):
        use_config(tmp_path, "behavior:\n  conflict_policy: fail\n")
        try:
            cfg = config_loader.load_config()
            assert cfg["behavior"]["conflict_policy"] == "fail"
            assert cfg["behavior"]["release_keep_days"] == 3
            assert cfg["behavior"]["release_change_detection"] is True
        finally:
            reset_config()

    def test_nested_merge_keeps_sibling_defaults(self, tmp_path):
        use_config(tmp_path, "network:\n  timeout_seconds: 30\n")
        try:
            cfg = config_loader.load_config()
            assert cfg["network"]["timeout_seconds"] == 30
            assert cfg["network"]["max_retries"] == 2
            assert cfg["network"]["max_concurrency"] == 6
        finally:
            reset_config()

    def test_empty_file_falls_back_to_defaults(self, tmp_path):
        use_config(tmp_path, "")
        try:
            assert config_loader.load_config()["behavior"]["conflict_policy"] == "warn"
        finally:
            reset_config()

    def test_malformed_yaml_warns_and_uses_defaults(self, tmp_path):
        use_config(tmp_path, "a: [1, 2\n")
        try:
            assert config_loader.load_config()["behavior"]["conflict_policy"] == "warn"
        finally:
            reset_config()

    def test_strict_mode_env_true(self, tmp_path):
        use_config(tmp_path)
        os.environ["STRICT_MODE"] = "true"
        try:
            assert config_loader.load_config()["behavior"]["strict_mode"] is True
        finally:
            reset_config()

    def test_strict_mode_env_is_case_insensitive_and_exact(self, tmp_path):
        use_config(tmp_path)
        try:
            for raw, expected in [("TRUE", True), ("True", True), ("false", False),
                                  ("1", False), ("yes", False), ("", False)]:
                config_loader._CONFIG = None
                os.environ["STRICT_MODE"] = raw
                assert config_loader.load_config()["behavior"]["strict_mode"] is expected
        finally:
            reset_config()

    def test_strict_mode_env_absent_keeps_default(self, tmp_path):
        use_config(tmp_path)
        try:
            assert config_loader.load_config()["behavior"]["strict_mode"] is False
        finally:
            reset_config()


class TestGet:
    def test_single_key(self, tmp_path):
        use_config(tmp_path)
        try:
            assert config_loader.get("behavior")["conflict_policy"] == "warn"
        finally:
            reset_config()

    def test_nested_keys(self, tmp_path):
        use_config(tmp_path)
        try:
            assert config_loader.get("network", "timeout_seconds") == 15
        finally:
            reset_config()

    def test_missing_returns_default(self, tmp_path):
        use_config(tmp_path)
        try:
            assert config_loader.get("nope") is None
            assert config_loader.get("nope", default=7) == 7
            assert config_loader.get("network", "nope", default="x") == "x"
        finally:
            reset_config()

    def test_traversing_through_scalar_returns_default(self, tmp_path):
        """对非 dict 继续取键不能抛异常。"""
        use_config(tmp_path)
        try:
            assert config_loader.get("network", "timeout_seconds", "deeper", default=9) == 9
        finally:
            reset_config()

    def test_value_zero_is_not_treated_as_missing(self, tmp_path):
        use_config(tmp_path, "behavior:\n  release_keep_days: 0\n")
        try:
            assert config_loader.get("behavior", "release_keep_days", default=99) == 0
        finally:
            reset_config()

    def test_false_is_not_treated_as_missing(self, tmp_path):
        use_config(tmp_path, "behavior:\n  strict_mode: false\n")
        try:
            assert config_loader.get("behavior", "strict_mode", default=True) is False
        finally:
            reset_config()
