from pathlib import Path

from utils import atomic_write


class ManifestError(Exception):
    pass


def load_manifest(path):
    p = Path(path)
    if not p.exists():
        return None
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        entries.append(s.replace("\\", "/"))
    return entries


def save_manifest(path, entries):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted({e.replace("\\", "/") for e in entries})) + "\n"
    atomic_write(str(p), body)
    return p


def collect_files(root, suffix=".txt"):
    root_path = Path(root)
    if not root_path.exists():
        return set()
    return {
        str(p.relative_to(root_path)).replace("\\", "/")
        for p in root_path.rglob(f"*{suffix}")
        if p.is_file() and not p.name.startswith(".")
    }


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


def merge_inputs(merge_tasks, base_dir):
    missing = []
    for task in merge_tasks or []:
        for rel in task.get("inputs") or []:
            candidate = Path(base_dir) / rel
            if not candidate.exists():
                missing.append(rel.replace("\\", "/"))
    return sorted(set(missing))
