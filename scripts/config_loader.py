"""
统一配置加载器。
优先级：config.yaml > 环境变量 > 默认值
"""
import os
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


_CONFIG = None
_CONFIG_FILE = Path("config.yaml")


def load_config():
    """加载统一配置，单例模式"""
    global _CONFIG, _CONFIG_FILE
    if _CONFIG is not None:
        return _CONFIG

    if not _CONFIG_FILE.exists():
        # 回退到旧版 merge-config.yaml（兼容过渡期）
        _CONFIG_FILE = Path("merge-config.yaml")

    defaults = {
        "network": {
            "timeout_seconds": 15,
            "max_retries": 2,
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
        },
        "mihomo": {
            "kernel_cache_path": ".cache/mihomo-kernel",
            "repo_api": "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
        },
    }

    if _CONFIG_FILE.exists() and _HAS_YAML:
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                user_data = yaml.safe_load(f) or {}
            _merge_dict(defaults, user_data)
        except Exception:
            pass
    elif _CONFIG_FILE.exists() and not _HAS_YAML:
        # 降级：无 PyYAML 时仅使用 merge-config.yaml 不存在时的默认值
        pass

    # 环境变量优先级最高
    if os.getenv("STRICT_MODE"):
        defaults["behavior"]["strict_mode"] = os.getenv("STRICT_MODE", "").lower() == "true"

    _CONFIG = defaults
    return _CONFIG


def _merge_dict(base, override):
    """深度合并字典，override 覆盖 base"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def get(*keys, default=None):
    """便捷取值：get('network', 'timeout_seconds')"""
    cfg = load_config()
    for k in keys:
        if isinstance(cfg, dict):
            cfg = cfg.get(k, default)
        else:
            return default
    return cfg if cfg is not None else default
