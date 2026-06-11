import os
import re
import json
import hashlib
import ipaddress
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone

CACHE_DIR = Path(".cache")
ETAG_FILE = CACHE_DIR / "etag_cache.json"


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
    CACHE_DIR.mkdir(exist_ok=True)
    if ETAG_FILE.exists():
        try:
            return json.loads(ETAG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {}


def save_etag_cache(cache):
    CACHE_DIR.mkdir(exist_ok=True)
    ETAG_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


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


def file_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_hash(dirpath, pattern="*"):
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
    return str(Path(p).as_posix())


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
