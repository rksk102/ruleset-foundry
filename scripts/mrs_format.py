import ctypes
import ctypes.util
import struct
from pathlib import Path

MRS_MAGIC = b"MRS\x01"
MRS_HEADER_SIZE = 21
MRS_BEHAVIORS = {"domain": 0, "ipcidr": 1}


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


def _decompress_header(data, extra_length=0):
    lib = _load_zstd()
    source = ctypes.create_string_buffer(data, len(data))
    input_buffer = _ZstdInput(ctypes.addressof(source), len(data), 0)
    target = ctypes.create_string_buffer(MRS_HEADER_SIZE + extra_length)
    stream = lib.ZSTD_createDStream()
    if not stream:
        raise MrsFormatError("无法创建 libzstd 解压上下文")
    try:
        output = _ZstdOutput(ctypes.addressof(target), MRS_HEADER_SIZE + extra_length, 0)
        while output.pos < output.size:
            result = lib.ZSTD_decompressStream(stream, ctypes.byref(output), ctypes.byref(input_buffer))
            if lib.ZSTD_isError(result):
                raise MrsFormatError("MRS zstd 压缩帧无效")
            if result == 0 or input_buffer.pos == input_buffer.size:
                break
        return target.raw[: output.pos]
    finally:
        lib.ZSTD_freeDStream(stream)


def validate_mrs(path, behavior):
    expected_behavior = MRS_BEHAVIORS.get(behavior)
    if expected_behavior is None:
        raise MrsFormatError(f"MRS 不支持行为: {behavior!r}")
    try:
        data = Path(path).read_bytes()
        payload = _decompress_header(data)
    except (OSError, MrsFormatError) as exc:
        raise MrsFormatError(f"MRS 文件读取或 zstd 解压失败: {path}（{exc}）") from exc
    if len(payload) < MRS_HEADER_SIZE or payload[:4] != MRS_MAGIC:
        raise MrsFormatError(f"MRS 头部无效: {path}")
    actual_behavior = payload[4]
    count, extra_length = struct.unpack(">qq", payload[5:MRS_HEADER_SIZE])
    if actual_behavior != expected_behavior:
        raise MrsFormatError(f"MRS 行为与 {behavior} 不匹配: {path}")
    if count <= 0 or extra_length < 0 or extra_length > 1024 * 1024:
        raise MrsFormatError(f"MRS 计数或扩展长度无效: {path}")
    if extra_length and len(payload) < MRS_HEADER_SIZE + extra_length:
        payload = _decompress_header(data, extra_length)
    if len(payload) != MRS_HEADER_SIZE + extra_length:
        raise MrsFormatError(f"MRS 头部或扩展区不完整: {path}")
    return count
