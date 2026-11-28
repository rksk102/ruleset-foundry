import ipaddress
import struct
from pathlib import Path

import config_loader
import manifest
import merger
import mrs_format
import processor
import pytest
import release_handler
from support import compress_zstd, domain_mrs_bytes, ipcidr_mrs_bytes, valid_mrs_bytes


def valid_mrs(path, behavior="domain"):
    path.write_bytes(valid_mrs_bytes(behavior))


@pytest.mark.parametrize("syntax", ["exact", "suffix", "subdomain", "wildcard"])
@pytest.mark.parametrize("over_limit", [False, True], ids=["at-limit", "over-limit"])
def test_parser_representation_obeys_mrs_key_limit(work_dir, syntax, over_limit):
    fixed = ".".join(["例" * 20] * 4 + ["com"])
    prefix = {"exact": "", "suffix": "+.", "subdomain": ".", "wildcard": "*."}[syntax]
    padding = (8 if syntax == "exact" else 6) + int(over_limit)
    value = prefix + "a" * padding + fixed

    rules, stats = processor.process_domain_detailed(processor.parse_lines(value))

    if over_limit:
        assert rules == []
        assert stats["unrecognized"] == 1
    else:
        assert rules == [value]
        assert stats["unrecognized"] == 0
        txt = work_dir / "boundary.txt"
        mrs = work_dir / "boundary.mrs"
        txt.write_text(rules[0] + "\n", encoding="utf-8")
        mrs.write_bytes(domain_mrs_bytes(rules))
        mrs_format.verify_product_pair(txt, mrs, "domain")
        assert mrs_format.validate_mrs(mrs, "domain") == 1


def test_accepted_unicode_boundary_survives_merge_and_release_preflight(work_dir, monkeypatch):
    value = "+." + "a" * 6 + ".".join(["例" * 20] * 4 + ["com"])
    rules, stats = processor.process_domain_detailed(processor.parse_lines(value))
    assert rules == [value]
    assert stats["unrecognized"] == 0
    source = work_dir / "rulesets"
    merged = work_dir / "merged"
    mrs_root = work_dir / "mrs"
    rel = "block/domain/Owner/boundary.txt"
    txt = source / rel
    txt.parent.mkdir(parents=True)
    txt.write_text(rules[0] + "\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "_CONFIG", config_loader._defaults())
    monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
    monkeypatch.setattr(merger, "OUTPUT_DIR", str(merged))
    manifest.save_manifest(
        source / manifest.MANIFEST_NAME, manifest.hash_entries(source, [rel]), generation="boundary",
    )

    result = merger.process_task_logic(
        "block", "domain", "Owner", "boundary.txt", [rel], "boundary",
        baseline=manifest.load_manifest(source / manifest.MANIFEST_NAME),
    )

    assert result["opt"] == 1
    manifest.save_manifest(
        merged / manifest.MANIFEST_NAME, manifest.hash_entries(merged, [rel]), generation="boundary",
    )
    merged_rules = [
        line for line in (merged / rel).read_text(encoding="utf-8").splitlines() if not line.startswith("#")
    ]
    assert merged_rules == [value]
    mrs = mrs_root / Path(rel).with_suffix(".mrs")
    mrs.parent.mkdir(parents=True)
    mrs.write_bytes(domain_mrs_bytes(merged_rules))
    monkeypatch.setattr(release_handler, "RULESETS_DIR", str(source))

    assert release_handler.verify_products(str(merged), str(mrs_root), require_baseline=True) == 1


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

    def test_reversed_range_across_mapping_boundary_is_rejected(self, work_dir):
        path = work_dir / "reversed-boundary.mrs"
        body = (
            b"\x01"
            + struct.pack(">q", 1)
            + ipaddress.ip_address("2001:db8::1").packed
            + ipaddress.ip_address("1.0.0.0").ipv6_mapped.packed
        )
        path.write_bytes(compress_zstd(b"MRS\x01\x01" + struct.pack(">qq", 1, 0) + body))
        with pytest.raises(mrs_format.MrsFormatError, match="range"):
            mrs_format.validate_mrs(path, "ipcidr")

    def test_mapped_ipv6_txt_matches_mrs(self, work_dir):
        txt = work_dir / "mapped.txt"
        txt.write_text("::ffff:192.0.2.0/120\n", encoding="utf-8")
        mrs = work_dir / "mapped.mrs"
        mrs.write_bytes(ipcidr_mrs_bytes(["::ffff:192.0.2.0/120"]))

        mrs_format.verify_product_pair(txt, mrs, "ipcidr", forbid_default_route=True)

    def test_ipv6_cidr_crossing_mapping_boundary_is_rejected(self, work_dir):
        network = "::fffe:0:0/95"
        txt_root = work_dir / "txt"
        mrs_root = work_dir / "mrs"
        rel = Path("direct/ipcidr/Owner/boundary.txt")
        txt = txt_root / rel
        txt.parent.mkdir(parents=True)
        txt.write_text(network + "\n", encoding="utf-8")
        mrs = mrs_root / rel.with_suffix(".mrs")
        mrs.parent.mkdir(parents=True)
        mrs.write_bytes(ipcidr_mrs_bytes([network]))

        with pytest.raises(manifest.ManifestError, match="地址族"):
            release_handler.validate_product_files(txt_root, mrs_root)

    @pytest.mark.parametrize(
        ("network", "halves"),
        [("0.0.0.0/0", ["0.0.0.0/1", "128.0.0.0/1"]), ("::/0", ["::/1", "8000::/1"])],
        ids=["ipv4", "ipv6"],
    )
    @pytest.mark.parametrize("split", [False, True], ids=["explicit", "split"])
    def test_pair_can_reject_complete_address_family(self, work_dir, network, halves, split):
        rules = halves if split else [network]
        txt = work_dir / "full.txt"
        txt.write_text("\n".join(rules) + "\n", encoding="utf-8")
        mrs = work_dir / "full.mrs"
        mrs.write_bytes(ipcidr_mrs_bytes([network], count=len(rules)))

        mrs_format.verify_product_pair(txt, mrs, "ipcidr")
        with pytest.raises(mrs_format.MrsFormatError, match=r"覆盖整个 IPv[46] 地址族"):
            mrs_format.verify_product_pair(txt, mrs, "ipcidr", forbid_default_route=True)

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
