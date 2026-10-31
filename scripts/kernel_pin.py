import argparse
import copy
import os
import re
import sys
from pathlib import Path

import yaml
from config_loader import ConfigError, load_config
from convert_mrs import _set_config_field
from logger import gh_error
from utils import is_safe_component

_PIN_KEYS = ("pinned_version", "kernel_sha256", "kernel_sha256_arm64")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_metadata(tag, kernel_sha256, kernel_sha256_arm64):
    if not is_safe_component(tag):
        raise ValueError(f"非法内核 tag: {tag!r}")
    values = {
        "pinned_version": tag,
        "kernel_sha256": kernel_sha256,
        "kernel_sha256_arm64": kernel_sha256_arm64,
    }
    for key in _PIN_KEYS[1:]:
        if not _SHA256_RE.fullmatch(values[key]):
            raise ValueError(f"{key} 必须为 64 位十六进制字符串")
    return values


def _read_yaml(path):
    data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(data, dict) or not isinstance(data.get("mihomo"), dict):
        raise ValueError(f"配置缺少 mihomo 映射: {path}")
    return data


def read_metadata(path="config.yaml"):
    mihomo = _read_yaml(path)["mihomo"]
    return validate_metadata(*(mihomo.get(key) for key in _PIN_KEYS))


def apply_pin(path, tag, kernel_sha256, kernel_sha256_arm64):
    metadata = validate_metadata(tag, kernel_sha256, kernel_sha256_arm64)
    path = Path(path)
    original_text = path.read_text(encoding="utf-8")
    original = _read_yaml(path)
    updated_text = original_text
    for key in _PIN_KEYS:
        updated_text = _set_config_field(updated_text, key, metadata[key])

    updated = yaml.load(updated_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    expected = copy.deepcopy(original)
    expected["mihomo"].update(metadata)
    if updated != expected:
        raise ValueError("内核钉扎更新产生了三字段以外的配置变化")
    path.write_text(updated_text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="传递或重建 Mihomo 内核钉扎元数据")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--kernel-sha256")
    parser.add_argument("--kernel-sha256-arm64")
    args = parser.parse_args(argv)

    try:
        if args.emit:
            load_config()
            metadata = read_metadata()
            output = os.environ.get("GITHUB_OUTPUT")
            if not output:
                raise ValueError("--emit 需要 GITHUB_OUTPUT")
            with open(output, "a", encoding="utf-8", newline="\n") as f:
                for key, value in metadata.items():
                    output_key = "tag" if key == "pinned_version" else key
                    f.write(f"{output_key}={value}\n")
            return

        if not all((args.tag, args.kernel_sha256, args.kernel_sha256_arm64)):
            raise ValueError("--apply 需要 tag 与双架构 SHA-256")
        apply_pin("config.yaml", args.tag, args.kernel_sha256, args.kernel_sha256_arm64)
        load_config()
    except (ConfigError, OSError, ValueError, yaml.YAMLError) as e:
        gh_error(f"内核钉扎元数据处理失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
