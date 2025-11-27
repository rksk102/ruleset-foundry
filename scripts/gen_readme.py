#!/usr/bin/env python3
import os
import math
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "rksk102/singbox-rules")
REF = os.environ.get("INPUT_REF", "main")

ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "rulesets"
TEMPLATE_FILE = ROOT / "README.template.md"
OUTPUT_FILE = ROOT / "README.md"

def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB"]
    i = 0
    value = size / 1024.0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024.0
        i += 1
    # 保留一位小数
    return f"{value:.1f} {units[i]}"

def build_fast_url(path: str) -> str:
    # GhProxy + raw.githubusercontent.com
    return f"https://ghproxy.net/https://raw.githubusercontent.com/{REPO}/{REF}/{path}"

def build_mirror_url(path: str) -> str:
    return f"https://raw.gitmirror.com/{REPO}/{REF}/{path}"

def build_raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{REF}/{path}"

def collect_rules():
    """
    扫描 rulesets/ 下的 .srs 和 .json
    只把 .srs 作为“规则文件”展示，每个 .srs 对应一个 Source JSON 链接（如果存在）
    返回一个列表，每项包含：
      - policy, type, owner, name, rel_path, size, kind
    """
    entries = []

    if not RULES_DIR.exists():
        return entries

    for srs_path in RULES_DIR.rglob("*.srs"):
        rel = srs_path.relative_to(ROOT).as_posix()  # 例如 rulesets/block/domain/Loyalsoldier/reject.srs
        parts = srs_path.relative_to(RULES_DIR).parts  # block/domain/Loyalsoldier/reject.srs

        if len(parts) < 4:
            # 结构不符合预期，跳过
            continue

        policy = parts[0]
        rtype = parts[1]
        owner = parts[2]
        name = os.path.splitext(parts[-1])[0]

        size = srs_path.stat().st_size

        entries.append(
            {
                "policy": policy,
                "type": rtype,
                "owner": owner,
                "name": name,
                "rel_path": rel,
                "size": size,
            }
        )

    # 排序顺序：policy -> type -> owner -> name
    entries.sort(key=lambda x: (x["policy"], x["type"], x["owner"], x["name"]))
    return entries

def render_table(entries):
    if not entries:
        return "_No rule sets found in `rulesets/`_"

    lines = []
    lines.append("| 规则名称 (Name) | 类型 (Type) | 大小 (Size) | 下载通道 (Download) |")
    lines.append("| :-- | :-- | :-- | :-- |")

    for e in entries:
        policy = e["policy"]
        rtype = e["type"]
        owner = e["owner"]
        name = e["name"]
        rel = e["rel_path"]
        size_str = f"`{human_size(e['size'])}`"

        # 显示类型：rule 或 ipcidr 等
        display_type = "RULE"
        if rtype.lower() in ("ip", "ipcidr", "ip-cidr"):
            display_type = "IP-CIDR"

        # 展示路径前缀
        prefix = f"📂 {os.path.dirname(rel)}/<br><strong>{name}</strong>"

        # 链接
        fast_url = build_fast_url(rel)
        mirror_url = build_mirror_url(rel)
        raw_url = build_raw_url(rel)

        # 对应的 JSON（如果存在则给链接，否则给占位符）
        json_rel = rel[:-4] + ".json"  # .srs -> .json
        json_path = ROOT / json_rel
        if json_path.exists():
            json_url = build_raw_url(json_rel)
            source_link = f"[Source]({json_url})"
        else:
            source_link = "`(no json)`"

        fast_btn = (
            f"[![btn]"
            f"(https://img.shields.io/badge/%F0%9F%9A%80_Fast_Download-GhProxy-009688"
            f"?style=flat-square&logo=rocket)]({fast_url})"
        )
        other_links = (
            f"[CDN Mirror]({mirror_url}) • "
            f"[Raw SRS]({raw_url}) • "
            f"{source_link}"
        )

        download_cell = f"{fast_btn}<br><span>{other_links}</span>"

        lines.append(
            f"| {prefix} | {display_type} | {size_str} | {download_cell} |"
        )

    return "\n".join(lines)

def main():
    if not TEMPLATE_FILE.exists():
        raise SystemExit(f"Template not found: {TEMPLATE_FILE}")

    template = TEMPLATE_FILE.read_text(encoding="utf-8")

    entries = collect_rules()
    table_md = render_table(entries)
    total_count = len(entries)

    output = template.replace("{{RULE_TABLE}}", table_md)
    output = output.replace("{{TOTAL_COUNT}}", str(total_count))

    # 如果 README.md 已存在且内容相同，就不写入
    old = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.exists() else ""
    if old == output:
        print("README.md unchanged.")
        return

    OUTPUT_FILE.write_text(output, encoding="utf-8")
    print(f"README.md updated. Total rule sets: {total_count}")

if __name__ == "__main__":
    main()
