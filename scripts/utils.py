import hashlib
import ipaddress
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    return datetime.now(BEIJING_TZ)


def beijing_timestamp():
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


def flatten_ip_cidr(cidr_strings, strict=False):
    ipv4_nets = []
    ipv6_nets = []
    errors = []

    for c in cidr_strings:
        c = c.strip()
        if not c:
            continue
        try:
            net = ipaddress.ip_network(c, strict=strict)
            if net.version == 4:
                ipv4_nets.append(net)
            else:
                ipv6_nets.append(net)
        except ValueError as e:
            errors.append((c, str(e)))

    v4_result = [str(n) for n in ipaddress.collapse_addresses(ipv4_nets)]
    v6_result = [str(n) for n in ipaddress.collapse_addresses(ipv6_nets)]
    return sorted(v4_result) + sorted(v6_result), errors


def atomic_write(filepath, content):
    if isinstance(content, list):
        content = "\n".join(content) + "\n"
    elif isinstance(content, str) and not content.endswith("\n"):
        content += "\n"

    dest = Path(filepath)
    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(dest))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def atomic_write_with_header(filepath, rules, metadata):
    lines = ["# " + "-" * 40]
    for key, value in metadata.items():
        lines.append(f"# {key.title()}: {value}")
    lines.append("# " + "-" * 40)
    lines.extend(rules)

    atomic_write(filepath, lines)


def file_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_file_body(path):
    """只哈希规则正文（跳过空行与 # 注释/元数据行），使时间戳不影响聚合哈希。"""
    h = hashlib.sha256()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            h.update(s.encode("utf-8") + b"\n")
    return h.hexdigest()


def dir_hash(dirpath, pattern="*", skip_comments=False):
    """计算目录下所有文件的聚合 SHA256。

    返回 (hash_hex, file_count)。空目录或不存在时返回 ("", 0)，
    以便调用方据此跳过 Release（避免对空内容发布"无变化"误判）。

    skip_comments=True 时按规则正文哈希（忽略 # Date: 等易变元数据行）。
    """
    p = Path(dirpath)
    if not p.exists():
        return "", 0

    h_all = hashlib.sha256()
    files = sorted(p.rglob(pattern))
    count = 0
    for f in files:
        if f.is_file() and not f.name.startswith("."):
            digest = _hash_file_body(str(f)) if skip_comments else file_sha256(str(f))
            h_all.update(digest.encode())
            count += 1

    if count == 0:
        return "", 0
    return h_all.hexdigest(), count


def load_last_hash(hash_file="state/release.sha256"):
    hp = Path(hash_file)
    if hp.exists():
        return hp.read_text(encoding="utf-8").strip()
    return None


def save_last_hash(hash_value, hash_file="state/release.sha256"):
    hp = Path(hash_file)
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(hash_value, encoding="utf-8")


def normalize_policy(p):
    p = p.lower()
    if any(x in p for x in ["reject", "block", "deny", "ads", "adblock"]):
        return "block"
    if any(x in p for x in ["direct", "bypass", "no-proxy"]):
        return "direct"
    if any(x in p for x in ["proxy", "gfw"]):
        return "policy"
    return p if p else "proxy"


def normalize_type(t):
    t = t.lower()
    return "ipcidr" if "ip" in t or "cidr" in t else "domain"


def get_owner_from_url(url):
    parts = url.split("/")
    # 标准格式: https://domain/owner/repo/...
    # parts[0]="https:", parts[1]="", parts[2]="domain", parts[3]="owner"...
    if len(parts) < 3:
        return "unknown"

    domain = parts[2]

    if "github" in domain:
        if len(parts) > 3 and parts[3]:
            return parts[3]
        return "github"
    elif domain == "cdn.jsdelivr.net":
        if len(parts) > 4 and parts[3] == "gh" and parts[4]:
            return parts[4]
        return "jsdelivr"
    else:
        return domain


def normalize_path(p):
    return str(Path(p).as_posix())


def dedup_domain_suffix(domains):
    """同策略内父子域名去重（无条件执行）。

    在 mihomo behavior:domain 语义下，每条规则等同于 DOMAIN-SUFFIX 匹配，
    父域名已覆盖所有子域名，因此子域名规则是冗余的，可安全移除。

    算法：构建与 mihomo 内核相同的倒序标签 Trie，按标签数从少到多遍历，
    若某域名的祖先节点已标记，则跳过；否则插入并标记。

    返回: (去重后的排序域名列表, 被移除的数量)
    """
    if not domains:
        return [], 0

    # 唯一哨兵对象，避免与真实域名标签冲突
    _MARK = object()

    # 按标签数从少到多排序，短域名（可能的父域名）优先处理
    sorted_domains = sorted(domains, key=lambda d: d.count("."))

    # 倒序标签 Trie: {"com": {"google": {MARK}, "youtube": {MARK}}}
    trie = {}
    kept = []
    removed = 0

    for domain in sorted_domains:
        # 按 . 分割并倒序，与 mihomo ValidAndSplitDomain 一致
        # "ads.google.com" → ["com", "google", "ads"]
        parts = domain.split(".")
        parts.reverse()

        # 在 Trie 中搜索：沿路径检查是否存在已标记的祖先节点
        node = trie
        has_marked_ancestor = False
        for part in parts:
            if part not in node:
                break
            node = node[part]
            if _MARK in node:
                has_marked_ancestor = True
                break

        if has_marked_ancestor:
            removed += 1
            continue

        # 无已标记祖先，插入 Trie 并标记
        node = trie
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
        node[_MARK] = True
        kept.append(domain)

    return sorted(kept), removed


def clean_directory(dirpath, keep_root=True):
    p = Path(dirpath)
    if not p.exists():
        if keep_root:
            p.mkdir(parents=True)
        return

    for item in p.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(str(item))
        except Exception:
            pass
