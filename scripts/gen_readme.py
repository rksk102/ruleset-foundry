import os
import re
import sys
import urllib.parse

from config_loader import get
from logger import error, group_end, group_start, info, success
from utils import beijing_now, combined_products_hash, load_last_hash

REPO_ROOT = os.getcwd()
DIR_RULES = os.path.join(REPO_ROOT, get("paths", "merged_output_dir", default="merged-rules"))
DIR_MRS = os.path.join(REPO_ROOT, get("paths", "mrs_output_dir", default="merged-rules-mrs"))
README_FILE = os.path.join(REPO_ROOT, "README.md")
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "Owner/Repo")
ARTIFACTS_BRANCH = os.getenv("ARTIFACTS_BRANCH", "artifacts")
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{ARTIFACTS_BRANCH}"
BASE_GHPROXY = f"https://ghproxy.net/{BASE_RAW}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{ARTIFACTS_BRANCH}"
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


def make_page_header(badge_time=None):
    repo_short = REPO_NAME.split("/")[-1]
    time_badge = get_time_badge(badge_time)

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
    return r"""
## 内核版本升级流程（维护者）

内核由 `config.yaml` 的 `mihomo.pinned_version` / `asset_name` / `kernel_sha256` 三字段钉扎；
`kernel-bump.yml` 每周一自动跟随最新正式版（即 `python scripts/convert_mrs.py --bump-config`），
正常情况下无需手工操作。手工升级时**必须先改前两个字段、最后再算哈希**：

1. 在 mihomo 官方 Release 页面确认目标 tag 与资产名（如 `mihomo-linux-amd64-v1.19.32.gz`）。
2. 先改 `config.yaml` 的 `pinned_version` 与 `asset_name`（`kernel_sha256` 暂留旧值）。
3. 再运行 `python scripts/convert_mrs.py --print-kernel-hash`：它会按新的 pin 下载该资产，
   打印解压后二进制的 sha256，用该输出覆盖 `kernel_sha256`。
4. 提 PR，由 CI（pytest + ruff）验证后合并。

> 第 3 步需要执行下载到的内核（`mihomo -v`）来确认可运行，因此只在 Linux 上可用；
> Windows/macOS 请用 WSL 或交给 CI。顺序颠倒会拿到**旧资产**的哈希，下一次流水线会以
> 「内核哈希不匹配」失败。

## 规则优先级与消费方式

策略优先级固定为 `block > direct > policy`；在代理客户端中按此顺序引用 rule-provider。

**每种产物都要按实际格式声明 `behavior` 与 `format`。** mihomo 的 `format` 默认是 `yaml`：
`.txt` 漏写它会被当 YAML 解析，静默得到一个**零规则**且不报错的规则集；`.mrs` 漏写则会直接报
`file must have a payload field`（二进制格式不匹配时不会静默通过）。

| 产物 | `behavior` | `format` |
| :--- | :--- | :--- |
| `merged-rules/<策略>/domain/**/*.txt` | `domain` | `text` |
| `merged-rules/<策略>/ipcidr/**/*.txt` | `ipcidr` | `text` |
| `merged-rules-mrs/**/*.mrs` | 与同名 `.txt` 相同 | `mrs` |

```yaml
rule-providers:
  block:
    type: http
    behavior: domain          # domain 目录用 behavior: domain，ipcidr 目录用 behavior: ipcidr
    format: text              # .txt 必须显式声明，默认值 yaml 会解析成零规则
    url: "https://raw.githubusercontent.com/rksk102/mihomo-rule-library/artifacts/merged-rules/block/domain/Loyalsoldier/reject.txt"
    path: ./ruleset/block.txt
    interval: 86400
  block-mrs:
    type: http
    behavior: domain
    format: mrs               # .mrs 专用二进制格式
    url: "https://raw.githubusercontent.com/rksk102/mihomo-rule-library/artifacts/merged-rules-mrs/block/domain/Loyalsoldier/reject.mrs"
    path: ./ruleset/block.mrs
    interval: 86400
```

产物由 CI 每日生成，**不进入 git 历史**，统一发布在本仓库的 `artifacts` 分支上。
上表所有下载链接均指向该分支；请按链接原样引用，不要改用 `main` 分支。

## 规则格式与匹配语义（重要）

`merged-rules/**/domain/*.txt` 按 **mihomo `behavior: domain` + `format: text` 规则集**格式生成，
四种写法的匹配范围各不相同：

| 写法 | 匹配 `example.com` | 匹配 `www.example.com` | 匹配 `a.b.example.com` |
| :--- | :---: | :---: | :---: |
| `+.example.com` | ✅ | ✅ | ✅ |
| `.example.com` | ❌ | ✅ | ✅ |
| `example.com` | ✅ | ❌ | ❌ |
| `*.example.com` | ❌ | ✅ | ❌ |
| `*.*.example.com` | ❌ | ❌ | ✅ |

记忆要点：

- `+.d` 是**域及其全部子域**（等价 `DOMAIN-SUFFIX`）
- `.d` 是**仅子域，不含 apex**
- 裸 `d` 是**仅该主机名**
- `*` 只匹配**恰好一级**，不匹配 apex

因此请务必用 `behavior: domain` **且** `format: text` 引用这类 `.txt`：换成其它 behavior，
`+.` 与 `.` 行会被当作字面域名而失效；漏写 `format` 则按默认的 `yaml` 解析，整份规则集变成
零规则且不报错。

> 以上依据 mihomo 源码 `component/trie/domain_set.go` 与其官方测试
> `component/trie/domain_set_test.go`（`.example.com` 对 apex 断言为 false，
> `+.example.org` 对 apex 断言为 true）。部分第三方文档把 `.d` 描述为包含 apex，
> 与实现不符，请以本表为准。

`.mrs` 由 `.txt` 编译而来，语义完全一致，但**必须显式声明 `format: mrs`**（默认的 `yaml`
不会识别该二进制格式）。

### 裸域名的语义取决于上游，本仓库不做猜测

上游对「裸域名」的约定**并不统一**，默认按「裸域名 = 精确匹配」处理：

- `MetaCubeX/meta-rules-dat` 的 `geo/geosite/*.list`：**同一文件内**裸行与 `+.` 行并存，
  裸行是维护者有意保留的「精确命中」（例如 `ai.google.dev`），按精确处理是**正确**的。
- `v2rayfly/domain-list-community`（`v2ray-rules-dat` 的上游）规范说明
  `domain:` 前缀可省略，裸行编译为 **sub-domain** 规则，即**后缀**语义。
  这类纯 DLC 系源若按精确处理，会漏掉其全部子域，必须在 `sources.urls` 中显式标注。

### 源级语义标记（`sources.urls`）

`sources.urls` 支持三种独占一行的标记，**按出现顺序作用于其后的所有 URL**（可反复切换）：

| 标记 | 作用 | 缺省值 |
| :--- | :--- | :--- |
| `[policy:...]` | 输出策略目录：含 `reject`/`block`/`deny`/`ads`/`adblock` → `block`；含 `direct`/`bypass`/`no-proxy` → `direct`；含 `proxy`/`gfw` → `policy` | `policy` |
| `[type:...]` | 输出类型目录：含 `ip`/`cidr` → `ipcidr`，否则 `domain` | `domain` |
| `[domain-kind:exact\|suffix]` | 该源**裸域名**的匹配语义 | `exact` |

`domain-kind` 的两种取值：

- `exact`：裸行 `d` 输出为 `d`，即**仅该主机名**（与 mihomo `behavior: domain` 的默认语义一致）。
- `suffix`：裸行 `d` 输出为 `+.d`，即**域及其全部子域**。

使用约束：

- `suffix` **只对上游语义确为「裸行 = 域及其全部子域」的纯文本列表标注**，不要凭猜测添加——
  标注会把该源全部裸行的匹配面扩大到所有子域。
- 标记只影响**没有显式前缀**的裸行：`full:` / `host:` 仍按精确处理，`domain:` / `domain-suffix:` /
  `+.d` / `.d` / `*.d` 等写法保持原有语义，标记不会改写它们。
- 标记行必须独占一行（`[domain-kind:exact|suffix]`、`[policy:...]`、`[type:...]` 均可反复出现）；
  `#` 开头的整行是注释。

当前已标注 `[domain-kind:suffix]` 的源（均为 DLC 系纯文本列表）：

| 策略 | 源 |
| :--- | :--- |
| `block`（拒绝） | `v2ray-rules-dat/release/reject-list.txt`、`win-extra.txt`、`win-spy.txt` |
| `direct` | `v2ray-rules-dat/release/direct-list.txt` |
| `policy`（代理） | `v2ray-rules-dat/release/proxy-list.txt`、`gfw.txt` |

标注方式：在 `sources.urls` 中把 `[domain-kind:suffix]` 写到目标 URL 之前（同组内写一次即可，
其后的 URL 都继承该语义），提 PR 由 CI 校验后生效。

## 发布去重语义

仅当规则**正文**（忽略 `# Date:` 等元数据）发生变化时才会新建 Release；
内容未变化时跳过发布，但产物与 README 仍会提交更新。

## 跨策略冲突处理（conflict_policy）

`behavior.conflict_policy` 支持 `ignore | warn | fail`，默认 `warn`。
`fail` 仅作为"新增源时的临时验收开关"：当前隐式冲突基线噪声较大（约 1.2 万条），
直接启用 `fail` 会中断发布；启用前请先人工核对冲突检测结果。

## 许可与上游署名

- **本仓库的脚本与工作流**（`scripts/`、`tests/`、`.github/`、`config.yaml` 等）以 **MIT** 许可发布，见 [LICENSE](LICENSE)。
- **规则产物**（`artifacts` 分支上的 `.txt` / `.mrs`）是对下列上游数据的下载、清洗与合并。
  上游仓库**均声明 GPL-3.0**（依据 GitHub API `license.spdx_id`）：

| 上游仓库 | 许可 | 本仓库使用的源 |
| :--- | :--- | :--- |
| [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) | GPL-3.0 | `geo/geosite/*.list`、`geo/geoip/cn.list` |
| [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat) | GPL-3.0 | `release/*.txt`（DLC 系列表） |
| [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) | GPL-3.0 | `release/*.txt` |
| [DustinWin/ruleset_geodata](https://github.com/DustinWin/ruleset_geodata) | GPL-3.0 | `mihomo-ruleset/*.list` |

- MIT 仅覆盖本仓库的**脚本代码**，不改变上游内容的许可。再分发本仓库产物（包括以 rule-provider
  URL 形式公开引用）时，请自行确认满足上游 GPL-3.0 的署名与许可要求。
- 规则内容由上游维护者判断，本仓库只做格式转换与合并，**不保证**其准确性、完整性或时效性；
  使用本仓库产物产生的任何后果由使用者自行承担。

## 安全

本项目会从 GitHub Releases 下载并**执行** mihomo 内核二进制（仅用于把规则集编译为 `.mrs`），
信任边界与漏洞报告渠道见 [SECURITY.md](SECURITY.md)。

"""


def main():
    group_start("生成 README")

    files_std = scan_files(DIR_RULES)
    files_mrs = scan_files(DIR_MRS)

    info(f"  标准规则文件: {len(files_std)}")
    info(f"  MRS 规则文件: {len(files_mrs)}")

    badge_time = resolve_badge_time()

    try:
        with open(README_FILE, "w", encoding="utf-8") as f:
            f.write(make_page_header(badge_time))

            f.write("## 规则列表\n\n")

            count_std, size_std = make_section(
                f, "基础规则集合",
                "面向 mihomo (Clash.Meta) 内核：按 `behavior: domain` 加载 `.txt`，"
                "含 `+.d` / `.d` 等 mihomo 专属前缀语义；"
                "Clash Premium、Sing-box 等其它内核不能直接消费 `.txt`，"
                "`.mrs` 更是 mihomo 专用二进制格式",
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
