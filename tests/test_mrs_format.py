import ipaddress
import struct

import mrs_format
import pytest
from support import compress_zstd, domain_mrs_bytes, ipcidr_mrs_bytes, valid_mrs_bytes


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

    def test_zero_leaves_cannot_claim_a_rule(self, work_dir):
        path = work_dir / "empty-leaves.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + b"\0" * 8
            + struct.pack(">q", 1)
            + b"\xc0" + b"\0" * 7
            + struct.pack(">q", 1)
            + b"x"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="有效叶节点"):
            mrs_format.validate_mrs(path, "domain")

    def test_label_bitmap_must_match_labels(self, work_dir):
        path = work_dir / "bad-labels.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + b"\0" * 7 + b"\x01"
            + struct.pack(">q", 2)
            + b"\0" * 16
            + struct.pack(">q", 1)
            + b"x"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match=r"标签位图|LOUDS 树"):
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

    def test_unreachable_label_data_is_rejected(self, work_dir):
        path = work_dir / "unreachable.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + b"\0" * 7 + b"\x01"
            + struct.pack(">q", 1)
            + b"\xa0" + b"\0" * 7
            + struct.pack(">q", 1)
            + b"x"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="树"):
            mrs_format.validate_mrs(path, "domain")

    @pytest.mark.parametrize(
        "rules", [["x"], ["+.example.com", "www.example.com"], ["*.*.example.com", ".example.com"]]
    )
    def test_normal_domain_trees_pass(self, work_dir, rules):
        path = work_dir / "normal.mrs"
        path.write_bytes(domain_mrs_bytes(rules))
        assert mrs_format.validate_mrs(path, "domain") == len(rules)

    def test_label_bitmap_must_have_node_terminator(self, work_dir):
        path = work_dir / "bad-tree.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + b"\xc0" + b"\0" * 7
            + struct.pack(">q", 1)
            + b"\x80" + b"\0" * 7
            + struct.pack(">q", 1)
            + b"x"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match=r"LOUDS 树|标签位图"):
            mrs_format.validate_mrs(path, "domain")

    def test_zero_length_bitmap_record_is_rejected(self, work_dir):
        path = work_dir / "zero-bitmap.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + b"\xc0" + b"\0" * 7
            + struct.pack(">q", 0)
            + struct.pack(">q", 1)
            + b"x"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")

    def test_reverse_ip_range_is_rejected(self, work_dir):
        path = work_dir / "reverse.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + bytes.fromhex("20010db8000000000000000000000000")
            + b"\0" * 16
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x01" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="range"):
            mrs_format.validate_mrs(path, "ipcidr")

    def test_mixed_address_family_range_is_rejected(self, work_dir):
        path = work_dir / "mixed.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + ipaddress.ip_address("1.0.0.0").ipv6_mapped.packed
            + ipaddress.ip_address("2001:db8::1").packed
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x01" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="range"):
            mrs_format.validate_mrs(path, "ipcidr")

    def test_mapped_ipv6_txt_matches_mrs(self, work_dir):
        txt = work_dir / "mapped.txt"
        txt.write_text("::ffff:192.0.2.0/120\n", encoding="utf-8")
        mrs = work_dir / "mapped.mrs"
        mrs.write_bytes(ipcidr_mrs_bytes(["::ffff:192.0.2.0/120"]))

        mrs_format.verify_product_pair(txt, mrs, "ipcidr")

    def test_duplicate_sibling_labels_are_rejected(self, work_dir):
        path = work_dir / "duplicate-labels.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1) + b"\x00" * 7 + b"\x0a"
            + struct.pack(">q", 1) + b"\x00" * 7 + b"\x6c"
            + struct.pack(">q", 3) + b"aab"
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x00" + struct.pack(">qq", 2, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="重复"):
            mrs_format.validate_mrs(path, "domain")

    def test_overlong_domain_key_is_rejected(self, work_dir):
        path = work_dir / "long-key.mrs"
        path.write_bytes(domain_mrs_bytes(["a" * 256]))
        with pytest.raises(mrs_format.MrsFormatError, match="长度上限"):
            mrs_format.validate_mrs(path, "domain")

    def test_excessive_total_domain_key_bytes_are_rejected(self, work_dir, monkeypatch):
        monkeypatch.setattr(mrs_format, "MAX_DOMAIN_KEYS_BYTES", 2)
        path = work_dir / "many-keys.mrs"
        path.write_bytes(domain_mrs_bytes(["a", "bb"]))
        with pytest.raises(mrs_format.MrsFormatError, match="键总量"):
            mrs_format.validate_mrs(path, "domain")

    def test_excessive_domain_key_working_set_is_rejected(self, work_dir, monkeypatch):
        monkeypatch.setattr(mrs_format, "MAX_DOMAIN_KEYS_BYTES", 1)
        path = work_dir / "wide-tree.mrs"
        path.write_bytes(domain_mrs_bytes(["aa", "bb"]))
        with pytest.raises(mrs_format.MrsFormatError, match="工作集"):
            mrs_format.validate_mrs(path, "domain")

    @pytest.mark.parametrize("network", ["0.0.0.0/0", "255.255.255.255/32", "::/0", "2001:db8::1/128"])
    def test_valid_ip_ranges_pass(self, work_dir, network):
        path = work_dir / "valid-range.mrs"
        path.write_bytes(ipcidr_mrs_bytes([network]))
        assert mrs_format.validate_mrs(path, "ipcidr") == 1

    def test_libzstd_unavailable_fails_closed(self, work_dir, monkeypatch):
        path = work_dir / "valid.mrs"
        valid_mrs(path)
        monkeypatch.setattr(mrs_format, "_load_zstd", lambda: (_ for _ in ()).throw(OSError("unavailable")))
        with pytest.raises(mrs_format.MrsFormatError):
            mrs_format.validate_mrs(path, "domain")
