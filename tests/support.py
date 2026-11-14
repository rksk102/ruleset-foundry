import ctypes
import ctypes.util
import ipaddress
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
    behavior = "ipcidr" if "/ipcidr/" in rel else "domain"
    if text == b"x":
        encoded = valid_mrs_bytes(behavior)
    elif behavior == "domain":
        encoded = domain_mrs_bytes(text.decode().splitlines())
    else:
        encoded = ipcidr_mrs_bytes(text.decode().splitlines())
    mrs.write_bytes(encoded)


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


def domain_mrs_bytes(rules, count=None):
    keys = set()
    for rule in rules:
        normalized = rule.lower()
        if normalized.startswith("+."):
            keys.add(normalized[2:][::-1].encode())
            keys.add(normalized[::-1].encode())
        elif normalized.startswith("."):
            keys.add(("+." + normalized[1:])[::-1].encode())
        else:
            keys.add(normalized[::-1].encode())
    keys = sorted(keys)
    leaves = []
    bitmap = []
    labels = []
    queue = [(0, len(keys))]
    depth = 0
    while queue:
        next_queue = []
        for start, end in queue:
            terminal = depth == len(keys[start])
            leaves.append(terminal)
            if terminal:
                start += 1
            while start < end:
                finish = start + 1
                while finish < end and keys[finish][depth] == keys[start][depth]:
                    finish += 1
                next_queue.append((start, finish))
                labels.append(keys[start][depth : depth + 1])
                bitmap.append(False)
                start = finish
            bitmap.append(True)
        queue = next_queue
        depth += 1

    def pack_bits(bits):
        return b"".join(
            sum((1 << offset) for offset, bit in enumerate(bits[index : index + 64]) if bit).to_bytes(8, "big")
            for index in range(0, len(bits), 64)
        )

    body = (
        b"\x01"
        + struct.pack(">q", (len(leaves) + 63) // 64)
        + pack_bits(leaves)
        + struct.pack(">q", (len(bitmap) + 63) // 64)
        + pack_bits(bitmap)
        + struct.pack(">q", sum(map(len, labels)))
        + b"".join(labels)
    )
    return compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", len(rules) if count is None else count, 0) + body)


def ipcidr_mrs_bytes(networks, count=None):
    ranges = []
    for network in sorted(map(ipaddress.ip_network, networks), key=lambda n: (n.version, int(n.network_address))):
        start = network.network_address.ipv6_mapped if network.version == 4 else network.network_address
        end = network.broadcast_address.ipv6_mapped if network.version == 4 else network.broadcast_address
        ranges.append(start.packed + end.packed)
    body = b"\x01" + struct.pack(">q", len(ranges)) + b"".join(ranges)
    return compress_zstd(b"MRS\x01\x01" + struct.pack(">qq", len(networks) if count is None else count, 0) + body)


def valid_mrs_bytes(behavior="domain"):
    if behavior == "domain":
        return domain_mrs_bytes(["x"])
    if behavior == "ipcidr":
        return ipcidr_mrs_bytes(["0.0.0.0/0"])
    raise ValueError(f"unsupported behavior: {behavior}")


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
