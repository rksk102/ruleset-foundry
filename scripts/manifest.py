import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from utils import atomic_write, beijing_timestamp, file_sha256

MANIFEST_NAME = "products.manifest"
MANIFEST_MAGIC = "# ruleset-foundry products manifest v2"
_MANIFEST_LINE_RE = re.compile(r"^(?P<sha>[0-9a-f]{64})\s+(?P<path>\S.*)$")
_GENERATION_PREFIX = "# generation: "


class ManifestError(Exception):
    pass


def _validate_entry_path(rel):
    if not isinstance(rel, str):
        raise ManifestError(f"产物清单路径必须是字符串: {rel!r}")
    normalized = rel.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ManifestError(f"产物清单路径非法或越界: {rel!r}")
    return normalized


@dataclass(frozen=True)
class Manifest:
    generation: str
    entries: dict[str, str]

    @property
    def paths(self):
        return tuple(sorted(self.entries))


def _github_generation():
    run_id = (os.getenv("GITHUB_RUN_ID") or "").strip()
    if not run_id:
        return None
    attempt = (os.getenv("GITHUB_RUN_ATTEMPT") or "").strip()
    return f"github-{run_id}-attempt-{attempt}" if attempt else f"github-{run_id}"


def current_generation():
    return _github_generation() or f"local-{beijing_timestamp()}-{uuid.uuid4().hex[:8]}"


def expected_generation():
    return _github_generation()


def load_manifest(path):
    p = Path(path)
    if not p.exists():
        return None
    header_seen = False
    generation = None
    entries = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if s == MANIFEST_MAGIC:
                header_seen = True
            elif s.startswith(_GENERATION_PREFIX):
                generation = s[len(_GENERATION_PREFIX) :].strip()
            continue
        if not header_seen:
            raise ManifestError(f"不支持的产物清单格式（旧版或缺版本头）: {p}；请重新运行对应构建步骤")
        match = _MANIFEST_LINE_RE.fullmatch(s)
        if not match:
            raise ManifestError(f"产物清单条目非法: {s!r}（{p}）")
        try:
            rel = _validate_entry_path(match.group("path"))
        except ManifestError as e:
            raise ManifestError(f"{e}（{p}）") from e
        if rel in entries:
            raise ManifestError(f"产物清单重复条目: {rel!r}（{p}）")
        entries[rel] = match.group("sha")
    if not header_seen:
        raise ManifestError(f"不支持的产物清单格式（旧版或缺版本头）: {p}；请重新运行对应构建步骤")
    if not generation:
        raise ManifestError(f"产物清单缺少 generation: {p}")
    return Manifest(generation, entries)


def save_manifest(path, entries, generation=None):
    p = Path(path)
    if not isinstance(entries, dict):
        raise ManifestError("save_manifest 需要 path -> sha256 的映射")
    normalized_entries = {}
    for rel, digest in entries.items():
        rel = _validate_entry_path(rel)
        if not _MANIFEST_LINE_RE.fullmatch(f"{digest}  {rel}"):
            raise ManifestError(f"产物清单条目非法: {rel!r} -> {digest!r}")
        if rel in normalized_entries:
            raise ManifestError(f"产物清单重复条目: {rel!r}")
        normalized_entries[rel] = digest
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [MANIFEST_MAGIC, f"{_GENERATION_PREFIX}{generation or current_generation()}"]
    lines.extend(f"{normalized_entries[rel]}  {rel}" for rel in sorted(normalized_entries))
    atomic_write(str(p), lines)
    return p


def collect_files(root, suffix=".txt"):
    root_path = Path(root)
    if not root_path.exists():
        return set()
    files = set()
    for p in root_path.rglob(f"*{suffix}"):
        if not p.is_file():
            continue
        rel = p.relative_to(root_path)
        if any(part.startswith(".") for part in rel.parts):
            continue
        files.add(str(rel).replace("\\", "/"))
    return files


def diff_against(manifest_entries, actual_rel_paths):
    expected = {e.replace("\\", "/") for e in (manifest_entries or [])}
    actual = {a.replace("\\", "/") for a in (actual_rel_paths or [])}
    return sorted(expected - actual), sorted(actual - expected)


def verify_matches(manifest_entries, actual_rel_paths, context):
    if manifest_entries is None:
        raise ManifestError(f"{context}: 缺少产物清单基准，无法校验")
    missing, extra = diff_against(manifest_entries, actual_rel_paths)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"缺失 {len(missing)}: {missing[:5]}")
        if extra:
            detail.append(f"多出 {len(extra)}: {extra[:5]}")
        raise ManifestError(f"{context}: 产物清单不一致（{'；'.join(detail)}）")
    return True


def verify_manifest(manifest_obj, root, context, suffix=".txt"):
    if manifest_obj is None:
        raise ManifestError(f"{context}: 缺少产物清单")
    actual = collect_files(root, suffix)
    verify_matches(manifest_obj.entries.keys(), actual, context)
    root_path = Path(root)
    mismatched = [
        rel for rel, digest in sorted(manifest_obj.entries.items()) if file_sha256(str(root_path / rel)) != digest
    ]
    if mismatched:
        raise ManifestError(f"{context}: 文件哈希与清单不一致（{mismatched[:5]}）")
    return True


def hash_entries(root, rel_paths):
    root_path = Path(root)
    return {rel: file_sha256(str(root_path / rel)) for rel in sorted(rel_paths)}


def merge_inputs(merge_tasks, base_dir):
    missing = []
    for task in merge_tasks or []:
        for rel in task.get("inputs") or []:
            candidate = Path(base_dir) / rel
            if not candidate.exists():
                missing.append(rel.replace("\\", "/"))
    return sorted(set(missing))
