import base64

import processor


class TestDomainCleaning:
    def clean(self, lines):
        return processor.process_domain(lines)

    def test_plain_domain_kept(self):
        assert self.clean(["example.com"]) == ["example.com"]

    def test_exception_rule_dropped_and_counted(self):
        result, stats = processor.process_domain_detailed(["@@ads.example.com", "ads.example.com"])
        assert result == ["ads.example.com"]
        assert stats["dropped_exception"] == 1

    def test_keyword_and_regexp_dropped_and_counted(self):
        result, stats = processor.process_domain_detailed(["keyword:tracker", "regexp:^ads\.", "ok.com"])
        assert result == ["ok.com"]
        assert stats["dropped_keyword"] == 2

    def test_full_and_host_widened_and_counted(self):
        result, stats = processor.process_domain_detailed(["full:cdn.example.com", "host:img.example.com"])
        assert result == ["cdn.example.com", "img.example.com"]
        assert stats["widened_exact"] == 2

    def test_domain_suffix_prefix(self):
        assert self.clean(["domain-suffix:example.com"]) == ["example.com"]

    def test_adblock_anchor_normalized_and_counted(self):
        result, stats = processor.process_domain_detailed(["||ads.example.com^"])
        assert result == ["ads.example.com"]
        assert stats["bad_anchor"] == 1

    def test_dollar_modifier_stripped_before_anchor(self):
        result, stats = processor.process_domain_detailed(["ads.example.com$third-party"])
        assert result == ["ads.example.com"]
        assert stats["bad_anchor"] == 0

    def test_hosts_file_format(self):
        assert self.clean(["127.0.0.1 ads.example.com"]) == ["ads.example.com"]
        assert self.clean(["0.0.0.0 ads.example.com"]) == ["ads.example.com"]

    def test_leading_wildcards_stripped(self):
        assert self.clean(["*.ads.example.com", ".ads.example.com", "+.ads.example.com"]) == ["ads.example.com"]

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
            "no-dots",
            "has space.com",
            "",
        ]) == []

    def test_idn_kept(self):
        assert self.clean(["例え.jp"]) == ["例え.jp"]

    def test_result_sorted_and_deduped(self):
        assert self.clean(["b.com", "a.com", "a.com"]) == ["a.com", "b.com"]


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

    def test_base64_payload_decoded(self):
        encoded = base64.b64encode(b"hidden.com\nanother.com").decode()
        assert processor.parse_lines(encoded) == ["hidden.com", "another.com"]

    def test_plain_text_not_treated_as_base64(self):
        assert processor.parse_lines("just-a-normal-rule.com") == ["just-a-normal-rule.com"]


class TestProcessIp:
    def test_v4_collapsed(self):
        assert processor.process_ip(["1.0.0.0/24", "1.0.1.0/24"]) == ["1.0.0.0/23"]

    def test_bare_ip_gets_prefixlen(self):
        assert processor.process_ip(["10.0.0.1"]) == ["10.0.0.1/32"]

    def test_default_route_dropped(self):
        assert processor.process_ip(["0.0.0.0/0", "::/0"]) == []

    def test_v4_before_v6(self):
        result = processor.process_ip(["2001:db8::/32", "10.0.0.0/8"])
        assert result == ["10.0.0.0/8", "2001:db8::/32"]

    def test_garbage_skipped(self):
        assert processor.process_ip(["hello", "not-an-ip"]) == []


class TestDecodeHelpers:
    def test_safe_decode_utf8(self):
        assert processor.safe_decode("域名.com".encode("utf-8")) == "域名.com"

    def test_safe_decode_garbage_returns_something_via_latin1(self):
        assert processor.safe_decode(b"\xff\xfe") != ""

    def test_is_text_data_rejects_binary(self):
        assert processor.is_text_data("ok") is True
        assert processor.is_text_data("a\0b\0c") is False
