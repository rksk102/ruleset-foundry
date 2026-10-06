import base64
import io
import sys

import processor


class TestDomainKind:
    def test_default_is_exact(self):
        assert processor.process_domain(["example.com"]) == ["example.com"]

    def test_suffix_kind_promotes_bare_domain(self):
        assert processor.process_domain(["example.com"], "suffix") == ["+.example.com"]

    def test_suffix_kind_promotes_multi_label_and_single_label(self):
        assert processor.process_domain(["ads.example.com", "local"], "suffix") == [
            "+.ads.example.com",
            "+.local",
        ]

    def test_suffix_kind_keeps_full_prefix_exact(self):
        assert processor.process_domain(["full:api.example.com"], "suffix") == ["api.example.com"]

    def test_suffix_kind_keeps_explicit_suffix_unchanged(self):
        assert processor.process_domain(["domain:example.com"], "suffix") == ["+.example.com"]

    def test_suffix_kind_preserves_wildcard_and_subdomain_forms(self):
        result = processor.process_domain(["*.example.com", ".sub.example.com"], "suffix")
        assert result == ["*.example.com", ".sub.example.com"]

    def test_suffix_kind_does_not_promote_explicit_full(self):
        assert processor.process_domain(["full:api.example.com"], "suffix") == ["api.example.com"]

    def test_suffix_kind_promotes_unmarked_only(self):
        result, stats = processor.process_domain_detailed(
            ["a.com", "full:b.com", "host:c.com", "domain:d.com"], "suffix"
        )
        assert stats["suffix_promoted"] == 1
        assert stats["relaxed_exact"] == 2
        assert stats["suffix"] == 1

    def test_suffix_kind_counts_promotions(self):
        _result, stats = processor.process_domain_detailed(
            ["a.com", "b.com", "full:c.com"], "suffix"
        )
        assert stats["suffix_promoted"] == 2
        assert stats["relaxed_exact"] == 1

    def test_exact_kind_counts_no_promotions(self):
        _result, stats = processor.process_domain_detailed(["a.com"], "exact")
        assert stats["suffix_promoted"] == 0
        assert stats["relaxed_exact"] == 1

    def test_suffix_promotion_does_not_drop_entries(self):
        lines = ["a.com", "b.example.com", "full:c.net", "*.d.org", ".e.io", "f"]
        exact = set(processor.process_domain(lines, "exact"))
        suffixed = set(processor.process_domain(lines, "suffix"))
        assert len(exact) == len(suffixed)
        assert {x.lstrip("+.") for x in suffixed} == {x.lstrip("+.") for x in exact}


class TestDomainCleaning:
    def clean(self, lines):
        return processor.process_domain(lines)

    def test_plain_domain_kept_as_exact(self):
        assert self.clean(["example.com"]) == ["example.com"]

    def test_exception_rule_dropped_and_counted(self):
        result, stats = processor.process_domain_detailed(["@@ads.example.com", "ads.example.com"])
        assert result == ["ads.example.com"]
        assert stats["dropped_exception"] == 1

    def test_keyword_and_regexp_dropped_and_counted(self):
        result, stats = processor.process_domain_detailed([r"keyword:tracker", r"regexp:^ads\.", "ok.com"])
        assert result == ["ok.com"]
        assert stats["dropped_keyword"] == 2

    def test_full_and_host_become_exact(self):
        result, stats = processor.process_domain_detailed(["full:cdn.example.com", "host:img.example.com"])
        assert result == ["cdn.example.com", "img.example.com"]
        assert stats["relaxed_exact"] == 2

    def test_domain_suffix_prefix_preserved_as_suffix(self):
        assert self.clean(["domain-suffix:example.com"]) == ["+.example.com"]
        assert self.clean(["domain:example.com"]) == ["+.example.com"]

    def test_explicit_suffix_prefix_preserved(self):
        assert self.clean(["+.example.com"]) == ["+.example.com"]

    def test_suffix_counter(self):
        result, stats = processor.process_domain_detailed(["+.a.com", "domain:b.com"])
        assert result == ["+.a.com", "+.b.com"]
        assert stats["suffix"] == 2
        assert stats["relaxed_exact"] == 0

    def test_adblock_anchor_normalized(self):
        result, _stats = processor.process_domain_detailed(["||ads.example.com^"])
        assert result == ["ads.example.com"]

    def test_dollar_modifier_stripped_before_anchor(self):
        result, _stats = processor.process_domain_detailed(["ads.example.com$third-party"])
        assert result == ["ads.example.com"]

    def test_hosts_file_format(self):
        assert self.clean(["127.0.0.1 ads.example.com"]) == ["ads.example.com"]
        assert self.clean(["0.0.0.0 ads.example.com"]) == ["ads.example.com"]

    def test_leading_dot_is_subdomain_only(self):
        """`.d` 是「仅子域，不含 apex」，与 `+.d` 匹配集不同，必须区分。"""
        result, stats = processor.process_domain_detailed([".ads.example.com"])
        assert result == [".ads.example.com"]
        assert stats["subdomain"] == 1
        assert stats["suffix"] == 0

    def test_explicit_suffix_kept_distinct_from_subdomain(self):
        result, stats = processor.process_domain_detailed(["+.ads.example.com"])
        assert result == ["+.ads.example.com"]
        assert stats["suffix"] == 1
        assert stats["subdomain"] == 0

    def test_single_level_wildcard_preserved(self):
        result, stats = processor.process_domain_detailed(["*.ads.example.com"])
        assert result == ["*.ads.example.com"]
        assert stats["wildcard"] == 1

    def test_multi_level_wildcard_preserved(self):
        result, stats = processor.process_domain_detailed(["*.*.microsoft.com"])
        assert result == ["*.*.microsoft.com"]
        assert stats["wildcard"] == 1

    def test_single_label_suffix_preserved(self):
        result, stats = processor.process_domain_detailed(["+.local", "+.lan", "+.internal"])
        assert result == ["+.internal", "+.lan", "+.local"]
        assert stats["suffix"] == 3

    def test_single_label_subdomain_preserved(self):
        result, stats = processor.process_domain_detailed([".local"])
        assert result == [".local"]
        assert stats["subdomain"] == 1

    def test_bare_single_label_kept_and_counted(self):
        result, stats = processor.process_domain_detailed(["localhost"])
        assert result == ["localhost"]
        assert stats["bare_single_label"] == 1

    def test_path_and_port_stripped(self):
        assert self.clean(["example.com/ads/path"]) == ["example.com"]
        assert self.clean(["example.com:8080"]) == ["example.com"]

    def test_ip_literal_dropped(self):
        assert self.clean(["1.2.3.4", "10.0.0.1/32"]) == []

    def test_malformed_domains_dropped(self):
        assert self.clean([
            "a..b.com",
            "trailing.com.",
            "-bad.com",
            "bad-.com",
            "has space.com",
            "",
        ]) == []

    def test_single_label_is_not_malformed(self):
        assert self.clean(["no-dots"]) == ["no-dots"]

    def test_idn_kept(self):
        assert self.clean(["例え.jp"]) == ["例え.jp"]

    def test_result_sorted_and_deduped(self):
        assert self.clean(["b.com", "a.com", "a.com"]) == ["a.com", "b.com"]


class TestClassicalRuleTypes:

    def route(self, line):
        return processor.process_domain_detailed([line])

    def test_domain_suffix_becomes_suffix(self):
        out, stats = self.route("DOMAIN-SUFFIX,ads.example.com")
        assert out == ["+.ads.example.com"]
        assert stats["suffix"] == 1

    def test_domain_becomes_exact(self):
        out, stats = self.route("DOMAIN,exact.example.com")
        assert out == ["exact.example.com"]
        assert stats["relaxed_exact"] == 1

    def test_case_insensitive(self):
        out, _ = self.route("domain-suffix,Ads.Example.COM")
        assert out == ["+.ads.example.com"]

    def test_keyword_and_regex_counted_by_type(self):
        out, stats = self.route("DOMAIN-KEYWORD,analytics")
        assert out == []
        assert stats["dropped_rule_type"]["DOMAIN-KEYWORD"] == 1

        out, stats = self.route(r"DOMAIN-REGEX,^ads\.")
        assert out == []
        assert stats["dropped_rule_type"]["DOMAIN-REGEX"] == 1

    def test_ip_rule_in_domain_source_counted(self):
        out, stats = self.route("IP-CIDR,1.2.3.0/24")
        assert out == []
        assert stats["ip_in_domain"] == 1

    def test_geoip_counted(self):
        out, stats = self.route("GEOIP,CN")
        assert out == []
        assert stats["dropped_rule_type"]["GEOIP"] == 1

    def test_process_name_counted(self):
        out, stats = self.route("PROCESS-NAME,curl")
        assert out == []
        assert stats["dropped_rule_type"]["PROCESS-NAME"] == 1

    def test_no_resolve_modifier_stripped(self):
        out, stats = self.route("IP-CIDR,1.2.3.0/24,no-resolve")
        assert out == []
        assert stats["ip_in_domain"] == 1


class TestNoSilentDrop:

    DROPPED_SHAPES = [
        "@@||white.example.com^",
        "keyword:tracker",
        r"regexp:^ads\.",
        "DOMAIN-KEYWORD,analytics",
        r"DOMAIN-REGEX,^ads\.",
        "GEOIP,CN",
        "IP-CIDR,1.2.3.0/24",
        "steampowered.com.8686c.com @cn",
        "1.2.3.4",
        "a..b.com",
        "trailing.com.",
        "-bad.com",
        "has space.com",
    ]

    def test_every_dropped_shape_is_counted(self):
        unaccounted = []
        for line in self.DROPPED_SHAPES:
            out, stats = processor.process_domain_detailed([line])
            if out:
                continue
            claimed = (
                stats["unrecognized"]
                + stats["dropped_exception"]
                + stats["dropped_keyword"]
                + stats["ip_in_domain"]
                + sum(stats["dropped_rule_type"].values())
            )
            if claimed != 1:
                unaccounted.append((line, claimed, stats))
        assert unaccounted == [], f"以下行被静默丢弃: {unaccounted}"

    def test_shapes_that_must_be_kept_are_not_dropped(self):
        keep = [
            "+.example.com",
            "domain:example.com",
            "*.ads.example.com",
            "*.*.microsoft.com",
            "+.local",
            "localhost",
            "example.com",
        ]
        for line in keep:
            out, _stats = processor.process_domain_detailed([line])
            assert out, f"{line!r} 应当被保留"

    def test_blank_lines_are_intentionally_free(self):
        _out, stats = processor.process_domain_detailed([""])
        assert stats["unrecognized"] == 0

class TestDetailedParity:
    MIXED = [
        "@@exception.com",
        "keyword:kw",
        "full:exact.com",
        "||anchor.com^",
        "normal.com",
    ]

    def test_detailed_matches_legacy_wrappers(self):
        expected_result = processor.process_domain(self.MIXED)
        expected_stats = processor.analyze_domain(self.MIXED)
        result, stats = processor.process_domain_detailed(self.MIXED)
        assert result == expected_result
        assert stats == expected_stats


class TestParseLines:
    def test_comments_and_blank_skipped(self):
        assert processor.parse_lines("# header\n\n! adblock-comment\ndomain.com\n") == ["domain.com"]

    def test_inline_comment_stripped(self):
        assert processor.parse_lines("domain.com # inline note") == ["domain.com"]

    def test_yaml_payload_list(self):
        content = "payload:\n  - '+.example.com'\n  - 'full:other.com'\n"
        assert processor.parse_lines(content) == ["+.example.com", "full:other.com"]

    def test_yaml_payload_inline_list(self):
        content = "payload: ['a.com', 'b.com']\n"
        assert processor.parse_lines(content) == ["a.com", "b.com"]

    def test_yaml_payload_ends_at_next_key(self):
        content = "payload:\n  - a.com\nbehavior: domain\n  - not-a-rule.com\n"
        assert processor.parse_lines(content) == ["a.com"]

    def test_payload_detected_after_long_comment_header(self):
        header = "\n".join(f"# comment {i}" for i in range(80))
        content = header + "\npayload:\n  - '+.example.com'\n"
        assert processor.parse_lines(content) == ["+.example.com"]

    def test_base64_payload_decoded(self):
        encoded = base64.b64encode(b"hidden.com\nanother.com").decode()
        assert processor.parse_lines(encoded) == ["hidden.com", "another.com"]

    def test_plain_text_not_treated_as_base64(self):
        assert processor.parse_lines("just-a-normal-rule.com") == ["just-a-normal-rule.com"]


class TestProcessIp:
    def test_v4_collapsed(self):
        assert processor.process_ip(["1.0.0.0/24", "1.0.1.0/24"])[0] == ["1.0.0.0/23"]

    def test_bare_ip_gets_prefixlen(self):
        assert processor.process_ip(["10.0.0.1"])[0] == ["10.0.0.1/32"]

    def test_default_route_dropped(self):
        assert processor.process_ip(["0.0.0.0/0", "::/0"])[0] == []

    def test_v4_before_v6(self):
        result, _ = processor.process_ip(["2001:db8::/32", "10.0.0.0/8"])
        assert result == ["10.0.0.0/8", "2001:db8::/32"]

    def test_garbage_skipped(self):
        assert processor.process_ip(["hello", "not-an-ip"])[0] == []

    def test_errors_are_returned_not_discarded(self):
        result, errors = processor.process_ip(["1.0.0.0/24", "garbage"])
        assert result == ["1.0.0.0/24"]
        assert len(errors) == 1
        assert errors[0][0] == "garbage"


class TestDecodeHelpers:
    def test_safe_decode_utf8(self):
        assert processor.safe_decode("域名.com".encode("utf-8")) == "域名.com"

    def test_safe_decode_garbage_returns_something_via_latin1(self):
        assert processor.safe_decode(b"\xff\xfe") != ""

    def test_is_text_data_rejects_binary(self):
        assert processor.is_text_data("ok") is True
        assert processor.is_text_data("a\0b\0c") is False


class TestRuleTypeTable:

    DOCUMENTED = [
        "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-WILDCARD", "DOMAIN-REGEX",
        "GEOSITE", "GEOIP", "SRC-GEOIP",
        "IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR", "IP-SUFFIX", "SRC-IP-SUFFIX", "IP-ASN", "SRC-IP-ASN",
        "DST-PORT", "SRC-PORT", "IN-PORT", "IN-TYPE", "IN-USER", "IN-NAME", "REMATCH-NAME",
        "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-WILDCARD", "PROCESS-PATH-WILDCARD",
        "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX", "UID", "NETWORK", "DSCP",
        "RULE-SET", "SUB-RULE", "AND", "OR", "NOT", "MATCH",
    ]

    def test_documented_mihomo_types_are_recognized(self):
        for name in self.DOCUMENTED:
            assert name in processor._MIHOMO_RULE_TYPES, name
            assert processor.classify_rule_line(f"{name},payload")[2] == name

    def test_alias_types_from_other_formats_are_recognized(self):
        for name in ("HOST-SUFFIX", "HOST", "FULL"):
            assert processor.classify_rule_line(f"{name},payload")[2] == name

    def test_types_mihomo_does_not_have_are_not_mihomo_types(self):
        for name in ("DST-IP-CIDR", "DST-IP-ASN", "DST-GEOIP", "SCRIPT",
                     "SRC-PORT-RANGE", "DST-PORT-RANGE"):
            assert name not in processor._MIHOMO_RULE_TYPES, name
            assert name in processor._UNSUPPORTED_TYPES, name

    def test_only_cidr_types_classify_as_ip(self):
        for name in ("IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR"):
            assert processor.classify_rule_line(f"{name},1.2.3.0/24")[0] == "ip"

    def test_unexpressible_ip_types_do_not_classify_as_ip(self):
        for name in ("IP-SUFFIX", "SRC-IP-SUFFIX", "IP-ASN", "SRC-IP-ASN", "DST-IP-ASN"):
            kind, _payload, type_name = processor.classify_rule_line(f"{name},x")
            assert kind == "opaque", name
            assert type_name == name

    def test_longest_type_name_wins(self):
        assert processor.classify_rule_line("PROCESS-NAME-WILDCARD,*telegram*")[2] == "PROCESS-NAME-WILDCARD"
        assert processor.classify_rule_line("PROCESS-PATH-WILDCARD,/usr/*/wget")[2] == "PROCESS-PATH-WILDCARD"
        assert processor.classify_rule_line("PROCESS-NAME-REGEX,curl$")[2] == "PROCESS-NAME-REGEX"


class TestNoSilentRewriteInIpMode:

    def test_ip_suffix_is_not_rewritten_to_cidr(self):
        result, errors, stats = processor.process_ip_detailed(["IP-SUFFIX,8.8.8.8/24"])
        assert result == []
        assert errors == []
        assert stats["dropped_rule_type"]["IP-SUFFIX"] == 1

    def test_ip_asn_is_reported_as_type_not_as_invalid_cidr(self):
        result, errors, stats = processor.process_ip_detailed(["IP-ASN,13335"])
        assert result == []
        assert errors == []
        assert stats["dropped_rule_type"]["IP-ASN"] == 1

    def test_src_ip_asn_and_src_ip_suffix_dropped(self):
        result, errors, stats = processor.process_ip_detailed(
            ["SRC-IP-ASN,9808", "SRC-IP-SUFFIX,192.168.1.1/8"]
        )
        assert result == []
        assert errors == []
        assert stats["dropped_rule_type"] == {"SRC-IP-ASN": 1, "SRC-IP-SUFFIX": 1}

    def test_unsupported_types_dropped_without_cidr_error(self):
        result, errors, stats = processor.process_ip_detailed(
            ["DST-IP-CIDR,1.2.3.0/24", "SCRIPT,code"]
        )
        assert result == []
        assert errors == []
        assert stats["dropped_rule_type"] == {"DST-IP-CIDR": 1, "SCRIPT": 1}

    def test_domain_rules_in_ip_source_are_dropped_and_counted(self):
        result, errors, stats = processor.process_ip_detailed(["DOMAIN-SUFFIX,ads.example.com"])
        assert result == []
        assert errors == []
        assert stats["dropped_rule_type"]["DOMAIN-SUFFIX"] == 1

    def test_drop_reasons_all_mention_ipcidr(self):
        for name in ("IP-SUFFIX", "SRC-IP-SUFFIX", "IP-ASN", "SRC-IP-ASN", "DST-IP-ASN",
                     "DST-IP-CIDR", "SCRIPT", "DOMAIN-SUFFIX"):
            assert "ipcidr 规则集表达" in processor.ipcidr_drop_reason(name), name


class TestClassicalRuleLinesInIpMode:

    def test_classic_rule_lines_keep_their_payload(self):
        result, errors = processor.process_ip([
            "IP-CIDR,1.2.3.0/24",
            "IP-CIDR6,2001:db8::/32",
            "SRC-IP-CIDR,10.0.0.0/8",
        ])
        assert errors == []
        assert result == ["1.2.3.0/24", "10.0.0.0/8", "2001:db8::/32"]

    def test_classic_line_is_not_read_as_hex_fragment(self):
        result, errors = processor.process_ip(["IP-CIDR,x"])
        assert result == []
        assert len(errors) == 1
        assert errors[0][0] == "x"

    def test_no_resolve_modifier_stripped(self):
        result, errors = processor.process_ip(["IP-CIDR,1.2.3.0/24,no-resolve"])
        assert result == ["1.2.3.0/24"]
        assert errors == []

    def test_rule_target_after_payload_is_ignored(self):
        result, errors = processor.process_ip(["IP-CIDR,1.2.3.0/24,DIRECT"])
        assert result == ["1.2.3.0/24"]
        assert errors == []

    def test_empty_payload_counted(self):
        _result, errors, stats = processor.process_ip_detailed(["IP-CIDR,"])
        assert errors == []
        assert stats["unrecognized"] == 1

    def test_unclassified_lines_still_use_extract(self):
        result, errors = processor.process_ip(["  1.2.3.0/24 # note", "10.0.0.0/8"])
        assert result == ["1.2.3.0/24", "10.0.0.0/8"]
        assert errors == []

    def test_process_ip_keeps_legacy_two_tuple(self):
        assert len(processor.process_ip(["1.2.3.0/24"])) == 2

    def test_classified_and_unclassified_lines_collapse_together(self):
        result, _errors = processor.process_ip(["IP-CIDR,1.0.0.0/24", "  1.0.1.0/24 # note"])
        assert result == ["1.0.0.0/23"]


class _FakeStdin:
    def __init__(self, raw):
        self.buffer = io.BytesIO(raw)


class TestMainCliContract:

    def invoke(self, monkeypatch, capsys, payload):
        monkeypatch.setattr(sys, "argv", ["processor.py", "ipcidr"])
        monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
        processor.main()
        return capsys.readouterr()

    def test_ipcidr_mode_classifies_before_extract(self, monkeypatch, capsys):
        captured = self.invoke(
            monkeypatch, capsys,
            b"IP-CIDR,1.2.3.0/24\nIP-CIDR6,2001:db8::/32\nIP-ASN,13335\n",
        )
        assert captured.out.splitlines() == ["1.2.3.0/24", "2001:db8::/32"]
        assert "无效 CIDR" not in captured.err
        assert "IP-ASN" in captured.err
        assert "无法用 ipcidr 规则集表达" in captured.err

    def test_ip_suffix_is_not_emitted_as_cidr(self, monkeypatch, capsys):
        captured = self.invoke(monkeypatch, capsys, b"IP-SUFFIX,8.8.8.8/24\n")
        assert captured.out.splitlines() == []
        assert "IP-SUFFIX" in captured.err
        assert "8.8.8.0/24" not in captured.out


class TestYamlPayloadParsing:

    def test_multiline_flow_array(self):
        content = "payload: [\n  'a.com',\n  'b.com'\n]\n"
        assert processor.parse_lines(content) == ["a.com", "b.com"]

    def test_rules_key(self):
        content = "rules:\n  - DOMAIN-SUFFIX,a.com\n  - IP-CIDR,1.2.3.0/24\n"
        assert processor.parse_lines(content) == ["DOMAIN-SUFFIX,a.com", "IP-CIDR,1.2.3.0/24"]

    def test_payload_preferred_over_rules(self):
        content = "payload:\n  - a.com\nrules:\n  - b.com\n"
        assert processor.parse_lines(content) == ["a.com"]

    def test_payload_with_trailing_key(self):
        content = "payload:\n  - '+.a.com'\n  - 'b.com'\nbehavior: domain\n"
        assert processor.parse_lines(content) == ["+.a.com", "b.com"]

    def test_quoted_and_unquoted_entries_agree(self):
        content = "payload:\n  - '+.a.com'\n  - b.com\n"
        assert processor.parse_lines(content) == ["+.a.com", "b.com"]

    def test_non_string_items_fall_back_to_line_scan(self):
        content = "payload:\n  - 13335\n  - a.com\n"
        assert processor.parse_lines(content) == ["13335", "a.com"]

    def test_invalid_yaml_falls_back_to_line_scan(self):
        content = "payload:\n  - a.com\n: : :\n"
        assert processor.parse_lines(content) == ["a.com"]

    def test_missing_pyyaml_falls_back_silently(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yaml", None)
        assert processor.parse_lines("payload: ['a.com', 'b.com']\n") == ["a.com", "b.com"]

    def test_missing_pyyaml_keeps_plain_line_scan(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yaml", None)
        assert processor.parse_lines("a.com\n# note\nb.com\n") == ["a.com", "b.com"]

    def test_base64_yaml_payload_decoded_then_parsed(self):
        encoded = base64.b64encode(b"payload:\n  - a.com\n  - b.com\n").decode()
        assert processor.parse_lines(encoded) == ["a.com", "b.com"]

    def test_plain_domain_list_is_not_taken_as_yaml(self):
        assert processor.parse_lines("a.com\nb.com\n") == ["a.com", "b.com"]
