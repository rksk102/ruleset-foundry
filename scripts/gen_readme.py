import argparse
import html
import os
import re
import sys
import urllib.parse
from io import StringIO
from pathlib import Path

from config_loader import ConfigError, get
from logger import error, gh_error, group_end, group_start, info, success
from utils import (
    anchor_cwd_to_repo_root,
    atomic_write,
    beijing_now,
    combined_products_hash,
    is_safe_component,
    load_last_hash,
)

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
try:
    DIR_RULES = os.path.join(REPO_ROOT, get("paths", "merged_output_dir", default="merged-rules"))
    DIR_MRS = os.path.join(REPO_ROOT, get("paths", "mrs_output_dir", default="merged-rules-mrs"))
except ConfigError as _cfg_err:
    gh_error(f"配置校验失败: {_cfg_err}")
    sys.exit(1)
README_FILE = os.path.join(REPO_ROOT, "README.md")
DEFAULT_REPO_NAME = "rksk102/ruleset-foundry"
DEFAULT_ARTIFACTS_BRANCH = "artifacts"
REPO_NAME = os.getenv("GITHUB_REPOSITORY") or DEFAULT_REPO_NAME
ARTIFACTS_BRANCH = os.getenv("ARTIFACTS_BRANCH") or DEFAULT_ARTIFACTS_BRANCH
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{ARTIFACTS_BRANCH}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{ARTIFACTS_BRANCH}"
# jsDelivr 单文件上限，超限返回 403；超出时 Download 列回退为仅 Source
JSDELIVR_MAX_BYTES = 20 * 1024 * 1024
STYLE = "flat-square"

_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _refresh_env():
    global REPO_NAME, ARTIFACTS_BRANCH, BASE_RAW, BASE_JSDELIVR
    REPO_NAME = os.getenv("GITHUB_REPOSITORY") or DEFAULT_REPO_NAME
    ARTIFACTS_BRANCH = os.getenv("ARTIFACTS_BRANCH") or DEFAULT_ARTIFACTS_BRANCH
    BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{ARTIFACTS_BRANCH}"
    BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{ARTIFACTS_BRANCH}"


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


BADGE_LABEL = urllib.parse.quote("产物生成")
BADGE_TIME_RE = re.compile(rf"{BADGE_LABEL}-(\d{{4}}--\d{{2}}--\d{{2}}%20\d{{2}}%3A\d{{2}})-blue")
# 兼容旧版 Updated 徽章格式，仅用于从上一版 README 继承时间
_LEGACY_BADGE_TIME_RE = re.compile(r"Updated-(\d{4}--\d{2}--\d{2}%20\d{2}%3A\d{2})-blue")


def get_time_badge(encoded_time=None):
    if encoded_time is None:
        encoded_time = urllib.parse.quote(beijing_now().strftime("%Y--%m--%d %H:%M"))
    return f"https://img.shields.io/badge/{BADGE_LABEL}-{encoded_time}-blue?style={STYLE}&logo=github"


def resolve_badge_time():
    if os.getenv("FORCE_RELEASE", "").lower() in ("1", "true", "yes"):
        return None
    if not get("behavior", "release_change_detection", default=True):
        return None
    try:
        current, _c1, _c2 = combined_products_hash(DIR_RULES, DIR_MRS)
        if current != load_last_hash():
            return None
    except Exception:
        return None
    try:
        with open(README_FILE, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = BADGE_TIME_RE.search(text) or _LEGACY_BADGE_TIME_RE.search(text)
    return m.group(1) if m else None


def scan_files(target_dir, suffix):
    if not os.path.exists(target_dir):
        return []
    base = os.path.abspath(target_dir)
    files = []
    for root, dirs, names in os.walk(target_dir, onerror=_raise_walk_error):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if not name.startswith(".") and name.endswith(suffix):
                files.append(os.path.join(root, name))
    # 按 POSIX 相对路径排序，保证 Windows/Linux 生成顺序一致
    return sorted(files, key=lambda p: os.path.relpath(p, base).replace(os.sep, "/"))


def _raise_walk_error(err):
    raise err


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
            f"<sub>{html.escape(category) if category else 'Root'}</sub>"
            f"<br><b>{html.escape(filename)}</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        )
        # URL path 编码：& 等会破坏链接；保留 "/" 与现有合法字符（如 category-ai-!cn.txt 的 !）
        full_rel = urllib.parse.quote(f"{root_name}/{rel_path}", safe="/!~-._")
        alt_name = html.escape(filename, quote=True)
        if size_bytes <= JSDELIVR_MAX_BYTES:
            download = (
                f'<a href="{BASE_JSDELIVR}/{full_rel}">'
                f'<img src="https://img.shields.io/badge/Download-34D399?style={STYLE}" '
                f'alt="Download {alt_name}"></a>'
            )
        else:
            download = (
                '<img src="https://img.shields.io/badge/over20MB-Source_only-lightgrey'
                f'?style={STYLE}" alt="{alt_name} 超出 jsDelivr 20MB 上限">'
            )
        source = (
            f'<a href="{BASE_RAW}/{full_rel}">'
            f'<img src="https://img.shields.io/badge/Source-181717?style={STYLE}&logo=github" '
            f'alt="GitHub raw {alt_name}"></a>'
        )
        f.write(f"| {name_col} | `{filesize}` | {download} | {source} |\n")


def make_section(f, title, desc, files, root_dir):
    count, total_size = collect_stats(files)
    f.write(f"### {title}\n\n*{desc}*\n\n")
    f.write(f"<details>\n<summary><b>{count} 个文件</b> | 总大小 <b>{format_size(total_size)}</b></summary>\n\n")
    f.write("| 文件名称 | 大小 | 下载 | GitHub raw |\n")
    f.write("| :--- | :--- | :--- | :--- |\n")
    write_table_rows(f, files, root_dir)
    f.write("\n</details>\n\n")
    return count, total_size


def make_page_header(badge_time=None):
    _refresh_env()
    repo_short = REPO_NAME.split("/")[-1]
    return f"""<div align="center">

<h1>{repo_short}</h1>

<p><strong>从上游开源规则自动合并、去重并构建为 mihomo 规则集（.txt / .mrs）</strong></p>

<p><sub>每日两次自动构建（北京时间清晨与傍晚，具体时间以 Actions 运行记录为准） · 按 <code>block &gt; direct &gt; policy</code> 策略分组</sub></p>

<p>
  <a href="https://github.com/{REPO_NAME}/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/{REPO_NAME}/pipeline.yml?style={STYLE}&label=Build" alt="Build status">
  </a>
  <a href="https://github.com/{REPO_NAME}/releases">
    <img src="{get_time_badge(badge_time)}" alt="产物生成时间">
  </a>
</p>

</div>

"""


def make_intro(repo_name=None, branch=None):
    repo_name = repo_name or os.getenv("GITHUB_REPOSITORY") or DEFAULT_REPO_NAME
    branch = branch or os.getenv("ARTIFACTS_BRANCH") or DEFAULT_ARTIFACTS_BRANCH
    return f"""## 快速开始

推荐使用 `.mrs` 二进制规则集（加载快、内存占用低）；`.txt` 文本规则集同样提供，见下方产物清单。
把以下内容并入 mihomo 配置即可使用（规则书写顺序即匹配优先级 `block > direct > policy`）：

<details>
<summary><b>完整配置示例</b>（点击展开）</summary>

```yaml
rule-providers:
  block:
    type: http
    behavior: domain
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/{repo_name}@{branch}/merged-rules-mrs/block/domain/rksk102/all-adblock.mrs"
    path: ./ruleset/block.mrs
    interval: 86400
  direct:
    type: http
    behavior: ipcidr
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/{repo_name}@{branch}/merged-rules-mrs/direct/ipcidr/rksk102/all-cnip.mrs"
    path: ./ruleset/direct.mrs
    interval: 86400
  policy:
    type: http
    behavior: domain
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/{repo_name}@{branch}/merged-rules-mrs/policy/domain/rksk102/all-proxy.mrs"
    path: ./ruleset/policy.mrs
    interval: 86400

rules:
  - RULE-SET,block,REJECT
  - RULE-SET,direct,DIRECT
  - RULE-SET,policy,PROXY
```

</details>

> [!NOTE]
> 以上链接走 jsDelivr CDN，内容更新最长滞后 **12 小时**。需要最新内容时，把 URL 中的
> `cdn.jsdelivr.net/gh/{repo_name}@{branch}` 换成
> `raw.githubusercontent.com/{repo_name}/{branch}`（约 5 分钟缓存）。

"""


def make_inventory(files_std, files_mrs):
    _refresh_env()
    count_std, size_std = collect_stats(files_std)
    count_mrs, size_mrs = collect_stats(files_mrs)
    buf = StringIO()
    buf.write("## 产物清单\n\n")
    buf.write(
        f"共 **{count_std + count_mrs}** 个产物文件，合计 **{format_size(size_std + size_mrs)}**"
        f"（.txt {format_size(size_std)} + .mrs {format_size(size_mrs)}）。"
        "引用时必须声明正确的 `behavior` 和 `format`，否则规则可能被静默解析为空："
        "`.txt` 使用 `format: text`，`.mrs` 使用 `format: mrs`；"
        "domain 产物使用 `behavior: domain`，IP 产物使用 `behavior: ipcidr`。\n\n"
    )
    buf.write("| 产物 | `behavior` | `format` |\n")
    buf.write("| :--- | :--- | :--- |\n")
    buf.write("| `merged-rules/<策略>/domain/**/*.txt` | `domain` | `text` |\n")
    buf.write("| `merged-rules/<策略>/ipcidr/**/*.txt` | `ipcidr` | `text` |\n")
    buf.write("| `merged-rules-mrs/**/*.mrs` | 与同名 `.txt` 相同 | `mrs` |\n\n")
    make_section(buf, "基础规则集合（.txt）", "mihomo 文本规则集", files_std, DIR_RULES)
    make_section(buf, "Mihomo 专用集合（.mrs）", "mihomo 二进制规则集", files_mrs, DIR_MRS)
    return buf.getvalue()


def make_static_sections(repo_name=None, branch=None):
    repo_name = repo_name or os.getenv("GITHUB_REPOSITORY") or DEFAULT_REPO_NAME
    branch = branch or os.getenv("ARTIFACTS_BRANCH") or DEFAULT_ARTIFACTS_BRANCH
    keep_days = get("behavior", "release_keep_days", default=3)
    return f"""
<details>
<summary><b>规则语义：domain 写法对照</b>（点击展开）</summary>

策略顺序固定为 `block > direct > policy`。`domain` 规则中的写法含义如下：

| 写法 | `example.com` | `www.example.com` | `a.b.example.com` |
| :--- | :---: | :---: | :---: |
| `+.example.com` | ✅ | ✅ | ✅ |
| `.example.com` | ❌ | ✅ | ✅ |
| `example.com` | ✅ | ❌ | ❌ |
| `*.example.com` | ❌ | ✅ | ❌ |
| `*.*.example.com` | ❌ | ❌ | ✅ |

</details>

## 版本与完整性

> [!WARNING]
> 产物每次以孤立提交**强制推送**到 `{branch}` 分支，历史会被重写，**不适合固定 commit**。
> 需要固定版本或整包下载请使用 [Releases](https://github.com/{repo_name}/releases)
> （含 `merged-rules-*.zip` 与 `SHA256SUMS`；校验前先解压 zip，再执行 `sha256sum -c SHA256SUMS`）。

Releases 仅适合**短期固定**：默认只保留最近 {keep_days} 天，过期的 Release 与 tag 会在**后续成功发布**
的收尾阶段清理（不是独立的每日定时任务）；需要长期使用时请跟随
[`latest`](https://github.com/{repo_name}/releases/latest) 或自行镜像产物。

## 安全与许可

脚本与工作流以 MIT 许可发布；规则内容来自上游项目，使用前请自行确认其许可与准确性。
漏洞请通过 [SECURITY.md](SECURITY.md) 的私密入口报告。

---

<sub>本文件由 `scripts/gen_readme.py` 自动生成，请勿直接编辑。</sub>
"""


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成 README.md")
    parser.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="允许 GITHUB_REPOSITORY 未设置（回退到默认仓库名，仅用于本地预览）",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="允许产物目录缺失或为空（将生成 0 文件表，仅用于本地预览）",
    )
    return parser.parse_args(argv)


def _preflight(args):
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        if not _REPO_NAME_RE.fullmatch(repo) or ".." in repo:
            error(f"GITHUB_REPOSITORY 格式非法: {repo!r}（应为 <owner>/<repo>）")
            sys.exit(1)
    elif not args.allow_placeholder:
        error("GITHUB_REPOSITORY 未设置，拒绝写入默认仓库链接（本地预览请加 --allow-placeholder）")
        sys.exit(1)
    branch = os.getenv("ARTIFACTS_BRANCH") or DEFAULT_ARTIFACTS_BRANCH
    if not is_safe_component(branch):
        error(f"ARTIFACTS_BRANCH 不是安全的分支名: {branch!r}")
        sys.exit(1)
    if not args.allow_empty:
        for target, suffix in ((DIR_RULES, ".txt"), (DIR_MRS, ".mrs")):
            if not scan_files(target, suffix):
                error(f"产物目录缺失或为空: {target}，拒绝生成空表（本地预览请加 --allow-empty）")
                sys.exit(1)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _refresh_env()
    group_start("生成 README")
    _preflight(args)
    files_std = scan_files(DIR_RULES, ".txt")
    files_mrs = scan_files(DIR_MRS, ".mrs")
    info(f"  标准规则文件: {len(files_std)}")
    info(f"  MRS 规则文件: {len(files_mrs)}")
    badge_time = resolve_badge_time()

    try:
        buf = StringIO()
        buf.write(make_page_header(badge_time))
        buf.write(make_intro())
        buf.write(make_inventory(files_std, files_mrs))
        buf.write(make_static_sections())
        atomic_write(README_FILE, buf.getvalue())
    except Exception as e:
        error(f"README 生成失败: {e}")
        sys.exit(1)

    group_end()
    count_std, size_std = collect_stats(files_std)
    count_mrs, size_mrs = collect_stats(files_mrs)
    success(f"README.md 已更新 (标准: {count_std}, MRS: {count_mrs})")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### README 生成报告\n\n")
            f.write("| 类型 | 文件数 | 大小 |\n| :--- | :---: | :---: |\n")
            f.write(f"| 标准规则 | **{count_std}** | {format_size(size_std)} |\n")
            f.write(f"| MRS 规则 | **{count_mrs}** | {format_size(size_mrs)} |\n")
            f.write(f"| **总计** | **{count_std + count_mrs}** | **{format_size(size_std + size_mrs)}** |\n")


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
