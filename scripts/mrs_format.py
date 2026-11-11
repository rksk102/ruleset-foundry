import ctypes
import ctypes.util
import struct
from pathlib import Path

MRS_MAGIC = b"MRS\x01"
MRS_HEADER_SIZE = 21
MRS_BEHAVIORS = {"domain": 0, "ipcidr": 1}
CHUNK_SIZE = 64 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_EXTRA_BYTES = 1024 * 1024


class MrsFormatError(ValueError):
    pass


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
    for _ in range(count):
        reader.read_exact(width)


def _validate_body(reader, behavior):
    if reader.read_exact(1) != b"\x01":
        raise MrsFormatError("MRS 主体版本非法")
    if behavior == "ipcidr":
        _records(reader, _int64(reader), 32)
        return
    leaves = _int64(reader)
    _records(reader, leaves, 8)
    bitmap = _int64(reader)
    _records(reader, bitmap, 8)
    labels = _int64(reader)
    if labels < 1 or labels > MAX_DECOMPRESSED_BYTES:
        raise MrsFormatError("MRS 标签长度非法")
    reader.read_exact(labels)


def validate_mrs(path, behavior):
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
        _validate_body(reader, behavior)
        reader.finish()
        return count
    except (OSError, struct.error, MrsFormatError) as exc:
        raise MrsFormatError(f"MRS 校验失败: {path}（{exc}）") from exc
    finally:
        if reader is not None:
            reader.close()
