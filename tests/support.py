import ctypes
import ctypes.util
import json
import struct
from pathlib import Path

import gen_readme


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_paired_product(root, rel, text=b"x"):
    relative = root / "merged-rules" / rel
    relative.parent.mkdir(parents=True, exist_ok=True)
    relative.write_bytes(text)
    mrs = root / "merged-rules-mrs" / Path(rel).with_suffix(".mrs")
    mrs.parent.mkdir(parents=True, exist_ok=True)
    mrs.write_bytes(valid_mrs_bytes("ipcidr" if "/ipcidr/" in rel else "domain"))


def isolate_readme_paths(monkeypatch, root, readme, rules_rel="merged-rules", mrs_rel="merged-rules-mrs"):
    monkeypatch.setattr(gen_readme, "REPO_ROOT", str(root))
    monkeypatch.setattr(gen_readme, "README_FILE", str(readme))
    monkeypatch.setattr(gen_readme, "DIR_RULES", str(root / rules_rel))
    monkeypatch.setattr(gen_readme, "DIR_MRS", str(root / mrs_rel))


def write_quickstart_mrs_placeholders(root):
    for task in gen_readme.quickstart_merge_tasks():
        path = root / gen_readme.quickstart_mrs_relpath(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MRS fixture")


def valid_mrs_bytes(behavior="domain"):
    return compress_zstd(b"MRS\x01" + bytes([0 if behavior == "domain" else 1]) + struct.pack(">qq", 1, 0) + b"rules")


def compress_zstd(payload):
    lib = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
    lib.ZSTD_compressBound.argtypes = (ctypes.c_size_t,)
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    )
    lib.ZSTD_compress.restype = ctypes.c_size_t
    target = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(payload)))
    size = lib.ZSTD_compress(target, len(target), payload, len(payload), 1)
    assert size < len(target)
    return target.raw[:size]


class GhStub:
    def __init__(
        self,
        fail_when=None,
        responses=None,
        asset_names=None,
        release_exists=True,
        latest_tag=None,
        asset_digests=None,
        sha256sums=None,
    ):
        self.calls = []
        self.fail_when = fail_when or (lambda cmd: False)
        self.responses = responses or {}
        self.asset_names = asset_names
        self.release_exists = release_exists
        self.latest_tag = latest_tag
        self.asset_digests = asset_digests
        self.sha256sums = sha256sums

    def __call__(self, cmd, strip=True):
        self.calls.append(list(cmd))
        if self.fail_when(cmd):
            return None
        if cmd[0] == "release" and len(cmd) > 1 and cmd[1] in ("create", "upload"):
            self.release_exists = True
            self.asset_names = None  # 发布成功后恢复为完整集合推导
        if cmd[0] == "api" and any("/releases/tags/" in part for part in cmd):
            if self.asset_digests is None:
                return None
            return "\n".join(f"{name}\t{digest}" for name, digest in self.asset_digests.items())
        if cmd[0] == "release" and len(cmd) > 1 and cmd[1] == "download":
            return self.sha256sums
        key = (cmd[0], cmd[1]) if len(cmd) > 1 else (cmd[0],)
        if key in self.responses:
            return self.responses[key]
        if key == ("release", "list"):
            if self.latest_tag:
                return json.dumps([{"tagName": self.latest_tag, "isLatest": True}])
            return "[]"
        if key == ("release", "view"):
            if "assets" in cmd:
                if not self.release_exists:
                    return None
                if self.asset_names is not None:
                    return self.asset_names
                date = cmd[2].removeprefix("rules-")
                return f"merged-rules-{date}.zip\nSHA256SUMS"
            return '{"tagName": "rules-x"}' if self.release_exists else None
        return "ok"

    def actions(self):
        return [tuple(call[:2]) for call in self.calls]

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def release_actions(self):
        return [call[1] for call in self.calls if call[0] == "release" and len(call) > 1]
