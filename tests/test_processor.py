import base64

import processor


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

    def test_leading_wildcards_stripped_to_suffix(self):
        assert self.clean([".ads.example.com", "+.ads.example.com"]) == ["+.ads.example.com"]
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
