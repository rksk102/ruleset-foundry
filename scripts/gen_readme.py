import os
import re
import sys
import urllib.parse
from io import StringIO
from pathlib import Path

from config_loader import get
from logger import error, group_end, group_start, info, success
from utils import (
    anchor_cwd_to_repo_root,
    atomic_write,
    beijing_now,
    combined_products_hash,
    load_last_hash,
)

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
DIR_RULES = os.path.join(REPO_ROOT, get("paths", "merged_output_dir", default="merged-rules"))
DIR_MRS = os.path.join(REPO_ROOT, get("paths", "mrs_output_dir", default="merged-rules-mrs"))
README_FILE = os.path.join(REPO_ROOT, "README.md")
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "Owner/Repo")
ARTIFACTS_BRANCH = os.getenv("ARTIFACTS_BRANCH", "artifacts")
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{ARTIFACTS_BRANCH}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{ARTIFACTS_BRANCH}"
# jsDelivr 单文件上限，超限返回 403；超出时 Download 列回退为仅 Source
JSDELIVR_MAX_BYTES = 20 * 1024 * 1024
STYLE = "flat-square"


def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    i = 0
    p = float(size_bytes)
    while p >= 1024 and i < len(units) - 1:
        p /= 1024
        i += 1
    return f"{p:.2f} {units[i]}"


BADGE_TIME_RE = re.compile(r"Updated-(\d{4}--\d{2}--\d{2}%20\d{2}%3A\d{2})-blue")


def get_time_badge(encoded_time=None):
    if encoded_time is None:
        encoded_time = urllib.parse.quote(beijing_now().strftime("%Y--%m--%d %H:%M"))
    return f"https://img.shields.io/badge/Updated-{encoded_time}-blue?style={STYLE}&logo=github"


def resolve_badge_time():
    if not get("behavior", "release_change_detection", default=True):
        return None
    try:
        current, _c1, _c2 = combined_products_hash(DIR_RULES, DIR_MRS)
    except Exception:
        return None
    if current != load_last_hash():
        return None
    try:
        with open(README_FILE, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = BADGE_TIME_RE.search(text)
    return m.group(1) if m else None


def scan_files(target_dir, suffix):
    if not os.path.exists(target_dir):
        return []
    files = []
    for root, _, names in os.walk(target_dir):
        for name in names:
            if not name.startswith(".") and name.endswith(suffix):
                files.append(os.path.join(root, name))
    return sorted(files)


def collect_stats(files):
    return len(files), sum(os.path.getsize(path) for path in files)


def write_table_rows(f, files, root_dir):
    for filepath in files:
        filename = os.path.basename(filepath)
        size_bytes = os.path.getsize(filepath)
        filesize = format_size(size_bytes)
        rel_path = os.path.relpath(filepath, root_dir).replace(os.sep, "/")
        root_name = os.path.basename(root_dir)
        category = os.path.dirname(rel_path)
        name_col = (
            f"<sub>{category or 'Root'}</sub><br><b>{filename}</b>"
            "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        )
        full_rel = f"{root_name}/{rel_path}"
        if size_bytes <= JSDELIVR_MAX_BYTES:
            download = (
                f'<a href="{BASE_JSDELIVR}/{full_rel}">'
                f'<img src="https://img.shields.io/badge/Download-34D399?style={STYLE}" '
                'alt="Download"></a>'
            )
        else:
            download = (
                '<img src="https://img.shields.io/badge/over20MB-Source_only-lightgrey'
                f'?style={STYLE}" alt="超出 jsDelivr 20MB 上限">'
            )
        source = (
            f'<a href="{BASE_RAW}/{full_rel}">'
            f'<img src="https://img.shields.io/badge/Source-181717?style={STYLE}&logo=github" '
            'alt="Source"></a>'
        )
        f.write(f"| {name_col} | `{filesize}` | {download} | {source} |\n")


def make_section(f, title, desc, files, root_dir):
    count, total_size = collect_stats(files)
    f.write(f"### {title}\n\n*{desc}*\n\n")
    f.write(
        "<details open>\n"
        f"<summary><b>{count} 个文件</b> | 总大小 <b>{format_size(total_size)}</b></summary>\n\n"
    )
    f.write("| 文件名称 | 大小 | 下载 | 源文件 |\n")
    f.write("| :--- | :--- | :--- | :--- |\n")
    write_table_rows(f, files, root_dir)
    f.write("\n</details>\n\n")
    return count, total_size


def make_page_header(badge_time=None):
    repo_short = REPO_NAME.split("/")[-1]
    return f"""<div align="center">

<h1>{repo_short}</h1>

<p>
  <a href="https://github.com/{REPO_NAME}/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/{REPO_NAME}/pipeline.yml?style={STYLE}&label=Build&color=2ea44f" alt="Build">
  </a>
  <a href="https://github.com/{REPO_NAME}">
    <img src="https://img.shields.io/github/repo-size/{REPO_NAME}?style={STYLE}&label=Size&color=orange" alt="Size">
  </a>
  <a href="#">
    <img src="{get_time_badge(badge_time)}" alt="Updated">
  </a>
</p>

<p><strong>自动生成</strong> &middot; <strong>每日同步</strong></p>

</div>

---

"""


def make_static_sections():
    return r"""
## 使用方式

`.txt` 是 mihomo 文本规则集，`.mrs` 是对应的二进制规则集。引用时必须声明正确的
`behavior` 和 `format`，否则规则可能被静默解析为空。
`.txt` 使用 `format: text`，`.mrs` 使用 `format: mrs`。
domain 产物使用 `behavior: domain`，IP 产物使用 `behavior: ipcidr`。

| 产物 | `behavior` | `format` |
| :--- | :--- | :--- |
| `merged-rules/<策略>/domain/**/*.txt` | `domain` | `text` |
| `merged-rules/<策略>/ipcidr/**/*.txt` | `ipcidr` | `text` |
| `merged-rules-mrs/**/*.mrs` | 与同名 `.txt` 相同 | `mrs` |

```yaml
rule-providers:
  block:
    type: http
    behavior: domain
    format: text
    url: "https://raw.githubusercontent.com/rksk102/mihomo-rule-library/artifacts/merged-rules/block/domain/Loyalsoldier/reject.txt"
    path: ./ruleset/block.txt
    interval: 86400
```

下载表中的 `Download` 是 jsDelivr 链接，`Source` 是 GitHub raw 链接。产物发布在
`artifacts` 分支，不进入 `main` 的历史。

## 规则语义

策略顺序固定为 `block > direct > policy`。`domain` 规则中的写法含义如下：

| 写法 | `example.com` | `www.example.com` | `a.b.example.com` |
| :--- | :---: | :---: | :---: |
| `+.example.com` | ✅ | ✅ | ✅ |
| `.example.com` | ❌ | ✅ | ✅ |
| `example.com` | ✅ | ❌ | ❌ |
| `*.example.com` | ❌ | ✅ | ❌ |
| `*.*.example.com` | ❌ | ❌ | ✅ |

`sources.urls` 可用 `[policy:...]`、`[type:...]` 和 `[domain-kind:exact|suffix]`
标记来源语义；未显式标记的裸域名按精确匹配处理。

## 配置与维护

`config.yaml` 是运行参数来源，`sources.urls` 是上游来源清单（仅接受 `https://`）。
常用命令：

```bash
python scripts/gen_readme.py
python -m pytest tests/ -q --cov=scripts --cov-branch --cov-fail-under=70
ruff check scripts tests
```

`behavior.*` 各键语义：

| 键 | 含义 |
| :--- | :--- |
| `release_change_detection` | 产物哈希与上次发布一致时跳过发布 |
| `release_keep_days` | 旧 Release 保留天数；上限 25（清理每次最多读 50 条 release，每日 2 轮） |
| `conflict_policy` | 跨策略冲突处理：`ignore` / `warn` / `fail` |
| `unrecognized_warn_ratio` | 单输出组未识别行占比超此值记解析错误（疑似上游格式变更） |
| `min_source_success_ratio` | 输出组成功率门禁；`0.0` = 禁用（当前由 `allow_partial` 承担 fail-closed） |
| `allow_partial` | `false` = 任一源下载/解析失败即整轮不发布，保留上一版产物 |

内核由 `mihomo.pinned_version` 和两个架构哈希钉扎；`kernel-bump.yml` 自动更新，
提交前会验证 amd64/arm64 内核。详细信任模型见 [SECURITY.md](SECURITY.md)。

## 安全与许可

脚本和工作流使用 MIT 许可；规则内容来自上游仓库，使用前请自行确认其许可和准确性。
漏洞请通过 GitHub Security 的私密报告入口提交，详见 [SECURITY.md](SECURITY.md)。

`README.md` 由 `scripts/gen_readme.py` 生成，修改说明文字请改 `make_static_sections()`。
"""


def main():
    group_start("生成 README")
    files_std = scan_files(DIR_RULES, ".txt")
    files_mrs = scan_files(DIR_MRS, ".mrs")
    info(f"  标准规则文件: {len(files_std)}")
    info(f"  MRS 规则文件: {len(files_mrs)}")
    badge_time = resolve_badge_time()

    try:
        buf = StringIO()
        buf.write(make_page_header(badge_time))
        buf.write("## 规则列表\n\n")
        count_std, size_std = make_section(
            buf, "基础规则集合", "mihomo 文本规则集", files_std, DIR_RULES)
        count_mrs, size_mrs = make_section(
            buf, "Mihomo 专用集合", "mihomo 二进制规则集", files_mrs, DIR_MRS)
        buf.write(make_static_sections())
        atomic_write(README_FILE, buf.getvalue())
    except Exception as e:
        error(f"README 生成失败: {e}")
        sys.exit(1)

    group_end()
    success(f"README.md 已更新 (标准: {count_std}, MRS: {count_mrs})")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### README 生成报告\n\n")
            f.write("| 类型 | 文件数 | 大小 |\n| :--- | :---: | :---: |\n")
            f.write(f"| 标准规则 | **{count_std}** | {format_size(size_std)} |\n")
            f.write(f"| MRS 规则 | **{count_mrs}** | {format_size(size_mrs)} |\n")
            f.write(f"| **总计** | **{count_std + count_mrs}** | "
                    f"**{format_size(size_std + size_mrs)}** |\n")


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
