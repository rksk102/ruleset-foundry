"""
公共工具模块 — CIDR 聚合、原子写入、ETag 缓存等通用能力。
"""
import os
import re
import json
import hashlib
import ipaddress
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# ---------- 路径常量 ----------
CACHE_DIR = Path(".cache")
ETAG_FILE = CACHE_DIR / "etag_cache.json"

# ---------- CIDR 聚合 ----------

def flatten_ip_cidr(cidr_strings, strict=False):
    """
    统一 CIDR 聚合：IPv4/IPv6 分离 → collapse_addresses → 返回排序后的字符串列表。
    所有需要 CIDR 聚合的地方都调用此函数。
    """
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


# ---------- 原子写入 ----------

def atomic_write(filepath, content):
    """
    原子写入：先写入同目录下的临时文件，成功后再 rename。
    保证文件不会处于半写状态。
    """
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
    """
    带元数据头的原子写入。
    metadata: dict (如 strategy, type, owner, count 等)
    rules: list of str
    """
    lines = ["# " + "-" * 40]
    for key, value in metadata.items():
        lines.append(f"# {key.title()}: {value}")
    lines.append("# " + "-" * 40)
    lines.extend(rules)

    atomic_write(filepath, lines)


# ---------- ETag / 增量缓存 ----------

def load_etag_cache():
    """加载 ETag 缓存"""
    CACHE_DIR.mkdir(exist_ok=True)
    if ETAG_FILE.exists():
        try:
            return json.loads(ETAG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {}


def save_etag_cache(cache):
    """保存 ETag 缓存"""
    CACHE_DIR.mkdir(exist_ok=True)
    ETAG_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def get_cached_headers(url):
    """获取某 URL 的缓存头，用于条件请求"""
    cache = load_etag_cache()
    entry = cache.get(url, {})
    headers = {}
    if "etag" in entry:
        headers["If-None-Match"] = entry["etag"]
    if "last_modified" in entry:
        headers["If-Modified-Since"] = entry["last_modified"]
    return headers


def update_etag_cache(url, response):
    """根据响应更新 ETag 缓存"""
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
        save_etag_cache(cache)


# ---------- 文件哈希 ----------

def file_sha256(filepath):
    """计算文件 SHA256"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_hash(dirpath, pattern="*"):
    """
    计算目录下所有文件的聚合哈希（用于变更检测）。
    返回 (combined_hash, file_count)
    """
    p = Path(dirpath)
    if not p.exists():
        return hashlib.sha256().hexdigest(), 0

    h_all = hashlib.sha256()
    files = sorted(p.rglob(pattern))
    count = 0
    for f in files:
        if f.is_file() and not f.name.startswith("."):
            h_all.update(file_sha256(str(f)).encode())
            count += 1
    return h_all.hexdigest(), count


def load_last_hash(hash_file=".cache/last_release_hash.txt"):
    """读取上次发布的哈希"""
    hp = Path(hash_file)
    if hp.exists():
        return hp.read_text(encoding="utf-8").strip()
    return None


def save_last_hash(hash_value, hash_file=".cache/last_release_hash.txt"):
    """保存当前哈希"""
    CACHE_DIR.mkdir(exist_ok=True)
    Path(hash_file).write_text(hash_value, encoding="utf-8")


# ---------- 策略/类型规范化 ----------

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
    """从 URL 中提取所有者"""
    parts = url.split("/")
    domain = parts[2]

    if "github" in domain:
        return parts[3]
    elif domain == "cdn.jsdelivr.net":
        if len(parts) > 4 and parts[3] == "gh":
            return parts[4]
        return "jsdelivr"
    else:
        return domain


def normalize_path(p):
    """标准化路径分隔符"""
    return str(Path(p).as_posix())


# ---------- 文件工具 ----------

def clean_directory(dirpath, keep_root=True):
    """清空目录内容但保留目录本身"""
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
