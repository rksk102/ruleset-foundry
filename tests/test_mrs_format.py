import struct

import mrs_format
import pytest
from support import compress_zstd, valid_mrs_bytes


def valid_mrs(path, behavior="domain"):
    path.write_bytes(valid_mrs_bytes(behavior))


class TestMrsFormat:
    @pytest.mark.parametrize("behavior", ["domain", "ipcidr"])
    def test_valid_header_passes(self, work_dir, behavior):
        path = work_dir / f"{behavior}.mrs"
        valid_mrs(path, behavior)
        assert mrs_format.validate_mrs(path, behavior) == 1

    def test_nonempty_garbage_rejected(self, work_dir):
        path = work_dir / "garbage.mrs"
        path.write_bytes(b"not an mrs\n")
        with pytest.raises(mrs_format.MrsFormatError, match="zstd"):
            mrs_format.validate_mrs(path, "domain")

    def test_truncated_frame_rejected(self, work_dir):
        path = work_dir / "truncated.mrs"
        valid_mrs(path)
        path.write_bytes(path.read_bytes()[:10])
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")

    def test_valid_header_with_truncated_body_rejected(self, work_dir):
        path = work_dir / "body-truncated.mrs"
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + b"\x01\x00"))
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")

    def test_trailing_compressed_data_rejected(self, work_dir):
        path = work_dir / "trailing.mrs"
        path.write_bytes(valid_mrs_bytes() + b"junk")
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")

    def test_wrong_behavior_rejected(self, work_dir):
        path = work_dir / "wrong.mrs"
        valid_mrs(path, "domain")
        with pytest.raises(mrs_format.MrsFormatError, match="行为"):
            mrs_format.validate_mrs(path, "ipcidr")

    def test_header_only_is_not_valid_product(self, work_dir):
        path = work_dir / "empty.mrs"
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 0, 0)))
        with pytest.raises(mrs_format.MrsFormatError, match="计数"):
            mrs_format.validate_mrs(path, "domain")

    def test_libzstd_unavailable_fails_closed(self, work_dir, monkeypatch):
        path = work_dir / "valid.mrs"
        valid_mrs(path)
        monkeypatch.setattr(mrs_format, "_load_zstd", lambda: (_ for _ in ()).throw(OSError("unavailable")))
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")
