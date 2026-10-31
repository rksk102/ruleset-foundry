import hashlib
import ipaddress
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BEIJING_TZ = timezone(timedelta(hours=8))

REPO_ROOT = Path(__file__).resolve().parent.parent

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

PROTECTED_TOP_LEVEL = {".git", ".github", "scripts", "tests", "state", "docs"}


def anchor_cwd_to_repo_root():
    """让配置里的相对路径始终相对仓库根，而不是调用者的当前目录。"""
    os.chdir(REPO_ROOT)


def is_repo_subdir(path):
    """True 仅当 path 解析后是仓库根下、且不属于受保护顶层目录的真子目录。"""
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    if REPO_ROOT not in target.parents:
        return False
    rel = target.relative_to(REPO_ROOT)
    return not (rel.parts and rel.parts[0].casefold() in PROTECTED_TOP_LEVEL)


def beijing_now():
    return datetime.now(BEIJING_TZ)


def beijing_timestamp():
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


_IP_CANDIDATE_RE = re.compile(r"^\s*([0-9a-fA-F:.]+(?:/[0-9]+)?)\s*(?:[#!].*)?$")


def flatten_ip_cidr(entries, strict=False, extract=False, dropped_default_routes=None):
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
            if dropped_default_routes is not None:
                dropped_default_routes.append(str(net))
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
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
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
    with open(path, encoding="utf-8", errors="replace") as f:
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
        if not f.is_file():
            continue
        rel_path = f.relative_to(p)
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        rel = rel_path.as_posix()
        digest = _hash_file_body(str(f)) if skip_comments else file_sha256(str(f))
        h_all.update(rel.encode("utf-8") + b"\n")
        h_all.update(digest.encode())
        count += 1

    if count == 0:
        return "", 0
    return h_all.hexdigest(), count


def combined_products_hash(txt_dir="merged-rules", mrs_dir="merged-rules-mrs", format_tag=""):
    """format_tag 参与哈希，使清单表头变化也能触发一次发布。"""
    h1, c1 = dir_hash(txt_dir, "*.txt", skip_comments=True)
    h2, c2 = dir_hash(mrs_dir, "*.mrs")
    if format_tag:
        h1 = hashlib.sha256(f"{h1}\0{format_tag}".encode()).hexdigest()
    return f"{h1}|{h2}|{c1}|{c2}", c1, c2


def load_last_hash(hash_file="state/release.sha256"):
    hp = Path(hash_file)
    if hp.exists():
        return hp.read_text(encoding="utf-8").strip()
    return None


def save_last_hash(hash_value, hash_file="state/release.sha256"):
    hp = Path(hash_file)
    hp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(hp), hash_value)


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_component(value):
    """单段名字白名单：首字符为字母/数字，禁 '..' 与路径分隔符。"""
    if not isinstance(value, str) or ".." in value:
        return False
    return bool(_COMPONENT_RE.fullmatch(value))


RULE_TYPES = ("domain", "ipcidr")


def get_rule_type(path_parts):
    """类型取固定层级（<策略>/<类型>/<所有者>/<文件> 的第 2 段），不做子串猜测。"""
    if len(path_parts) >= 2:
        candidate = str(path_parts[1]).lower()
        if candidate in RULE_TYPES:
            return candidate
    return None


def normalize_policy(p):
    p = p.lower()
    if any(x in p for x in ["reject", "block", "deny", "adblock"]):
        return "block"
    tokens = re.split(r"[-_]+", p)
    if "ads" in tokens or any(token.startswith("adservice") for token in tokens):
        return "block"
    if any(x in p for x in ["direct", "bypass", "no-proxy"]):
        return "direct"
    if any(x in p for x in ["proxy", "gfw"]):
        return "policy"
    return p if p else "proxy"


def normalize_type(t):
    t = t.lower()
    if t in RULE_TYPES:
        return t
    raise ValueError(f"非法规则类型: {t!r}（仅允许 {'/'.join(RULE_TYPES)}）")


def get_owner_from_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return "unknown"

    path = [seg for seg in parts.path.strip("/").split("/") if seg]

    if host in (
        "github.com",
        "www.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "gist.githubusercontent.com",
        "github.io",
    ) or host.endswith(".github.io"):
        return path[0] if path else "github"
    if host == "cdn.jsdelivr.net":
        if len(path) >= 2 and path[0] == "gh":
            return path[1]
        return "jsdelivr"
    return host


def normalize_path(p):
    return str(Path(p).as_posix())


def _is_suffix(short, long):
    return len(short) <= len(long) and long[-len(short) :] == short


def _wildcard_covers(pattern_stars, pattern_fixed, entry):
    """判断 `*^n.fixed` 通配模式是否覆盖一个具体条目（通配符条目不参与）。"""
    if entry.startswith("*."):
        return False
    if entry.startswith("+."):
        kind, fixed = "suffix", entry[2:].split(".")
    elif entry.startswith("."):
        kind, fixed = "subdomain", entry[1:].split(".")
    else:
        kind, fixed = "exact", entry.split(".")
    pattern_depth = pattern_stars + len(pattern_fixed)
    child_depth = len(fixed)
    if kind == "exact":
        return child_depth == pattern_depth and _is_suffix(pattern_fixed, fixed)
    if kind == "suffix":
        if _is_suffix(fixed, pattern_fixed):
            return pattern_depth >= child_depth
        if _is_suffix(pattern_fixed, fixed):
            return pattern_stars >= child_depth - len(pattern_fixed)
        return False
    if _is_suffix(fixed, pattern_fixed):
        return pattern_depth > child_depth
    if _is_suffix(pattern_fixed, fixed):
        return pattern_depth > child_depth and pattern_stars >= child_depth - len(pattern_fixed)
    return False


class DomainTrie:
    """倒序标签 Trie；SUFFIX=`+.d`、SUBDOMAIN=`.d`、EXACT=裸 `d`，只有 SUFFIX 能覆盖别的条目。"""

    SUFFIX = 1
    SUBDOMAIN = 2
    EXACT = 3

    def __init__(self):
        self._root = {}
        self._wildcards = []
        self._wildcard_fixed = {}
        self._wildcard_suffix = {}

    def add(self, domain, kind=None):
        if domain.startswith("*.") and domain not in self._wildcards:
            labels = domain.split(".")
            stars = 0
            while stars < len(labels) and labels[stars] == "*":
                stars += 1
            fixed = tuple(labels[stars:])
            depth = stars + len(fixed)
            record = (len(self._wildcards), stars, fixed, domain)
            self._wildcards.append(domain)
            self._wildcard_fixed.setdefault(fixed, {}).setdefault(depth, []).append(record)
            for start in range(len(fixed) + 1):
                suffix = fixed[start:]
                self._wildcard_suffix.setdefault(suffix, {}).setdefault(depth, []).append(record)
        node = self._root
        for part in reversed(domain.split(".")):
            node = node.setdefault(part, {})
        node[kind or self.EXACT] = True

    def _wildcard_candidates(self, entry):
        if entry.startswith("+."):
            kind, fixed = "suffix", entry[2:].split(".")
        elif entry.startswith("."):
            kind, fixed = "subdomain", entry[1:].split(".")
        else:
            kind, fixed = "exact", entry.split(".")
        depth = len(fixed)
        if kind == "exact":
            for start in range(depth + 1):
                suffix = tuple(fixed[start:])
                yield from self._wildcard_fixed.get(suffix, {}).get(depth, ())
            return

        min_depth = depth + 1 if kind == "subdomain" else depth
        for candidate_depth, records in self._wildcard_suffix.get(tuple(fixed), {}).items():
            if candidate_depth >= min_depth:
                yield from records
        for start in range(depth + 1):
            suffix = tuple(fixed[start:])
            for candidate_depth, records in self._wildcard_fixed.get(suffix, {}).items():
                if candidate_depth >= min_depth:
                    yield from records

    def covering_wildcard(self, entry):
        """返回覆盖该具体条目的通配模式（`*.d` 单级语义），无则 None。"""
        if entry.startswith("*."):
            return None
        best = None
        best_order = len(self._wildcards)
        seen = set()
        for order, stars, fixed, pattern in self._wildcard_candidates(entry):
            if order in seen or order >= best_order:
                continue
            seen.add(order)
            if _wildcard_covers(stars, list(fixed), entry):
                best = pattern
                best_order = order
        return best

    def _walk(self, domain):
        node = self._root
        for matched, part in enumerate(reversed(domain.split(".")), 1):
            if part not in node:
                return
            node = node[part]
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
            if kind is None:
                hit = node.get(self.SUFFIX) or node.get(self.SUBDOMAIN) or node.get(self.EXACT)
            elif isinstance(kind, (tuple, set, frozenset)):
                hit = any(node.get(k) for k in kind)
            else:
                hit = node.get(kind)
            if hit:
                return ".".join(parts[len(parts) - matched :])
        return None


def dedup_domain_suffix(domains):
    if not domains:
        return [], 0

    trie = DomainTrie()
    kept = []
    removed = 0
    seen = set()

    def bare(d):
        return d.lstrip("+.") if d.startswith("+.") or d.startswith(".") else d

    for entry in sorted(domains, key=lambda d: (bare(d).count("."), d)):
        if entry in seen:
            removed += 1
            continue
        seen.add(entry)
        name = bare(entry)
        if entry.startswith("+."):
            kind = DomainTrie.SUFFIX
        elif entry.startswith("."):
            kind = DomainTrie.SUBDOMAIN
        else:
            kind = DomainTrie.EXACT

        if trie.has_marked_ancestor(name, DomainTrie.SUFFIX) or trie.covering_parent(
            name, (DomainTrie.SUFFIX, DomainTrie.SUBDOMAIN)
        ):
            removed += 1
            continue
        if kind == DomainTrie.EXACT and trie.covering_wildcard(entry):
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
