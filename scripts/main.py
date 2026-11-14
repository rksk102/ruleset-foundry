import asyncio
import contextlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import manifest
import processor
from config_loader import ConfigError, get
from logger import debug, gh_error, group_end, group_start, info, section, success, warning
from utils import (
    REPO_ROOT,
    USERINFO_HIDDEN_PLACEHOLDER,
    WINDOWS_RESERVED_NAMES,
    anchor_cwd_to_repo_root,
    atomic_write,
    beijing_now,
    beijing_timestamp,
    file_sha256,
    get_owner_from_url,
    is_repo_subdir,
    is_safe_component,
    normalize_policy,
    normalize_type,
    redact_url_userinfo,
)

try:
    SOURCES_FILE = get("paths", "sources_file", default="sources.urls")
    RULESETS_DIR = Path(get("paths", "rulesets_dir", default="rulesets"))
    TIMEOUT = get("network", "timeout_seconds", default=15)
    RETRIES = get("network", "max_retries", default=2)
    MAX_SOURCE_BYTES = get("network", "max_source_bytes", default=64 * 1024 * 1024)
    UNRECOGNIZED_WARN_RATIO = get("behavior", "unrecognized_warn_ratio", default=0.10)
    CONCURRENCY = get("network", "max_concurrency", default=6)
    PER_HOST = get("network", "max_per_host", default=2)
    MAX_RETRY_AFTER = get("network", "max_retry_after_seconds", default=60)
    MIN_SUCCESS_RATIO = get("behavior", "min_source_success_ratio", default=0.0)
    ALLOW_PARTIAL = get("behavior", "allow_partial", default=False)
except ConfigError as _cfg_err:
    gh_error(f"配置校验失败: {_cfg_err}")
    sys.exit(1)

MANIFEST_FILE = RULESETS_DIR / manifest.MANIFEST_NAME

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5

# 单源总时限（秒）：重定向、重试、退避全部共享；不新增配置键，避免被上游拖住整轮任务。
SOURCE_TOTAL_TIMEOUT = 90.0
# 目标端口白名单：仅允许标准 HTTPS 端口，非预期端口一律拒绝。
ALLOWED_TARGET_PORTS = frozenset({443})

UNRECOGNIZED_MIN_LINES = 20

_UNEXPECTED_DROP_KEYS = (
    "dropped_keyword",
    "dropped_modifier",
    "dropped_path",
    "dropped_exception",
    "ip_in_domain",
)

AUTH_HOST_SUFFIXES = (
    "githubusercontent.com",
    "github.com",
)
USER_AGENT = "ruleset-foundry/1.0"


def is_trusted_host(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return any(host == sfx or host.endswith("." + sfx) for sfx in AUTH_HOST_SUFFIXES)


def auth_headers(url):
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token or not is_trusted_host(url):
        return None
    return {"Authorization": f"Bearer {token}"}


def _is_https_url(value):
    try:
        return urlsplit(str(value)).scheme == "https"
    except ValueError:
        return False


def _same_host(left, right):
    try:
        return (urlsplit(str(left)).hostname or "").lower() == (urlsplit(str(right)).hostname or "").lower()
    except ValueError:
        return False


def _literal_ip(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_public_address(addr):
    """仅接受全局可路由且非多播/未指定的地址；回环、私网、链路本地、保留段全部拒绝。"""
    return addr.is_global and not addr.is_multicast and not addr.is_unspecified


class TargetResolutionRejected(aiohttp.ClientConnectionError):
    """连接器 resolver 拒绝非公网/混合/空解析结果。"""


def _ascii_host(host):
    """按 IDNA 编码主机名，使其与 yarl 的 raw_host（DNS 缓存键）一致。

    ASCII 主机名原样返回（与 yarl 一致，不额外套用 IDNA 的字符限制）；只有非 ASCII 才编码。
    """
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _resolved_host(result):
    return result.get("host") if isinstance(result, dict) else getattr(result, "host", None)


class _PublicTargetResolver(aiohttp.abc.AbstractResolver):
    """包装真实 resolver：任何一次实际解析都必须全部解析成公网地址。

    预检与建连之间仍存在 DNS 重新解析（缓存过期/未命中）的窗口，连接器实际走的这个 resolver
    才是无法被上游重定向绕开的关口：非公网、公私混合、空结果一律拒绝。
    """

    def __init__(self, resolver):
        self._resolver = resolver

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self._resolver.resolve(host, port, family)
        if not results:
            raise TargetResolutionRejected(f"无法解析目标主机 {host}，已拒绝（fail-closed）")
        for result in results:
            address = _resolved_host(result)
            resolved = _literal_ip(address) if address else None
            if resolved is None or not _is_public_address(resolved):
                raise TargetResolutionRejected(f"拒绝解析到非公网地址的目标主机 {host} -> {address}")
        return results

    async def close(self):
        await self._resolver.close()


async def _resolved_addresses(session, hostname, port):
    """用连接器实际使用的 resolver 解析目标地址（含连接器 DNS 缓存，缩小 rebinding 窗口）。

    返回地址列表；无法验证（没有连接器/resolver、解析失败）返回 None 或空列表，由调用方 fail-closed。
    临时 DNS 故障是例外：转成 aiohttp.ClientError 交给 download_one 的既有退避重试吸收；
    安全拒绝（TargetResolutionRejected）不重试。
    """
    connector = getattr(session, "connector", None)
    resolve_host = getattr(connector, "_resolve_host", None)
    if resolve_host is None:
        return None
    try:
        results = await resolve_host(hostname, port)
    except TargetResolutionRejected:
        raise
    except (aiohttp.ClientError, OSError) as e:
        raise aiohttp.ClientConnectionError(
            f"目标主机 {hostname} 解析失败（{type(e).__name__}），可重试"
        ) from e
    except Exception:
        return []
    addresses = []
    for item in results:
        address = _resolved_host(item)
        if address:
            addresses.append(str(address))
    return addresses


async def check_request_target(session, url):
    """逐跳请求前校验目标地址，返回拒绝原因；None 表示放行。

    拒绝条件：userinfo、非预期端口、非公网 IP 字面量，以及主机名按 IDNA 编码后由连接器 resolver
    解析出的非公网地址。解析不到或没有可用 resolver 一律 fail-closed；只有临时解析故障
    （OSError/ClientError）会向上抛出，交给 ``download_one`` 的退避重试。这里只是快速预检；来源下载
    已禁用 trust_env 代理，实际连接由 ``_PublicTargetResolver`` 再次强制校验。
    """
    display = redact_url_userinfo(url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # 异常文本会回显畸形 netloc（可能含密码前缀），只回报已脱敏的显示形式
        return f"目标 URL 端口或主机名非法: {display}"
    if display == USERINFO_HIDDEN_PLACEHOLDER:
        # host:port 与畸形 userinfo 无法可靠区分，拒绝后才不会进入请求或产物日志
        return f"拒绝 userinfo 解析有歧义的目标地址: {display}"
    if parts.username is not None or parts.password is not None:
        return f"拒绝含 userinfo 的目标地址: {display}"
    host = parts.hostname
    if not host:
        return f"目标 URL 缺少主机名: {display}"
    if (port if port is not None else 443) not in ALLOWED_TARGET_PORTS:
        return (
            f"拒绝非预期端口 {port}: {display}"
            f"（仅允许 {'/'.join(str(p) for p in sorted(ALLOWED_TARGET_PORTS))}）"
        )

    literal = _literal_ip(host)
    if literal is not None:
        if not _is_public_address(literal):
            return f"拒绝非公网目标地址 {host}: {display}"
        return None

    ascii_host = _ascii_host(host)
    if ascii_host is None:
        return f"目标主机名无法按 IDNA 编码，已拒绝: {display}"
    try:
        addresses = await _resolved_addresses(session, ascii_host, port if port is not None else 443)
    except TargetResolutionRejected as e:
        return f"{e}: {display}"
    if not addresses:
        return f"无法验证目标主机解析结果，已拒绝（fail-closed）: {display}"
    for address in addresses:
        resolved = _literal_ip(address)
        if resolved is None or not _is_public_address(resolved):
            return f"拒绝解析到非公网地址的目标主机 {host} -> {address}: {display}"
    return None


def _timeout_reason(remaining):
    return f"超过单源总时限 {SOURCE_TOTAL_TIMEOUT:.0f}s（剩余 {max(remaining, 0):.1f}s）"


def parse_retry_after(value, default, max_seconds=MAX_RETRY_AFTER):
    if not value:
        return default
    text = value.strip()
    if text.isascii() and text.isdecimal():
        seconds = int(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except TypeError, ValueError:
            return default
        if when is None:
            return default
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = int((when - datetime.now(UTC)).total_seconds())
    if max_seconds is not None:
        seconds = min(seconds, max_seconds)
    return max(0, seconds)


def source_repo_slug(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.strip("/").split("/") if p]

    if host in (
        "github.com",
        "www.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "gist.githubusercontent.com",
        "github.io",
    ) or host.endswith(".github.io"):
        if len(path) >= 2:
            return f"{path[0]}__{path[1]}"
        return ""
    if host == "cdn.jsdelivr.net":
        if len(path) >= 3 and path[0] == "gh":
            return f"{path[1]}__{path[2].split('@')[0]}"
        if len(path) >= 2:
            return f"{path[0]}__{path[1].split('@')[0]}"
        return ""
    return host.replace(".", "_") if host else ""


_UNSAFE_COMPONENT_CHARS_RE = re.compile(r'[/\\:*?"<>|#%@\s\x00-\x1f\x7f]')


def safe_marker_value(value, label, line_no=None):
    if not is_safe_component(value):
        where = f"sources.urls:{line_no}: " if line_no else ""
        gh_error(
            f"{where}非法 [{label}:] 标记: {value!r}（仅允许 [A-Za-z0-9._-]，不得含 '..'、前导点、盘符或路径分隔符）"
        )
        sys.exit(1)
    return value


def clean_path_component(value, fallback):
    text = _UNSAFE_COMPONENT_CHARS_RE.sub("_", value or "")
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip(". ")
    if not text or text.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        return fallback
    return text


def _anchored_path(path):
    target = Path(path)
    if not target.is_absolute():
        target = REPO_ROOT / target
    return target.resolve()


def _resolved_rulesets_dir():
    try:
        root = _anchored_path(RULESETS_DIR)
    except (OSError, RuntimeError) as e:
        gh_error(f"rulesets 根目录解析失败，拒绝写入或清理: {RULESETS_DIR}（{e}）")
        sys.exit(1)
    if not is_repo_subdir(root):
        gh_error(f"rulesets 根目录越界，拒绝写入或清理: {root}（须为 {REPO_ROOT} 内的真子目录）")
        sys.exit(1)
    return root


def ensure_within_rulesets(abs_path):
    root = _resolved_rulesets_dir()
    target = _anchored_path(abs_path)
    if target != root and root not in target.parents:
        gh_error(f"输出路径越界，拒绝写入: {target}（须位于 {root} 内）")
        sys.exit(1)
    return target


_STAGING_PREFIX = ".staging-"


def _staging_output_path(staging_root, final_path):
    root = _resolved_rulesets_dir()
    final = ensure_within_rulesets(final_path)
    relative = final.relative_to(root)
    return ensure_within_rulesets(Path(staging_root) / relative)


def _cleanup_stale_staging(root):
    _recover_stale_transactions(root)
    for path in root.iterdir():
        if path.name.startswith(_STAGING_PREFIX) and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def _sync_lock(root):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("本地同步锁当前仅支持 Linux，拒绝无保护地并发写入")
    import fcntl

    lock_path = root / ".sync.lock"
    root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"另一个同步进程正在写入 {root}，本轮拒绝并发提交") from exc
        yield


_ROLLBACK_MANIFEST = "transaction.json"


def _write_rollback_manifest(rollback_root, staging_root, entries, committed=False):
    payload = {
        "version": 1,
        "staging": str(Path(staging_root)),
        "committed": committed,
        "entries": entries,
    }
    atomic_write(rollback_root / _ROLLBACK_MANIFEST, json.dumps(payload, ensure_ascii=False))


def _read_rollback_manifest(root, rollback_root):
    try:
        data = json.loads((rollback_root / _ROLLBACK_MANIFEST).read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("entries"), list):
            raise ValueError("事务清单结构无效")
        staging = Path(data["staging"])
        ensure_within_rulesets(staging)
        if staging.parent != root or not staging.name.startswith(_STAGING_PREFIX):
            raise ValueError("事务暂存目录无效")
        entries = []
        seen = set()
        for item in data["entries"]:
            final = ensure_within_rulesets(root / item["path"])
            backup_name = item["backup"]
            if not isinstance(backup_name, str) or not backup_name.isdigit() or final in seen:
                raise ValueError("事务备份名或目标路径无效")
            if not isinstance(item["existed"], bool):
                raise ValueError("事务原文件状态无效")
            seen.add(final)
            entries.append((final, rollback_root / backup_name, item["existed"]))
        return staging, bool(data["committed"]), entries
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取恢复事务 {rollback_root}: {exc}") from exc


def _restore_entries(entries):
    failed = False
    for final, backup, existed in reversed(entries):
        try:
            if backup.is_file() and not backup.is_symlink():
                os.replace(backup, final)
            elif not existed:
                final.unlink(missing_ok=True)
        except OSError as exc:
            failed = True
            gh_error(f"旧产物恢复失败: {final}（{exc}）")
    return not failed


def _recover_stale_transactions(root):
    for rollback_root in sorted(root.glob(".rollback-*")):
        if not rollback_root.is_dir() or rollback_root.is_symlink():
            raise ValueError(f"恢复事务不是普通目录: {rollback_root}")
        if not (rollback_root / _ROLLBACK_MANIFEST).exists() and not any(rollback_root.iterdir()):
            shutil.rmtree(rollback_root)
            continue
        staging, committed, entries = _read_rollback_manifest(root, rollback_root)
        if not committed and not _restore_entries(entries):
            raise RuntimeError(f"旧产物恢复未完成，保留事务现场: {rollback_root}")
        shutil.rmtree(rollback_root)
        if staging.exists():
            shutil.rmtree(staging)


def commit_staged_products(staging_root, expected_files):
    root = _resolved_rulesets_dir()
    moves = []
    entries = []
    for expected in expected_files:
        final = ensure_within_rulesets(expected)
        staged = ensure_within_rulesets(Path(staging_root) / final.relative_to(root))
        if not staged.is_file():
            raise FileNotFoundError(f"暂存产物缺失: {staged}")
        moves.append((staged, final))
        entries.append({
            "path": final.relative_to(root).as_posix(),
            "backup": str(len(entries)),
            "existed": final.exists(),
            "sha256": file_sha256(final) if final.is_file() and not final.is_symlink() else None,
        })
    for _staged, final in moves:
        final.parent.mkdir(parents=True, exist_ok=True)
    rollback_root = Path(tempfile.mkdtemp(prefix=".rollback-", dir=root))
    try:
        _write_rollback_manifest(rollback_root, staging_root, entries)
    except BaseException:
        shutil.rmtree(rollback_root, ignore_errors=True)
        raise
    try:
        for (staged, final), entry in zip(moves, entries, strict=True):
            backup = rollback_root / entry["backup"]
            if entry["existed"]:
                if not final.is_file() or final.is_symlink():
                    raise OSError(f"旧产物不是普通文件，拒绝替换: {final}")
                os.replace(final, backup)
            os.replace(staged, final)
        _write_rollback_manifest(rollback_root, staging_root, entries, committed=True)
    except BaseException:
        if not _restore_entries([
            (final, rollback_root / entry["backup"], entry["existed"])
            for final, entry in zip((final for _staged, final in moves), entries, strict=True)
        ]):
            gh_error(f"恢复未完成，保留旧产物备份与事务清单: {rollback_root}")
            raise
        shutil.rmtree(rollback_root)
        raise
    shutil.rmtree(rollback_root)


def _base_rel_path(task):
    owner = clean_path_component(get_owner_from_url(task["url"]), "unknown")
    last_segment = task["url"].split("/")[-1].split("?")[0].split("#")[0]
    filename = clean_path_component(last_segment.rsplit(".", 1)[0], "rules") + ".txt"
    policy = clean_path_component(task["policy"], "policy")
    type_name = clean_path_component(task["type"], "domain")
    return Path(policy) / type_name / owner / filename


def normalize_source_url(url):
    """规范化来源 URL 作为去重/比对键：去掉 userinfo、默认端口与片段。

    这里只做结构归一化，不经过日志脱敏：畸形 userinfo 的遮蔽结果是整体替换的占位符，
    而正常 URL（含显式 443、路径中的 `@`）必须保持原样。凭据安全由调用方在解析阶段拒绝。
    """
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        # 主机/端口部分无法解析时不做猜测，退回已遮蔽的显示形式（保证不含凭据）
        return redact_url_userinfo(url)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


def validate_source_url(url, line_no, line):
    display = redact_url_userinfo(line)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # 异常文本会回显畸形 netloc（例如 "Port could not be cast ... '12'"），可能泄露密码前缀
        _fail_marker(line_no, display, "来源 URL 端口或主机名非法")
    if display == USERINFO_HIDDEN_PLACEHOLDER:
        _fail_marker(line_no, display, "来源 URL 的 userinfo 解析有歧义")
    if parts.username is not None or parts.password is not None:
        _fail_marker(line_no, display, "来源 URL 含 userinfo（凭据必须放在环境变量，禁止写入 sources.urls）")
    if not parts.hostname:
        _fail_marker(line_no, display, "来源 URL 缺少主机名")
    if port == 0:
        _fail_marker(line_no, display, "来源 URL 端口非法: 0")
    if (port if port is not None else 443) not in ALLOWED_TARGET_PORTS:
        _fail_marker(line_no, display, f"来源 URL 端口 {port} 不在允许范围（仅允许 443）")
    literal = _literal_ip(parts.hostname)
    if literal is not None and not _is_public_address(literal):
        _fail_marker(line_no, display, f"来源 URL 指向非公网地址: {parts.hostname}")


def check_same_slug_collisions(tasks):
    by_path = {}
    for task in tasks:
        rel = str(_base_rel_path(task))
        slug = source_repo_slug(task["url"]) or rel
        by_path.setdefault(rel, {}).setdefault(slug, {}).setdefault(normalize_source_url(task["url"]), task["url"])

    for rel, slug_map in sorted(by_path.items()):
        for slug, urls in sorted(slug_map.items()):
            if len(urls) > 1:
                detail = "、".join(sorted(urls))
                gh_error(
                    f"同仓库来源映射到同一产物 {Path(rel).as_posix()}（{slug}: {detail}）；"
                    "请为来源使用不同文件名，显式输出别名尚未支持"
                )
                sys.exit(1)


def colliding_output_paths(tasks):
    by_path = {}
    for task in tasks:
        rel = _base_rel_path(task)
        by_path.setdefault(str(rel), []).append(source_repo_slug(task["url"]))

    plan = {}
    for rel, slugs in by_path.items():
        distinct = {s for s in slugs if s}
        if len(slugs) > 1 and len(distinct) > 1:
            plan[rel] = sorted(distinct)
    return plan


def build_filepath(task, collide_plan=None, taken=None):
    rel_path = _base_rel_path(task)
    owner = rel_path.parent.name
    slug = source_repo_slug(task["url"]) or owner

    if collide_plan and str(rel_path) in collide_plan:
        candidate = clean_path_component(slug, "source")
        proposal = Path(task["policy"]) / task["type"] / candidate / rel_path.name
        if taken is not None:
            while str(proposal) in taken and taken[str(proposal)] != slug:
                candidate += "_"
                proposal = Path(task["policy"]) / task["type"] / candidate / rel_path.name
        rel_path = proposal
        owner = candidate

    if taken is not None:
        taken[str(rel_path)] = slug
    return owner, rel_path.name, rel_path, RULESETS_DIR / rel_path


def plan_groups(tasks):
    check_same_slug_collisions(tasks)

    deduped = []
    seen = set()
    for idx, task in enumerate(tasks):
        key = (str(_base_rel_path(task)), normalize_source_url(task["url"]), task.get("domain_kind", "exact"))
        if key in seen:
            warning(f"  重复来源已去重: {task['url']} -> {Path(key[0]).as_posix()}")
            continue
        seen.add(key)
        deduped.append((idx, task))

    collide_plan = colliding_output_paths([task for _idx, task in deduped])
    for rel, slugs in sorted(collide_plan.items()):
        warning(f"  输出路径跨来源冲突 {rel} -> 已按来源拆分为 {slugs}")

    taken = {}
    for _idx, task in deduped:
        base = _base_rel_path(task)
        taken.setdefault(str(base), source_repo_slug(task["url"]) or base.parent.name)

    groups = {}
    for idx, task in deduped:
        _, _, _, abs_path = build_filepath(task, collide_plan, taken)
        key = str(abs_path)
        group = groups.setdefault(
            key,
            {
                "path": key,
                "policy": task["policy"],
                "type": task["type"],
                "domain_kind": task.get("domain_kind", "exact"),
                "members": [],
                "sources": [],
            },
        )
        group["members"].append((idx, task))
        group["sources"].append(task["url"])
        if task.get("domain_kind", "exact") != group["domain_kind"]:
            group["domain_kind"] = "mixed"

    casefolded = {}
    for key in groups:
        normalized = Path(key).as_posix().casefold()
        if normalized in casefolded:
            gh_error(f"输出路径存在大小写变体碰撞，拒绝写入: {casefolded[normalized]} <-> {key}")
            sys.exit(1)
        casefolded[normalized] = key

    conflicts = []
    for key, group in groups.items():
        slugs = {source_repo_slug(t["url"]) for _i, t in group["members"]}
        if len(group["members"]) > 1 and len(slugs) > 1:
            conflicts.append((key, sorted(slugs)))
    if conflicts:
        for key, slugs in sorted(conflicts):
            warning(f"  输出路径仍被多来源共享 {key} <- {slugs}")

    ordered = []
    for key in sorted(groups):
        group = groups[key]
        if len(group["members"]) > 1:
            info(f"  同输出多源合并 {len(group['members'])} 个源 -> {group['path']}")
        ordered.append(group)
    return ordered


def _process_ip_group(all_lines):
    result, cidr_errors, stats = processor.process_ip_detailed(all_lines)
    for bad, why in cidr_errors.samples:
        warning(f"    无效 CIDR 已丢弃: {bad} -> {why}")
    if cidr_errors.count > len(cidr_errors.samples):
        warning(f"    ... 及其他 {cidr_errors.count - len(cidr_errors.samples)} 条无效 CIDR")

    stats["unrecognized"] += cidr_errors.count
    if stats["dropped_default_route"]:
        warning(f"    默认路由(/0) 已按设计丢弃: {stats['dropped_default_route']} 行")
    for type_name, count in sorted(stats["dropped_rule_type"].items()):
        warning(f"    不可表达规则类型被丢弃 [{type_name}]: {count} 行")
    return result, stats


def _unexpected_drop_count(stats):
    return sum(stats.get(key, 0) for key in _UNEXPECTED_DROP_KEYS) + sum(
        (stats.get("dropped_rule_type") or {}).values()
    )


def _unique_gate_lines(lines, rule_type, domain_kind="exact"):
    """以规范化语义识别重复候选，避免改写形式改变门禁分母。"""
    unique = set()
    result = []
    for line in lines:
        text = str(line)
        candidate = text
        if isinstance(line, processor._HostsField):
            key = (line.semantic, line.field.casefold())
            candidate = line
        elif rule_type == "ipcidr":
            kind, payload, _type_name = processor.classify_rule_line(text)
            candidate = payload if kind == "ip" else text
            candidate = re.split(r"[#!]", candidate, maxsplit=1)[0].strip()
            with contextlib.suppress(ValueError):
                candidate = str(ipaddress.ip_network(candidate, strict=False))
            key = candidate
        else:
            normalized, _stats = processor.process_domain_detailed([line], domain_kind)
            _prefix, payload, type_name = processor.classify_rule_line(text)
            if type_name in processor._OPAQUE_TYPES or type_name in processor._UNEXPRESSIBLE_IP_TYPES:
                key = text.casefold()
            else:
                key = tuple(normalized) if normalized else text.casefold()
        if key not in unique:
            unique.add(key)
            result.append(candidate)
    return result


def _gate_drops(unrecognized, unexpected, total, contributing, errors):
    """合计丢弃门禁：未识别 + 意外丢弃共用一个分子，阈值与绝对下限只应用一次。

    默认路由等“按设计丢弃”的类别不进入分子，也不进入分母（由调用方扣除）。
    """
    dropped = unrecognized + unexpected
    if not dropped:
        return True
    if total <= 0:
        sources_label = contributing[0] if contributing else "未知来源"
        errors["parse"].append((sources_label, "存在丢弃行但有效输入计数为 0，疑似上游格式变更"))
        return False
    ratio = dropped / total
    warning(f"    丢弃行合计: {dropped}/{total} ({ratio:.1%})（未识别 {unrecognized} + 意外丢弃 {unexpected}）")
    if ratio >= UNRECOGNIZED_WARN_RATIO and dropped >= UNRECOGNIZED_MIN_LINES:
        sources_label = contributing[0] + (f" 等 {len(contributing)} 个源" if len(contributing) > 1 else "")
        errors["parse"].append(
            (
                sources_label,
                f"丢弃行合计占比 {ratio:.1%} 超过阈值 {UNRECOGNIZED_WARN_RATIO:.0%}"
                f"（未识别 {unrecognized} + 意外丢弃 {unexpected} = {dropped}/{total}），疑似上游格式变更",
            )
        )
        return False
    if ratio >= UNRECOGNIZED_WARN_RATIO:
        warning(f"    占比超阈值但丢弃行合计只有 {dropped} 行（< {UNRECOGNIZED_MIN_LINES}），仅告警不阻断发布")
    return True


def process_group(group, raw_by_index, err_by_index=None, write_root=None):
    all_lines = []
    errors = {"download": [], "parse": []}
    contributing = []
    yaml_dropped = 0
    for idx, task in group["members"]:
        raw = raw_by_index.get(idx)
        if raw is None:
            errors["download"].append((task["url"], (err_by_index or {}).get(idx) or "下载失败"))
            continue
        try:
            parsed = processor.parse_lines(processor.safe_decode(raw))
        except Exception as e:
            errors["parse"].append((task["url"], f"解析失败: {e}"))
            continue
        yaml_errors = getattr(parsed, "yaml_errors", 0)
        if yaml_errors:
            message = f"YAML 非字符串或空规则候选 {yaml_errors} 项，已拒绝"
            if not ALLOW_PARTIAL:
                errors["parse"].append((task["url"], message))
                continue
            warning(f"    {task['url']}: {message}")
            yaml_dropped += yaml_errors
        # 表头只记录解析出非空行的源；规则最终是否保留由后续去重/门禁决定
        if parsed:
            all_lines.extend(parsed)
            contributing.append(task["url"])
        elif raw:
            errors["parse"].append((task["url"], "未解析出任何行（疑似空规则集或格式变更）"))

    all_lines = _unique_gate_lines(all_lines, group["type"], group.get("domain_kind", "exact"))
    if not all_lines:
        if not errors["parse"] and not errors["download"]:
            # 所有成员都是空响应：raw 为空时报一次汇总错误，避免静默通过
            if contributing:
                label = contributing[0]
            elif group["members"]:
                label = group["members"][0][1]["url"]
            else:
                label = group["path"]
            errors["parse"].append((label, "未解析出任何行（疑似空规则集或格式变更）"))
        return None, errors

    if group["type"] == "ipcidr":
        result, _special = _process_ip_group(all_lines)
        gate_total = len(all_lines) - _special.get("dropped_default_route", 0)
        gate_ok = _gate_drops(
            _special.get("unrecognized", 0) + yaml_dropped,
            _unexpected_drop_count(_special),
            gate_total + yaml_dropped,
            contributing,
            errors,
        )
    else:
        effective_kind = group.get("domain_kind", "exact")
        if effective_kind == "mixed":
            for _i, task in group["members"]:
                errors["parse"].append((task["url"], "同一输出混用 exact/suffix 语义来源，拒绝静默降级"))
            warning(f"    同一输出混用精确/后缀语义源，拒绝按单一语义输出: {group['path']}")
            return None, errors
        result, special = processor.process_domain_detailed(all_lines, effective_kind)
        if special.get("extracted_url"):
            info(f"    URL hostname 提取: {special['extracted_url']} 条")
        if special.get("suffix_promoted"):
            info(f"    后缀语义源: 提升 {special['suffix_promoted']} 条裸域名为 '+.' 形式")
        for key, label in (
            ("suffix", "后缀规则(+./domain:)已保留 +. 前缀"),
            ("subdomain", "仅子域规则(.d)已保留"),
            ("relaxed_exact", "精确规则(full:/host:/裸域名)按精确匹配输出"),
            ("wildcard", "通配符规则(*)已原样保留"),
            ("bare_single_label", "裸单标签条目按精确匹配保留"),
        ):
            if special.get(key):
                info(f"    {label}: {special[key]} 行")
        for key, label in (
            ("dropped_exception", "例外规则(@@)被丢弃"),
            ("dropped_keyword", "关键字/正则规则被丢弃"),
            ("dropped_modifier", "含 $ 修饰符的规则被丢弃"),
            ("dropped_path", "路径或不可无损表达的分隔符规则被丢弃"),
            ("ip_in_domain", "IP 规则出现在 domain 源中，已丢弃"),
        ):
            if special.get(key):
                warning(f"    {label}: {special[key]} 行")
        for type_name, count in sorted((special.get("dropped_rule_type") or {}).items()):
            warning(f"    不可表达规则类型被丢弃 [{type_name}]: {count} 行")

        gate_ok = _gate_drops(
            special.get("unrecognized", 0) + yaml_dropped,
            _unexpected_drop_count(special),
            len(all_lines) + yaml_dropped,
            contributing,
            errors,
        )

    if not gate_ok:
        return None, errors
    if not result:
        for _idx, task in group["members"]:
            errors["parse"].append((task["url"], "未解析出有效规则"))
        return None, errors

    header = [f"# Source: {url}" for url in contributing]
    final_path = ensure_within_rulesets(group["path"])
    output_path = final_path if write_root is None else _staging_output_path(write_root, final_path)
    atomic_write(output_path, header + result)
    return len(result), errors


class SyncStats:
    def __init__(self):
        self.success = 0
        self.download_errors = []
        self.parse_errors = []
        self.total_lines = 0
        self.start_time = time.time()

    def elapsed(self):
        return f"{time.time() - self.start_time:.1f}s"


_MARKER_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*)\s*:([^\]]*)\]\s*(?:#.*)?$")
_SOURCE_LINE_RE = re.compile(r"^(https://[^\s#]+)(?:\s+#.*)?$")


def _fail_marker(line_no, text, detail):
    gh_error(f"sources.urls:{line_no}: {detail}: {text!r}")
    sys.exit(1)


def parse_sources():
    tasks = []
    current_policy = "policy"
    current_type = "domain"
    current_domain_kind = "exact"

    if not os.path.exists(SOURCES_FILE):
        gh_error(f"文件 {SOURCES_FILE} 未找到！")
        sys.exit(1)

    with open(SOURCES_FILE, encoding="utf-8") as f:
        content = f.read().lstrip("\ufeff")

    for line_no, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        marker = _MARKER_RE.match(line)
        if marker:
            key = marker.group(1).lower()
            value = marker.group(2).strip()
            if key == "policy":
                current_policy = normalize_policy(safe_marker_value(value, "policy", line_no))
            elif key == "type":
                try:
                    current_type = normalize_type(safe_marker_value(value, "type", line_no))
                except ValueError as e:
                    _fail_marker(line_no, line, str(e))
            elif key == "domain-kind":
                kind = value.lower()
                if kind not in ("exact", "suffix"):
                    _fail_marker(line_no, line, "非法 [domain-kind:] 标记（仅允许 exact | suffix）")
                current_domain_kind = kind
            else:
                _fail_marker(line_no, line, f"无法识别的标记 [{key}:]（仅允许 policy / type / domain-kind）")
            continue

        if line.startswith("["):
            _fail_marker(
                line_no,
                line,
                "无法识别的标记（标记必须独占一行且形如 [policy:...] / [type:...] / [domain-kind:exact|suffix]）",
            )

        source = _SOURCE_LINE_RE.match(line)
        if not source:
            _fail_marker(
                line_no,
                redact_url_userinfo(line),
                "来源行必须是独占一行的 https:// URL（可在空白后加 # 尾随注释）",
            )
        validate_source_url(source.group(1), line_no, line)
        tasks.append(
            {
                "policy": current_policy,
                "type": current_type,
                "domain_kind": current_domain_kind,
                "url": source.group(1),
            }
        )

    return tasks


async def read_capped(stream):
    chunks = []
    total = 0
    async for chunk in stream.iter_chunked(65536):
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_delay(resp, attempt, backoff_budget):
    header = resp.headers.get("Retry-After") if resp.headers else None
    if header:
        requested = parse_retry_after(header, None, max_seconds=None)
        if requested is not None:
            if requested > MAX_RETRY_AFTER or requested > backoff_budget:
                return requested, True
            return requested, False
    return 1 * (attempt + 1), False


async def download_one(session, task):
    url = task["url"]
    backoff_budget = MAX_RETRY_AFTER * 2
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SOURCE_TOTAL_TIMEOUT

    def remaining():
        return deadline - loop.time()

    def expire(context):
        budget = remaining()
        warning(f"  {context}，单源总时限 {SOURCE_TOTAL_TIMEOUT:.0f}s 已用尽: {redact_url_userinfo(task['url'])}")
        return (task, None, _timeout_reason(budget))

    for attempt in range(RETRIES + 1):
        url = task["url"]
        redirects = 0
        delay = None
        try:
            while True:
                budget = remaining()
                if budget <= 0:
                    return expire("请求前")
                if not _is_https_url(url):
                    warning(f"  拒绝非 HTTPS 请求: {redact_url_userinfo(url)}")
                    return (task, None, "重定向到非 HTTPS 地址")
                try:
                    rejection = await asyncio.wait_for(check_request_target(session, url), budget)
                except TimeoutError:
                    return expire("目标地址校验")
                if rejection:
                    warning(f"  {rejection}")
                    return (task, None, rejection)
                # 校验耗时也属于单源总预算，ClientTimeout 必须用校验后的剩余预算
                budget = remaining()
                if budget <= 0:
                    return expire("目标地址校验后")
                headers = dict(auth_headers(url) or {}) if _same_host(url, task["url"]) else {}
                headers["User-Agent"] = USER_AGENT
                async with session.get(
                    url,
                    # 总超时严格取剩余预算（绝不越过单源 deadline），单次连接/读取仍受配置上限约束
                    timeout=aiohttp.ClientTimeout(
                        total=budget,
                        sock_connect=min(TIMEOUT, budget),
                        sock_read=min(TIMEOUT, budget),
                    ),
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in REDIRECT_STATUS:
                        location = resp.headers.get("Location")
                        if not location:
                            warning(f"  重定向缺少 Location: {redact_url_userinfo(url)} -> HTTP {resp.status}")
                            return (task, None, f"HTTP {resp.status}（重定向缺少 Location）")
                        next_url = urljoin(url, location)
                        if not _is_https_url(next_url):
                            warning(
                                f"  拒绝非 HTTPS 重定向: {redact_url_userinfo(url)}"
                                f" -> {redact_url_userinfo(next_url)}"
                            )
                            return (task, None, "重定向到非 HTTPS 地址")
                        if redirects >= MAX_REDIRECTS:
                            warning(f"  重定向次数超过 {MAX_REDIRECTS} 次: {redact_url_userinfo(task['url'])}")
                            return (task, None, f"重定向次数超过 {MAX_REDIRECTS} 次")
                        url = next_url
                        redirects += 1
                        continue

                    final_url = getattr(resp, "url", url)
                    history = getattr(resp, "history", ())
                    if not _is_https_url(final_url) or any(
                        not _is_https_url(getattr(item, "url", "")) for item in history
                    ):
                        warning(
                            f"  拒绝非 HTTPS 重定向: {redact_url_userinfo(url)}"
                            f" -> {redact_url_userinfo(final_url)}"
                        )
                        return (task, None, "重定向到非 HTTPS 地址")
                    if resp.status == 200:
                        content = await read_capped(resp.content)
                        if content is None:
                            warning(f"  响应超过 {MAX_SOURCE_BYTES} 字节上限: {redact_url_userinfo(url)}")
                            return (task, None, f"超过 {MAX_SOURCE_BYTES} 字节上限")
                        if not content:
                            warning(f"  空响应: {redact_url_userinfo(url)}")
                            return (task, None, "空响应")
                        # 全量 NUL 检查 + 前 64 KB 采样解码；后段无 NUL 的二进制会进入解析并被未识别门禁拦下
                        if b"\x00" in content or not processor.is_text_data(processor.safe_decode(content[:65536])):
                            warning(f"  非文本响应: {redact_url_userinfo(url)}")
                            return (task, None, "非文本响应")
                        return (task, content, None)

                    if 400 <= resp.status < 500 and resp.status not in RETRYABLE_STATUS:
                        warning(f"  下载失败 (不可重试): {redact_url_userinfo(url)} -> HTTP {resp.status}")
                        return (task, None, f"HTTP {resp.status}")

                    if attempt == RETRIES:
                        return (task, None, f"HTTP {resp.status}")

                    delay, exceeds_budget = _retry_delay(resp, attempt, backoff_budget)
                    if exceeds_budget:
                        warning(
                            f"  Retry-After {delay}s 超过退避上限/预算"
                            f"（{MAX_RETRY_AFTER}s / {backoff_budget}s），拒绝提前重试: {redact_url_userinfo(url)}"
                        )
                        return (task, None, f"HTTP {resp.status}（Retry-After {delay}s 超过退避预算）")
                    # 单次重试的退避等待也必须落在总时限内
                    if delay > remaining():
                        return expire(f"等待 {delay}s 退避")
                    warning(
                        f"  下载失败，{delay}s 后重试 [{attempt + 1}/{RETRIES + 1}]:"
                        f" {redact_url_userinfo(url)} -> HTTP {resp.status}"
                    )
                    break

            if delay:
                if delay > backoff_budget:
                    warning(f"  退避预算耗尽（{backoff_budget}s），放弃重试: {redact_url_userinfo(url)}")
                    return (task, None, f"HTTP {resp.status}（退避预算耗尽）")
                backoff_budget -= delay
                await asyncio.sleep(delay)

        except TargetResolutionRejected as e:
            warning(f"  目标地址校验失败: {redact_url_userinfo(url)} -> {e}")
            return (task, None, f"拒绝目标地址: {e}")
        except (TimeoutError, aiohttp.ClientError) as e:
            if attempt == RETRIES:
                warning(f"  下载失败 [{attempt + 1}/{RETRIES + 1}]: {redact_url_userinfo(url)} -> {e}")
                return (task, None, f"{type(e).__name__}: {e}")
            wait = 2 * (attempt + 1)
            if wait > remaining():
                return expire(f"等待 {wait}s 重试")
            await asyncio.sleep(wait)
        except Exception as e:
            warning(f"  下载异常: {redact_url_userinfo(url)} -> {type(e).__name__}: {e}")
            return (task, None, f"{type(e).__name__}: {e}")

    return (task, None, "未知错误")


async def download_all(tasks):
    # 来源下载不信任环境代理（trust_env=False）：代理侧解析会让目标地址校验失去意义。
    # 同时把连接器 resolver 包成公网白名单，任何一次实际解析（含预检后 DNS 重新解析）都会被校验。
    resolver = _PublicTargetResolver(aiohttp.DefaultResolver())
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=PER_HOST, resolver=resolver)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            max_field_size=16 * 1024,
        ) as session:
            coros = [download_one(session, t) for t in tasks]
            results = await asyncio.gather(*coros, return_exceptions=True)
    finally:
        # resolver 是外部注入的，aiohttp 不会代为关闭
        await resolver.close()

    normalized = []
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, (KeyboardInterrupt, SystemExit)):
            raise result
        if isinstance(result, Exception):
            normalized.append((task, None, f"{type(result).__name__}: {result}"))
        else:
            normalized.append(result)
    return normalized


def clean_orphans(expected_files):
    root = _resolved_rulesets_dir()
    group_start("清理孤儿文件")
    if not root.exists():
        group_end()
        return

    actual_files = {
        str(p)
        for p in root.rglob("*.txt")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }
    expected_set = {str(_anchored_path(f)) for f in expected_files}

    removed = 0
    for f in actual_files:
        if f not in expected_set:
            os.remove(f)
            debug(f"  已删除: {f}")
            removed += 1

    for dirpath, _, _ in os.walk(root, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    info(f"  共清理 {removed} 个孤儿文件")
    group_end()


def finalize_products(expected_files):
    root = _resolved_rulesets_dir()
    clean_orphans(expected_files)

    produced = manifest.collect_files(root)
    expected_rel = {str(_anchored_path(p).relative_to(root)).replace("\\", "/") for p in expected_files}
    if produced != expected_rel:
        gh_error(
            f"产物清单与磁盘不一致：缺失 {sorted(expected_rel - produced)}，"
            f"多出 {sorted(produced - expected_rel)}"
            "（若为 Windows 本地运行，请检查路径的大小写与结尾点号是否被文件系统规范化）"
        )
        sys.exit(1)

    hashes = {rel: file_sha256(str(root / rel)) for rel in sorted(produced)}
    generation = manifest.current_generation()
    manifest_path = ensure_within_rulesets(MANIFEST_FILE)
    manifest.save_manifest(manifest_path, hashes, generation=generation)
    info(f"  产物清单已写入 {MANIFEST_FILE}（{len(produced)} 项，generation={generation}）")
    return produced


def _md_cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def generate_summary(stats, degraded_reasons=None):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    dl_fail = len(stats.download_errors)
    parse_fail = len(stats.parse_errors)
    total_fail = dl_fail + parse_fail

    section(f"同步报告 | 成功:{stats.success} 失败:{total_fail} | 耗时:{stats.elapsed()}")

    if stats.download_errors:
        warning(f"  下载失败 ({dl_fail}):")
        for url, reason in stats.download_errors:
            warning(f"    L {url} | {reason}")

    if stats.parse_errors:
        warning(f"  解析失败 ({parse_fail}):")
        for url, reason in stats.parse_errors:
            warning(f"    L {url} | {reason}")

    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("# 规则同步仪表盘\n\n")
        if degraded_reasons:
            f.write("### 本次未发布（消费者继续使用上一版产物）\n\n")
            for reason in degraded_reasons:
                f.write(f"- {_md_cell(reason)}\n")
            f.write("\n")
        f.write("| 成功 | 失败 | 总规则数 |\n")
        f.write("| :---: | :---: | :---: |\n")
        f.write(f"| **{stats.success}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

        if dl_fail > 0:
            f.write("### 下载失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.download_errors:
                f.write(f"| `{_md_cell(url)}` | {_md_cell(reason)} |\n")
            f.write("\n")

        if parse_fail > 0:
            f.write("### 解析失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.parse_errors:
                f.write(f"| `{_md_cell(url)}` | {_md_cell(reason)} |\n")
            f.write("\n")

        if total_fail == 0:
            f.write("### 全部源同步正常\n\n")

        f.write(f"\n_生成时间: {beijing_now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间)_\n")


def main():
    with _sync_lock(_resolved_rulesets_dir()):
        _main_locked()


def _main_locked():
    stats = SyncStats()

    group_start("初始化")
    root = _resolved_rulesets_dir()
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_staging(root)
    info(f"  超时:{TIMEOUT}s | 重试:{RETRIES}次")
    tasks = parse_sources()
    info(f"  加载 {len(tasks)} 个上游源")
    group_end()

    group_start(f"并发下载 ({len(tasks)} 源)")
    results = asyncio.run(download_all(tasks))
    info("  下载完成")
    group_end()

    groups = plan_groups(tasks)
    raw_by_index = {}
    err_by_index = {}
    for idx, (_task, raw_bytes, err) in enumerate(results):
        if raw_bytes is not None:
            raw_by_index[idx] = raw_bytes
        elif err:
            err_by_index[idx] = err

    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(root)))
    try:
        expected_files = []
        for group in groups:
            count, errors = process_group(group, raw_by_index, err_by_index, write_root=staging)
            for url, reason in errors["download"]:
                stats.download_errors.append((url, reason))
                warning(f"  下载失败: {url} | {reason}")
            for url, reason in errors["parse"]:
                stats.parse_errors.append((url, reason))
                warning(f"  解析失败: {url} | {reason}")
            if count is None:
                warning(f"  跳过（无可用规则）: {group['path']}")
                continue
            expected_files.append(Path(group["path"]))
            stats.success += 1
            stats.total_lines += count
            label = f"[{group['policy']}/{group['type']}] {Path(group['path']).name}"
            success(f"  {label} -> {count} 条规则")

        degraded = stats.success == 0
        degraded_reasons = []
        if degraded:
            degraded_reasons.append(f"没有任何可用产物（成功 0/{len(groups)}）")
        if not degraded and groups:
            ratio = stats.success / len(groups)
            if MIN_SUCCESS_RATIO > 0 and ratio < MIN_SUCCESS_RATIO:
                degraded = True
                degraded_reasons.append(
                    f"源成功率 {ratio:.1%} 低于门禁 {MIN_SUCCESS_RATIO:.0%}（成功 {stats.success}/{len(groups)}）"
                )
                gh_error(
                    f"源成功率 {ratio:.1%} 低于门禁 {MIN_SUCCESS_RATIO:.0%}（成功 {stats.success}/{len(groups)}）"
                )
            partial_failures = bool(stats.download_errors or stats.parse_errors)
            if partial_failures and not ALLOW_PARTIAL:
                degraded = True
                degraded_reasons.append(
                    f"存在失败源（下载 {len(stats.download_errors)} / "
                    f"解析 {len(stats.parse_errors)}）且 behavior.allow_partial=false"
                )
                gh_error(
                    f"存在失败源（下载 {len(stats.download_errors)} / 解析 {len(stats.parse_errors)}），"
                    "behavior.allow_partial=false 时拒绝发布部分产物"
                )

        if degraded:
            info("  本次不发布，保留上一版产物：")
            for reason in degraded_reasons:
                info(f"    - {reason}")
            summary_file = _resolved_rulesets_dir() / "sync-summary.txt"
            summary_file.write_text(
                "# 同步摘要\n"
                f"# 时间: {beijing_timestamp()}\n"
                f"# 成功: {stats.success} "
                f"失败: {len(stats.download_errors) + len(stats.parse_errors)}\n"
                f"# 原因: {'；'.join(degraded_reasons)}\n",
                encoding="utf-8",
            )
            info("  已跳过孤儿文件清理（避免在降级状态下清空已有产物）")
            generate_summary(stats, degraded_reasons)
            gh_error(
                f"同步未发布（成功 {stats.success}/{len(groups)}）："
                f"{'；'.join(degraded_reasons)}。请检查 sources.urls 与上游可达性；"
                "如需接受部分产物请设 behavior.allow_partial=true（或调低 "
                "behavior.min_source_success_ratio）"
            )
            sys.exit(1)

        commit_staged_products(staging, expected_files)
        finalize_products(expected_files)
        generate_summary(stats)
    finally:
        if staging.exists() and not list(root.glob(".rollback-*")):
            shutil.rmtree(staging, ignore_errors=True)

    info(
        f"\n同步完成: {stats.success} 个输出成功, "
        f"{len(stats.download_errors)} 下载失败, {len(stats.parse_errors)} 解析失败"
    )


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
