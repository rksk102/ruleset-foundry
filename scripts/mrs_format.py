import ctypes
import ctypes.util
import ipaddress
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path

MRS_MAGIC = b"MRS\x01"
MRS_HEADER_SIZE = 21
MRS_BEHAVIORS = {"domain": 0, "ipcidr": 1}
CHUNK_SIZE = 64 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_EXTRA_BYTES = 1024 * 1024
# 域名键由 LOUDS 标签逐层展开；限制单键长度与键总量，避免小体积畸形文件放大成 GB 级内存。
MAX_DOMAIN_KEY_BYTES = 255
MAX_DOMAIN_KEYS_BYTES = MAX_DECOMPRESSED_BYTES


class MrsFormatError(ValueError):
    pass


@dataclass(frozen=True)
class MrsContent:
    count: int
    keys: frozenset
    ranges: tuple


class _ZstdInput(ctypes.Structure):
    _fields_ = (("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t))


class _ZstdOutput(ctypes.Structure):
    _fields_ = (("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t))


def _load_zstd():
    name = ctypes.util.find_library("zstd") or "libzstd.so.1"
    try:
        lib = ctypes.CDLL(name)
        lib.ZSTD_createDStream.argtypes = ()
        lib.ZSTD_createDStream.restype = ctypes.c_void_p
        lib.ZSTD_freeDStream.argtypes = (ctypes.c_void_p,)
        lib.ZSTD_freeDStream.restype = ctypes.c_size_t
        lib.ZSTD_decompressStream.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        lib.ZSTD_decompressStream.restype = ctypes.c_size_t
        lib.ZSTD_isError.argtypes = (ctypes.c_size_t,)
        lib.ZSTD_isError.restype = ctypes.c_uint
    except (AttributeError, OSError) as exc:
        raise MrsFormatError(f"无法加载 libzstd: {exc}") from exc
    return lib


class _MrsReader:
    """用固定缓冲区消费一个 zstd 帧；只暴露解析器当前请求的字节。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lib = _load_zstd()
        self.stream = self.lib.ZSTD_createDStream()
        if not self.stream:
            raise MrsFormatError("无法创建 libzstd 解压上下文")
        self.file = None
        self.input_buffer = None
        self.compressed_bytes = 0
        self.input = _ZstdInput()
        self.output_buffer = ctypes.create_string_buffer(CHUNK_SIZE)
        self.output = _ZstdOutput(ctypes.addressof(self.output_buffer), CHUNK_SIZE, 0)
        self._read_pos = 0
        self.decompressed_bytes = 0
        self.finished = False

    def close(self):
        if self.file is not None:
            self.file.close()
        self.lib.ZSTD_freeDStream(self.stream)

    def _refill(self):
        if self.input.pos < self.input.size:
            return True
        chunk = self.path.open("rb") if self.file is None else self.file
        self.file = chunk
        data = chunk.read(CHUNK_SIZE)
        if not data:
            return False
        self.compressed_bytes += len(data)
        if self.compressed_bytes > MAX_COMPRESSED_BYTES:
            raise MrsFormatError("MRS 读取超过压缩大小上限")
        self.input_buffer = ctypes.create_string_buffer(data, len(data))
        self.input = _ZstdInput(ctypes.addressof(self.input_buffer), len(data), 0)
        return True

    def _produce(self):
        while self._read_pos == self.output.pos and not self.finished:
            if not self._refill():
                raise MrsFormatError("MRS zstd 压缩帧不完整")
            self.output.pos = 0
            self._read_pos = 0
            result = self.lib.ZSTD_decompressStream(self.stream, ctypes.byref(self.output), ctypes.byref(self.input))
            if self.lib.ZSTD_isError(result):
                raise MrsFormatError("MRS zstd 压缩帧无效")
            self.decompressed_bytes += self.output.pos
            if self.decompressed_bytes > MAX_DECOMPRESSED_BYTES:
                raise MrsFormatError("MRS 解压后超过结构校验上限")
            self.finished = result == 0
            if self.finished and self.input.pos != self.input.size:
                raise MrsFormatError("MRS 含尾随压缩数据或额外帧")

    def read_exact(self, size):
        target = bytearray()
        while len(target) < size:
            self._produce()
            if self._read_pos == self.output.pos:
                if self.finished:
                    raise MrsFormatError("MRS 解压主体不完整")
                continue
            take = min(size - len(target), self.output.pos - self._read_pos)
            target.extend(self.output_buffer.raw[self._read_pos : self._read_pos + take])
            self._read_pos += take
        return bytes(target)

    def finish(self):
        self._produce()
        if not self.finished:
            raise MrsFormatError("MRS zstd 压缩帧未完整结束")
        if self._read_pos != self.output.pos:
            raise MrsFormatError("MRS 主体含额外数据")
        if self._refill():
            raise MrsFormatError("MRS 含尾随压缩数据")


def _int64(reader):
    return struct.unpack(">q", reader.read_exact(8))[0]


def _records(reader, count, width):
    if count < 1 or count > MAX_DECOMPRESSED_BYTES // width:
        raise MrsFormatError("MRS 主体记录数非法")
    return b"".join(reader.read_exact(width) for _ in range(count))


def _bit(data, index):
    word, offset = divmod(index, 64)
    return bool(data[word * 8 + 7 - offset // 8] & (1 << (offset % 8)))


def _bit_count(data):
    return sum(int.from_bytes(data[offset : offset + 8], "big").bit_count() for offset in range(0, len(data), 8))


def _has_bit_from(data, start):
    word, offset = divmod(start, 64)
    for index in range(word, len(data) // 8):
        value = int.from_bytes(data[index * 8 : index * 8 + 8], "big")
        if index == word:
            value &= ~((1 << offset) - 1)
        if value:
            return True
    return False


def _validate_domain_tree(leaves, bitmap, labels):
    if not bitmap:
        raise MrsFormatError("MRS 标签位图为空")
    if not _bit_count(leaves):
        raise MrsFormatError("MRS 没有有效叶节点")
    if _bit_count(bitmap) > len(labels) + 1:
        raise MrsFormatError("MRS 标签位图与标签内容长度不一致")
    queue = deque([b""])
    keys = set()
    label_offset = 0
    index = 0
    node_count = 1
    node = 0
    total_key_bytes = 0
    queue_bytes = 0
    while node < node_count:
        if index >= len(bitmap) * 8:
            raise MrsFormatError("MRS LOUDS 树节点终止位不足")
        node_key = queue.popleft()
        queue_bytes -= len(node_key)
        if _bit(leaves, node):
            keys.add(node_key)
            total_key_bytes += len(node_key)
            if total_key_bytes > MAX_DOMAIN_KEYS_BYTES:
                raise MrsFormatError("MRS 域名键总量超过校验上限")
        child_labels = set()
        while index < len(bitmap) * 8 and not _bit(bitmap, index):
            if label_offset >= len(labels):
                raise MrsFormatError("MRS LOUDS 树标签越界")
            child = index - node + 1
            if child != node_count or child >= len(leaves) * 8:
                raise MrsFormatError("MRS LOUDS 树子节点边界非法")
            label = labels[label_offset : label_offset + 1]
            if label in child_labels:
                raise MrsFormatError("MRS LOUDS 树同一节点标签重复")
            child_labels.add(label)
            new_key = node_key + label
            if len(new_key) > MAX_DOMAIN_KEY_BYTES:
                raise MrsFormatError("MRS 域名键超过长度上限")
            queue_bytes += len(new_key)
            if queue_bytes > MAX_DOMAIN_KEYS_BYTES:
                raise MrsFormatError("MRS 域名键工作集超过校验上限")
            node_count += 1
            queue.append(new_key)
            label_offset += 1
            index += 1
        if index >= len(bitmap) * 8 or not _bit(bitmap, index):
            raise MrsFormatError("MRS LOUDS 树未闭合")
        index += 1
        node += 1
    if label_offset != len(labels) or node_count != _bit_count(bitmap):
        raise MrsFormatError("MRS LOUDS 树含不可达标签或位图数据")
    if _has_bit_from(leaves, node_count):
        raise MrsFormatError("MRS 终结位越出 LOUDS 树节点边界")
    return frozenset(keys)


def _unmap_ip(address):
    """MRS 的 16 字节地址以 IPv4-mapped IPv6 保存 IPv4；TXT/MRS 比较前统一还原。"""
    return address.ipv4_mapped or address if address.version == 6 else address


def _validate_body(reader, behavior):
    if reader.read_exact(1) != b"\x01":
        raise MrsFormatError("MRS 主体版本非法")
    if behavior == "ipcidr":
        records = _records(reader, _int64(reader), 32)
        ranges = []
        for offset in range(0, len(records), 32):
            start = _unmap_ip(ipaddress.ip_address(records[offset : offset + 16]))
            end = _unmap_ip(ipaddress.ip_address(records[offset + 16 : offset + 32]))
            if start.version != end.version or start > end:
                raise MrsFormatError("MRS IP range 地址族或起止顺序无效")
            ranges.append((int(start), int(end), start.version))
        return frozenset(), tuple(ranges)
    leaves = _records(reader, _int64(reader), 8)
    bitmap = _records(reader, _int64(reader), 8)
    labels_length = _int64(reader)
    if labels_length < 1 or labels_length > MAX_DECOMPRESSED_BYTES:
        raise MrsFormatError("MRS 标签长度非法")
    labels = reader.read_exact(labels_length)
    return _validate_domain_tree(leaves, bitmap, labels), ()


def read_mrs(path, behavior):
    expected_behavior = MRS_BEHAVIORS.get(behavior)
    if expected_behavior is None:
        raise MrsFormatError(f"MRS 不支持行为: {behavior!r}")
    reader = None
    try:
        target = Path(path)
        if not target.is_file() or target.is_symlink() or target.stat().st_size > MAX_COMPRESSED_BYTES:
            raise MrsFormatError(f"MRS 文件非法或超过压缩大小上限: {path}")
        reader = _MrsReader(target)
        payload = reader.read_exact(MRS_HEADER_SIZE)
        if payload[:4] != MRS_MAGIC:
            raise MrsFormatError(f"MRS 头部无效: {path}")
        count, extra_length = struct.unpack(">qq", payload[5:MRS_HEADER_SIZE])
        if payload[4] != expected_behavior:
            raise MrsFormatError(f"MRS 行为与 {behavior} 不匹配: {path}")
        if count <= 0 or extra_length < 0 or extra_length > MAX_EXTRA_BYTES:
            raise MrsFormatError(f"MRS 计数或扩展长度无效: {path}")
        reader.read_exact(extra_length)
        keys, ranges = _validate_body(reader, behavior)
        reader.finish()
        return MrsContent(count, keys, ranges)
    except (OSError, struct.error, MrsFormatError) as exc:
        raise MrsFormatError(f"MRS 校验失败: {path}（{exc}）") from exc
    finally:
        if reader is not None:
            reader.close()


def validate_mrs(path, behavior):
    return read_mrs(path, behavior).count


def _domain_text_key(rule):
    if not rule or rule != rule.strip() or rule.endswith(".") or "/" in rule:
        raise MrsFormatError(f"TXT 含内核无法接受的域名规则: {rule!r}")
    parts = rule.lower().split(".")
    if any(not part for part in parts[1:]):
        raise MrsFormatError(f"TXT 含空域名标签: {rule!r}")
    if len(parts) == 1 and not parts[0]:
        raise MrsFormatError("TXT 含空域名")
    for index, part in enumerate(parts):
        if "+" in part and (part != "+" or index != 0 or len(parts) == 1):
            raise MrsFormatError(f"TXT 含非法 + 通配符: {rule!r}")
        if "*" in part and part != "*":
            raise MrsFormatError(f"TXT 通配符必须占据完整标签: {rule!r}")
    if parts[0] == "+":
        return frozenset((rule[2:].lower()[::-1].encode(), rule.lower()[::-1].encode()))
    if parts[0] == "":
        return frozenset((("+." + rule[1:]).lower()[::-1].encode(),))
    return frozenset((rule.lower()[::-1].encode(),))


def _text_network(rule):
    try:
        network = ipaddress.ip_network(rule, strict=False)
    except ValueError as exc:
        raise MrsFormatError(f"TXT 含无效 CIDR: {rule!r}") from exc
    start = _unmap_ip(network.network_address)
    end = _unmap_ip(network.broadcast_address)
    if start.version != end.version:
        raise MrsFormatError(f"TXT 含无法归一化的地址族: {rule!r}")
    return int(start), int(end), start.version


def _merge_ranges(ranges):
    merged = []
    for start, end, version in sorted(ranges, key=lambda item: (item[2], item[0], item[1])):
        if merged and merged[-1][2] == version and start <= merged[-1][1] + 1:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), version)
        else:
            merged.append((start, end, version))
    return tuple(merged)


def verify_product_pair(txt_path, mrs_path, behavior):
    try:
        lines = Path(txt_path).read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise MrsFormatError(f"TXT 不是有效 UTF-8: {txt_path}") from exc
    except OSError as exc:
        raise MrsFormatError(f"TXT 读取失败: {txt_path}") from exc
    actual = read_mrs(mrs_path, behavior)
    if behavior == "domain":
        keys = set()
        count = 0
        for line in lines:
            rule = line.strip()
            if not rule or rule.startswith("#") or rule.startswith("//"):
                continue
            keys.update(_domain_text_key(rule))
            count += 1
        if count != actual.count or keys != actual.keys:
            raise MrsFormatError(f"TXT/MRS 内容不对应: {txt_path} <-> {mrs_path}")
        return
    networks = []
    count = 0
    for line in lines:
        rule = line.strip()
        if not rule or rule.startswith("#") or rule.startswith("//"):
            continue
        networks.append(_text_network(rule))
        count += 1
    if count != actual.count or _merge_ranges(networks) != actual.ranges:
        raise MrsFormatError(f"TXT/MRS 内容不对应: {txt_path} <-> {mrs_path}")
