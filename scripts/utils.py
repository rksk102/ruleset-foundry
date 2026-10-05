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
    h = hashlib.sha256()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            h.update(s.encode("utf-8") + b"\n")
    return h.hexdigest()


def dir_hash(dirpath, pattern="*", skip_comments=False):
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
    """倒序标签 Trie，判定父子域名关系。

    SUFFIX 为 `+.d`（域及全部子域），SUBDOMAIN 为 `.d`（仅子域），EXACT 为 `d`（仅该主机名）。
    只有 SUFFIX 能覆盖其他条目。
    """

    SUFFIX = 1
    SUBDOMAIN = 2
    EXACT = 3

    def __init__(self):
        self._root = {}

    def add(self, domain, kind=None):
        node = self._root
        for part in reversed(domain.split(".")):
            node = node.setdefault(part, {})
        node[kind or self.EXACT] = True

    def _walk(self, domain):
        node = self._root
        matched = 0
        for part in reversed(domain.split(".")):
            if part not in node:
                return
            node = node[part]
            matched += 1
            yield matched, node

    def has_marked_ancestor(self, domain, kind=None):
        for _matched, node in self._walk(domain):
            if kind is None:
                if node.get(self.SUFFIX) or node.get(self.SUBDOMAIN) or node.get(self.EXACT):
                    return True
            elif node.get(kind):
                return True
        return False

    def covering_parent(self, domain, kind=None):
        parts = domain.split(".")
        for matched, node in self._walk(domain):
            if matched >= len(parts):
                break
            hit = (node.get(self.SUFFIX) or node.get(self.SUBDOMAIN)
                   or node.get(self.EXACT)) if kind is None else node.get(kind)
            if hit:
                return ".".join(parts[len(parts) - matched:])
        return None


def dedup_domain_suffix(domains):
    """同策略内父子域名去重，返回 (排序后的列表, 被移除的数量)。

    条目写法与其匹配集：
      `+.d` 域及其全部子域（最宽）
      `.d`  仅子域，不含 apex
      `d`   仅主机名 d
    只有 `+.` 前缀具备覆盖能力：它同时涵盖 `.d` 与 `d`，也涵盖二者的子级写法。
    """
    if not domains:
        return [], 0

    trie = DomainTrie()
    kept = []
    removed = 0

    def bare(d):
        return d.lstrip("+.") if d.startswith("+.") or d.startswith(".") else d

    for entry in sorted(domains, key=lambda d: (bare(d).count("."), d)):
        name = bare(entry)
        if entry.startswith("+."):
            kind = DomainTrie.SUFFIX
        elif entry.startswith("."):
            kind = DomainTrie.SUBDOMAIN
        else:
            kind = DomainTrie.EXACT

        # `+.p` 是唯一能覆盖其他条目的形态
        if trie.has_marked_ancestor(name, DomainTrie.SUFFIX):
            removed += 1
            continue

        trie.add(name, kind)
        kept.append(entry)

    return sorted(kept), removed


def clean_directory(dirpath, keep_root=True):
    p = Path(dirpath)
    if not p.exists():
        if keep_root:
            p.mkdir(parents=True)
        return []

    failed = []
    for item in p.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(str(item))
        except Exception as e:
            failed.append((str(item), str(e)))

    return failed
