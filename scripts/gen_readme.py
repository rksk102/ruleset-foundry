import os
import sys
import urllib.parse

from logger import error, get_logger, group_end, group_start, info, success
from utils import beijing_now

logger = get_logger()

REPO_ROOT = os.getcwd()
DIR_RULES = os.path.join(REPO_ROOT, "merged-rules")
DIR_MRS = os.path.join(REPO_ROOT, "merged-rules-mrs")
README_FILE = os.path.join(REPO_ROOT, "README.md")
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "Owner/Repo")
BRANCH_NAME = os.getenv("GITHUB_REF_NAME", "main")
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH_NAME}"
BASE_GHPROXY = f"https://ghproxy.net/{BASE_RAW}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{BRANCH_NAME}"
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


def get_time_badge():
    now = beijing_now().strftime("%Y--%m--%d %H:%M")
    enc_now = urllib.parse.quote(now)
    return f"https://img.shields.io/badge/Updated-{enc_now}-blue?style={STYLE}&logo=github"


def scan_files(target_dir):
    files_list = []
    if not os.path.exists(target_dir):
        return []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if not file.startswith("."):
                files_list.append(os.path.join(root, file))
    return sorted(files_list)


def collect_stats(files):
    total_size = sum(os.path.getsize(fp) for fp in files)
    return len(files), total_size


def write_table_rows(f, files, root_dir):
    for filepath in files:
        filename = os.path.basename(filepath)
        filesize = format_size(os.path.getsize(filepath))
        rel_path = os.path.relpath(filepath, root_dir)
        url_path = rel_path.replace(os.sep, "/")
        root_name = os.path.basename(root_dir)
        category = os.path.dirname(url_path)

        if category:
            name_col = f"<sub>{category}</sub><br><b>{filename}</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        else:
            name_col = f"<sub>Root</sub><br><b>{filename}</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"

        full_rel = f"{root_name}/{url_path}"
        cdn = (
            f'<a href="{BASE_GHPROXY}/{full_rel}">'
            f'<img src="https://img.shields.io/badge/GhProxy-009688?style={STYLE}&logo=rocket" alt="GhProxy"></a> '
            f'<a href="{BASE_JSDELIVR}/{full_rel}">'
            f'<img src="https://img.shields.io/badge/jsDelivr-E34F26?style={STYLE}&logo=jsdelivr" alt="jsDelivr"></a>'
        )
        src = (
            f'<a href="{BASE_RAW}/{full_rel}">'
            f'<img src="https://img.shields.io/badge/Source-181717?style={STYLE}&logo=github" alt="Source"></a>'
        )
        f.write(f"| {name_col} | `{filesize}` | {cdn} | {src} |\n")


def make_section(f, title, desc, files, root_dir):
    count, total_size = collect_stats(files)

    f.write(f"### {title}\n\n")
    f.write(f"*{desc}*\n\n")
    f.write(
        f"<details open>\n"
        f"<summary><b>{count} 个文件</b> | "
        f"总大小 <b>{format_size(total_size)}</b> | "
        f"点击折叠 / 展开</summary>\n\n"
    )

    f.write("| 文件名称 | 大小 | CDN 下载 | 源文件 |\n")
    f.write("| :--- | :--- | :--- | :--- |\n")
    write_table_rows(f, files, root_dir)

    f.write("\n</details>\n\n")
    return count, total_size


def make_page_header():
    repo_short = REPO_NAME.split("/")[-1]
    time_badge = get_time_badge()

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
    <img src="{time_badge}" alt="Updated">
  </a>
</p>

<p>
  <strong>全自动构建</strong> &middot; <strong>全球 CDN 加速</strong> &middot; <strong>每日同步更新</strong>
</p>

</div>

---

"""


def make_static_sections():
    """生成与产物无关的静态运维说明。

    必须由生成器输出：README.md 每次都被整体重写，手写追加的尾部会被覆盖。
    """
    return """
## 内核版本升级流程（维护者）

1. 运行 `python scripts/convert_mrs.py --print-kernel-hash`（会下载并打印解压后二进制 sha256）。
2. 在 mihomo 官方 Release 页面核对 `pinned_version` 与资产名。
3. 更新 `config.yaml` 的 `mihomo.pinned_version` / `asset_name` / `kernel_sha256` 三字段。
4. 提 PR，由 CI（pytest + ruff）验证后合并。

## 规则优先级与消费方式

策略优先级固定为 `block > direct > policy`；在代理客户端中按此顺序引用 rule-provider。
仓库提供 `.txt`（通用）与 `.mrs`（Mihomo 专用）两种格式，路径一一对应。

## 发布去重语义

仅当规则**正文**（忽略 `# Date:` 等元数据）发生变化时才会新建 Release；
内容未变化时跳过发布，但产物与 README 仍会提交更新。

## 跨策略冲突处理（conflict_policy）

`behavior.conflict_policy` 支持 `ignore | warn | fail`，默认 `warn`。
`fail` 仅作为"新增源时的临时验收开关"：当前隐式冲突基线噪声较大（约 1.2 万条），
直接启用 `fail` 会中断发布；启用前请先人工核对冲突检测结果。

"""


def main():
    group_start("生成 README")

    files_std = scan_files(DIR_RULES)
    files_mrs = scan_files(DIR_MRS)

    info(f"  标准规则文件: {len(files_std)}")
    info(f"  MRS 规则文件: {len(files_mrs)}")

    try:
        with open(README_FILE, "w", encoding="utf-8") as f:
            f.write(make_page_header())

            f.write("## 规则列表\n\n")

            count_std, size_std = make_section(
                f, "基础规则集合",
                "适用于 Clash Premium / Clash Verge / Sing-box 等通用格式 (.txt)",
                files_std, DIR_RULES,
            )

            count_mrs, size_mrs = make_section(
                f, "Mihomo 专用集合",
                "仅适用于 Mihomo (Clash.Meta) 内核，二进制格式 (.mrs) 性能更好、加载更快",
                files_mrs, DIR_MRS,
            )

            f.write(make_static_sections())

    except Exception as e:
        error(f"README 生成失败: {e}")
        sys.exit(1)

    group_end()
    success(f"README.md 已更新 (标准: {count_std}, MRS: {count_mrs}, 总计: {count_std + count_mrs})")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### README 生成报告\n\n")
            f.write("| 类型 | 文件数 | 大小 |\n| :--- | :---: | :---: |\n")
            f.write(f"| 标准规则 | **{count_std}** | {format_size(size_std)} |\n")
            f.write(f"| MRS 规则 | **{count_mrs}** | {format_size(size_mrs)} |\n")
            f.write(f"| **总计** | **{count_std + count_mrs}** | **{format_size(size_std + size_mrs)}** |\n")


if __name__ == "__main__":
    main()
