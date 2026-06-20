import os
import json
import hashlib
import ipaddress
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone

CACHE_DIR = Path(".cache")
ETAG_FILE = CACHE_DIR / "etag_cache.json"

_etag_cache = None
_etag_dirty = False


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


def is_valid_cidr(c):
    try:
        ipaddress.ip_network(c.strip(), strict=False)
        return True
    except ValueError:
        return False


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


def load_etag_cache():
    """加载 ETag 缓存到内存，仅首次访问时读文件，后续返回内存副本。"""
    global _etag_cache
    if _etag_cache is not None:
        return _etag_cache
    _etag_cache = {}
    if ETAG_FILE.exists():
        try:
            _etag_cache = json.loads(ETAG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            _etag_cache = {}
    return _etag_cache


def save_etag_cache(cache):
    CACHE_DIR.mkdir(exist_ok=True)
    ETAG_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def flush_etag_cache():
    """若内存缓存有变更，则一次性落盘。"""
    global _etag_dirty
    if _etag_dirty and _etag_cache is not None:
        save_etag_cache(_etag_cache)
        _etag_dirty = False


def get_cached_headers(url):
    cache = load_etag_cache()
    entry = cache.get(url, {})
    headers = {}
    if "etag" in entry:
        headers["If-None-Match"] = entry["etag"]
    if "last_modified" in entry:
        headers["If-Modified-Since"] = entry["last_modified"]
    return headers


def update_etag_cache(url, response):
    global _etag_dirty
    cache = load_etag_cache()
    entry = cache.get(url, {})
    changed = False

    etag = response.headers.get("ETag")
    last_mod = response.headers.get("Last-Modified")
    if etag:
        entry["etag"] = etag
        changed = True
    if last_mod:
        entry["last_modified"] = last_mod
        changed = True

    if changed:
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        cache[url] = entry
        _etag_dirty = True


def file_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_hash(dirpath, pattern="*"):
    """计算目录下所有文件的聚合 SHA256。

    返回 (hash_hex, file_count)。空目录或不存在时返回 ("", 0)，
    以便调用方据此跳过 Release（避免对空内容发布"无变化"误判）。
    """
    p = Path(dirpath)
    if not p.exists():
        return "", 0

    h_all = hashlib.sha256()
    files = sorted(p.rglob(pattern))
    count = 0
    for f in files:
        if f.is_file() and not f.name.startswith("."):
            h_all.update(file_sha256(str(f)).encode())
            count += 1

    if count == 0:
        return "", 0
    return h_all.hexdigest(), count


def load_last_hash(hash_file=".cache/last_release_hash.txt"):
    hp = Path(hash_file)
    if hp.exists():
        return hp.read_text(encoding="utf-8").strip()
    return None


def save_last_hash(hash_value, hash_file=".cache/last_release_hash.txt"):
    CACHE_DIR.mkdir(exist_ok=True)
    Path(hash_file).write_text(hash_value, encoding="utf-8")


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
    """同策略内父子域名去重（严格模式）。

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
