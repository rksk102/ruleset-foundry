import hashlib
import ipaddress
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    return datetime.now(BEIJING_TZ)


def beijing_timestamp():
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


_IP_CANDIDATE_RE = re.compile(r"([0-9a-fA-F:.]+(?:/[0-9]+)?)")


def flatten_ip_cidr(entries, strict=False, extract=False):
    """解析并合并 CIDR，返回 (列表, 错误列表)。

    丢弃默认路由（/0）；extract=True 按行内子串提取，否则整串解析；
    输出为 v4 块 + v6 块，块内字典序。
    """
    ipv4_nets = []
    ipv6_nets = []
    errors = []

    for entry in entries:
        text = entry.strip()
        if not text:
            continue

        candidate = text
        if extract:
            match = _IP_CANDIDATE_RE.search(text)
            if not match:
                errors.append((text, "未找到 IP/CIDR"))
                continue
            candidate = match.group(1)

        try:
            net = ipaddress.ip_network(candidate, strict=strict)
        except ValueError as e:
            errors.append((text, str(e)))
            continue

        if net.prefixlen == 0:
            continue
        if net.version == 4:
            ipv4_nets.append(net)
        else:
            ipv6_nets.append(net)

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
    """计算目录下所有文件的聚合 SHA256，返回 (hash_hex, file_count)。

    空目录或不存在时返回 ("", 0)，供调用方跳过发布；
    skip_comments=True 忽略 # Date: 等易变元数据行。
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


def combined_products_hash(txt_dir="merged-rules", mrs_dir="merged-rules-mrs"):
    """产物聚合哈希，返回 (hash, txt_count, mrs_count)。

    与 release_handler 变更检测同口径：.txt 按正文哈希，.mrs 整文件哈希。
    """
    h1, c1 = dir_hash(txt_dir, "*.txt", skip_comments=True)
    h2, c2 = dir_hash(mrs_dir, "*.mrs")
    return f"{h1}|{h2}|{c1}|{c2}", c1, c2


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
    # 标准格式 https://域名/owner/repo/...：parts[2] 为域名，parts[3] 起为 owner
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


class DomainTrie:
    """倒序标签 Trie，用于父子域名关系判定。

    遍历顺序与 mihomo ValidAndSplitDomain 一致，判定语义等同 DOMAIN-SUFFIX。
    """

    _MARK = object()

    def __init__(self):
        self._root = {}

    def add(self, domain):
        node = self._root
        for part in reversed(domain.split(".")):
            node = node.setdefault(part, {})
        node[self._MARK] = True

    def has_marked_ancestor(self, domain):
        node = self._root
        for part in reversed(domain.split(".")):
            if part not in node:
                return False
            node = node[part]
            if node.get(self._MARK):
                return True
        return False

    def covering_parent(self, domain):
        """返回已标记的严格祖先域名，没有则返回 None。"""
        parts = domain.split(".")
        node = self._root
        matched = []
        for part in reversed(parts):
            if part not in node:
                break
            node = node[part]
            matched.append(part)
            if node.get(self._MARK) and len(matched) < len(parts):
                return ".".join(reversed(matched))
        return None


def dedup_domain_suffix(domains):
    """同策略内父子域名去重，返回 (排序后的域名列表, 被移除的数量)。"""
    if not domains:
        return [], 0

    trie = DomainTrie()
    kept = []
    removed = 0

    for domain in sorted(domains, key=lambda d: d.count(".")):
        if trie.has_marked_ancestor(domain):
            removed += 1
            continue
        trie.add(domain)
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
