import os
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


_CONFIG = None
_CONFIG_FILE = Path("config.yaml")

_TYPES = {
    ("network", "timeout_seconds"): int,
    ("network", "max_retries"): int,
    ("network", "max_source_bytes"): int,
    ("network", "max_concurrency"): int,
    ("network", "max_per_host"): int,
    ("network", "max_retry_after_seconds"): int,
    ("paths", "sources_file"): str,
    ("paths", "rulesets_dir"): str,
    ("paths", "merged_output_dir"): str,
    ("paths", "mrs_output_dir"): str,
    ("paths", "cache_dir"): str,
    ("paths", "log_dir"): str,
    ("behavior", "strict_mode"): bool,
    ("behavior", "release_change_detection"): bool,
    ("behavior", "release_keep_days"): int,
    ("behavior", "conflict_policy"): str,
    ("behavior", "unrecognized_warn_ratio"): float,
    ("behavior", "min_source_success_ratio"): float,
    ("behavior", "allow_partial"): bool,
    ("mihomo", "kernel_cache_path"): str,
    ("mihomo", "repo_api"): str,
    ("mihomo", "pinned_version"): str,
    ("mihomo", "asset_name"): str,
    ("mihomo", "kernel_sha256"): str,
}

_POSITIVE_INTS = {
    ("network", "timeout_seconds"),
    ("network", "max_retries"),
    ("network", "max_source_bytes"),
    ("network", "max_concurrency"),
    ("network", "max_per_host"),
    ("network", "max_retry_after_seconds"),
    ("behavior", "release_keep_days"),
}

_RATIOS = {
    ("behavior", "unrecognized_warn_ratio"),
    ("behavior", "min_source_success_ratio"),
}

_CONFLICT_POLICIES = ("ignore", "warn", "fail")

_PATH_KEYS = {
    ("paths", "sources_file"),
    ("paths", "rulesets_dir"),
    ("paths", "merged_output_dir"),
    ("paths", "mrs_output_dir"),
    ("paths", "cache_dir"),
    ("paths", "log_dir"),
    ("mihomo", "kernel_cache_path"),
}

_MERGE_REQUIRED = ("strategy", "type", "owner", "filename", "inputs")


class ConfigError(Exception):
    pass


def _defaults():
    return {
        "network": {
            "timeout_seconds": 15,
            "max_retries": 2,
            "max_source_bytes": 64 * 1024 * 1024,
            "max_concurrency": 6,
            "max_per_host": 2,
            "max_retry_after_seconds": 60,
        },
        "paths": {
            "sources_file": "sources.urls",
            "rulesets_dir": "rulesets",
            "merged_output_dir": "merged-rules",
            "mrs_output_dir": "merged-rules-mrs",
            "cache_dir": ".cache",
            "log_dir": "logs",
        },
        "merges": [],
        "behavior": {
            "strict_mode": False,
            "release_change_detection": True,
            "release_keep_days": 3,
            "conflict_policy": "warn",
            "unrecognized_warn_ratio": 0.10,
            "min_source_success_ratio": 0.0,
            "allow_partial": False,
        },
        "mihomo": {
            "kernel_cache_path": ".cache/mihomo-kernel",
            "repo_api": "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
            "pinned_version": "",
            "asset_name": "",
            "kernel_sha256": "",
        },
    }


def _merge_dict(base, override):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def _validate_scalar(section, key, value):
    expected = _TYPES.get((section, key))
    if expected is None:
        return
    if isinstance(value, bool) and expected is not bool:
        raise ConfigError(f"{section}.{key} 期望 {expected.__name__}，实际 bool（{value!r}）")
    if expected is float and isinstance(value, int):
        return value
    if not isinstance(value, expected):
        raise ConfigError(
            f"{section}.{key} 期望 {expected.__name__}，实际 {type(value).__name__}（{value!r}）"
        )
    if expected is not None and expected is str and (section, key) in _PATH_KEYS:
        if not value.strip():
            raise ConfigError(f"{section}.{key} 不能为空字符串")
    if (section, key) in _POSITIVE_INTS and value <= 0:
        raise ConfigError(f"{section}.{key} 必须为正整数，实际 {value!r}")
    if (section, key) in _RATIOS and not 0.0 <= value <= 1.0:
        raise ConfigError(f"{section}.{key} 必须在 0..1 之间，实际 {value!r}")
    if (section, key) == ("behavior", "conflict_policy") and value not in _CONFLICT_POLICIES:
        raise ConfigError(
            f"behavior.conflict_policy 取值非法: {value!r}（允许 {'/'.join(_CONFLICT_POLICIES)}）"
        )
    return value


def _validate_merges(merges):
    if merges is None:
        raise ConfigError("merges 不能为 null，需为列表（空列表表示不配置合并任务）")
    if not isinstance(merges, list):
        raise ConfigError(f"merges 期望 list，实际 {type(merges).__name__}")
    for idx, task in enumerate(merges):
        if not isinstance(task, dict):
            raise ConfigError(f"merges[{idx}] 期望 dict，实际 {type(task).__name__}")
        missing = [k for k in _MERGE_REQUIRED if k not in task]
        if missing:
            raise ConfigError(f"merges[{idx}] 缺少必需字段: {', '.join(missing)}")
        if not isinstance(task["inputs"], list) or not task["inputs"]:
            raise ConfigError(f"merges[{idx}].inputs 必须为非空列表")
        for field in ("strategy", "type", "owner", "filename"):
            if not isinstance(task[field], str) or not task[field].strip():
                raise ConfigError(f"merges[{idx}].{field} 必须为非空字符串")


def load_config():
    global _CONFIG, _CONFIG_FILE
    if _CONFIG is not None:
        return _CONFIG

    cfg = _defaults()

    if _CONFIG_FILE.exists():
        if not _HAS_YAML:
            raise ConfigError(
                f"检测到 {_CONFIG_FILE} 但 PyYAML 未安装，拒绝以默认值继续；"
                "请运行 pip install -r requirements.txt"
            )
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                user_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件 {_CONFIG_FILE} YAML 语法错误: {e}") from e
        except OSError as e:
            raise ConfigError(f"配置文件 {_CONFIG_FILE} 读取失败: {e}") from e

        if user_data is None:
            user_data = {}
        if not isinstance(user_data, dict):
            raise ConfigError(
                f"配置文件 {_CONFIG_FILE} 顶层必须是映射，实际 {type(user_data).__name__}"
            )

        _merge_dict(cfg, user_data)

    for section, values in cfg.items():
        if section == "merges" or not isinstance(values, dict):
            continue
        for key, value in values.items():
            _validate_scalar(section, key, value)
    _validate_merges(cfg.get("merges"))

    if os.getenv("STRICT_MODE"):
        raw = os.getenv("STRICT_MODE", "").strip().lower()
        cfg["behavior"]["strict_mode"] = raw in ("true", "1", "yes", "on")

    _CONFIG = cfg
    return _CONFIG


def get(*keys, default=None):
    cfg = load_config()
    for k in keys:
        if isinstance(cfg, dict):
            cfg = cfg.get(k, default)
        else:
            return default
    return cfg if cfg is not None else default
