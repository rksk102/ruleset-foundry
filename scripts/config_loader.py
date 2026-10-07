from pathlib import Path

from utils import is_safe_component

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


_CONFIG = None
_CONFIG_FILE = Path("config.yaml")
_REPO_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yaml"


def resolve_config_file():
    """默认的 config.yaml 优先取运行目录，缺失时回落到仓库根。"""
    if _CONFIG_FILE.exists():
        return _CONFIG_FILE
    if str(_CONFIG_FILE) == "config.yaml" and _REPO_CONFIG_FILE.exists():
        return _REPO_CONFIG_FILE
    return _CONFIG_FILE

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
    ("paths", "log_dir"): str,
    ("behavior", "release_change_detection"): bool,
    ("behavior", "release_keep_days"): int,
    ("behavior", "conflict_policy"): str,
    ("behavior", "unrecognized_warn_ratio"): float,
    ("behavior", "min_source_success_ratio"): float,
    ("behavior", "allow_partial"): bool,
    ("mihomo", "kernel_cache_path"): str,
    ("mihomo", "repo_api"): str,
    ("mihomo", "pinned_version"): str,
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

CONFLICT_POLICIES = ("ignore", "warn", "fail")

_PATH_KEYS = {
    ("paths", "sources_file"),
    ("paths", "rulesets_dir"),
    ("paths", "merged_output_dir"),
    ("paths", "mrs_output_dir"),
    ("paths", "log_dir"),
    ("mihomo", "kernel_cache_path"),
}

_MERGE_REQUIRED = ("strategy", "type", "owner", "filename", "inputs")

_KNOWN_SECTIONS = ("network", "paths", "merges", "behavior", "mihomo")


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
            "log_dir": "logs",
        },
        "merges": [],
        "behavior": {
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
            "kernel_sha256": "",
        },
    }


def _merge_dict(base, override):
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


_PATH_FORBIDDEN_NAMES = {"", ".", "..", "./", "../", "/", "\\"}


def _validate_path_value(label, value):
    text = value.strip()
    if not text:
        raise ConfigError(f"{label} 不能为空字符串")
    if "\0" in value:
        raise ConfigError(f"{label} 不能包含 NUL 字符")
    if text.startswith(("/", "\\")):
        raise ConfigError(f"{label} 必须是相对路径，不能是绝对路径（{value!r}）")
    if len(text) > 1 and text[1] == ":":
        raise ConfigError(f"{label} 不能是盘符路径（{value!r}）")
    normalized = text.replace("\\", "/").strip("/")
    if normalized in _PATH_FORBIDDEN_NAMES or ".." in normalized.split("/"):
        raise ConfigError(
            f"{label} 必须是指向仓库内的子目录/文件，不能是 {value!r}"
            "（'.'、'..'、绝对路径与盘符会让他处的清理逻辑波及仓库或系统）"
        )
    return value


def _validate_scalar(section, key, value):
    expected = _TYPES.get((section, key))
    if expected is None:
        return
    if isinstance(value, bool) and expected is not bool:
        raise ConfigError(f"{section}.{key} 期望 {expected.__name__}，实际 bool（{value!r}）")
    if expected is float and isinstance(value, int):
        try:
            value = float(value)
        except OverflowError as e:
            raise ConfigError(f"{section}.{key} 数值超出 float 范围（{value!r}）") from e
    if not isinstance(value, expected):
        raise ConfigError(
            f"{section}.{key} 期望 {expected.__name__}，实际 {type(value).__name__}（{value!r}）"
        )
    if expected is not None and expected is str and (section, key) in _PATH_KEYS:
        _validate_path_value(f"{section}.{key}", value)
    if (section, key) in _POSITIVE_INTS and value <= 0:
        raise ConfigError(f"{section}.{key} 必须为正整数，实际 {value!r}")
    if (section, key) in _RATIOS and not 0.0 <= value <= 1.0:
        raise ConfigError(f"{section}.{key} 必须在 0..1 之间，实际 {value!r}")
    if (section, key) == ("behavior", "conflict_policy") and value not in CONFLICT_POLICIES:
        raise ConfigError(
            f"behavior.conflict_policy 取值非法: {value!r}（允许 {'/'.join(CONFLICT_POLICIES)}）"
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
            if not is_safe_component(task[field]):
                raise ConfigError(
                    f"merges[{idx}].{field} 必须是以字母/数字开头的单段名字"
                    f"（允许 [A-Za-z0-9._-]，不得含 '..'、路径分隔符或空白）: {task[field]!r}"
                )
        for pos, rel in enumerate(task["inputs"]):
            if not isinstance(rel, str) or not rel.strip():
                raise ConfigError(f"merges[{idx}].inputs[{pos}] 必须为非空字符串")
            _validate_path_value(f"merges[{idx}].inputs[{pos}]", rel)


def load_config():
    global _CONFIG, _CONFIG_FILE
    if _CONFIG is not None:
        return _CONFIG

    cfg = _defaults()
    config_file = resolve_config_file()

    if config_file.exists():
        if not _HAS_YAML:
            raise ConfigError(
                f"检测到 {config_file} 但 PyYAML 未安装，拒绝以默认值继续；"
                "请运行 pip install -r requirements.txt"
            )
        try:
            with open(config_file, encoding="utf-8") as f:
                user_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件 {config_file} YAML 语法错误: {e}") from e
        except OSError as e:
            raise ConfigError(f"配置文件 {config_file} 读取失败: {e}") from e

        if user_data is None:
            user_data = {}
        if not isinstance(user_data, dict):
            raise ConfigError(
                f"配置文件 {config_file} 顶层必须是映射，实际 {type(user_data).__name__}"
            )

        _merge_dict(cfg, user_data)

    for section, values in cfg.items():
        if section not in _KNOWN_SECTIONS:
            raise ConfigError(f"未知配置节: {section}（请检查拼写）")
        if section == "merges":
            continue
        if not isinstance(values, dict):
            raise ConfigError(
                f"配置节 {section!r} 必须是映射，实际 {type(values).__name__}"
            )
        for key, value in values.items():
            if (section, key) not in _TYPES:
                raise ConfigError(f"未知配置项: {section}.{key}（请检查拼写）")
            values[key] = _validate_scalar(section, key, value)
    _validate_merges(cfg.get("merges"))

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
