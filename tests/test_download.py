import asyncio

import main


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, chunks, headers=None):
        self.status = status
        self.content = FakeStream(chunks)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, timeout=None, headers=None, **kw):
        self.calls.append({"url": url, "headers": headers})
        return self._response


TASK = {"url": "https://example.com/list.txt", "policy": "block", "type": "domain"}


def download(chunks, status=200, headers=None):
    session = FakeSession(FakeResponse(status, chunks, headers))
    return asyncio.run(main.download_one(session, TASK))


def with_limit(value, fn):
    original = main.MAX_SOURCE_BYTES
    main.MAX_SOURCE_BYTES = value
    try:
        return fn()
    finally:
        main.MAX_SOURCE_BYTES = original


class TestDownloadCap:
    def test_content_within_limit_returned(self):
        _task, content, err = download([b"example.com\n", b"ads.example.com\n"])
        assert err is None
        assert content == b"example.com\nads.example.com\n"

    def test_exact_limit_allowed(self):
        _task, content, err = with_limit(12, lambda: download([b"x" * 6, b"y" * 6]))
        assert err is None
        assert len(content) == 12

    def test_oversized_response_rejected(self):
        _task, content, err = with_limit(10, lambda: download([b"x" * 6, b"y" * 6]))
        assert content is None
        assert err == "超过 10 字节上限"

    def test_empty_response_reported(self):
        _task, content, err = download([])
        assert content is None
        assert err == "空响应"

    def test_non_text_response_reported(self):
        _task, content, err = download(["a\0b\0c".encode()])
        assert content is None
        assert err == "非文本响应"

    def test_client_error_not_retried(self):
        _task, content, err = download([b""], status=404)
        assert content is None
        assert err == "HTTP 404"


class TestRetryPolicy:
    def retry_with(self, status, headers=None, retries=2):
        slept = []
        original_sleep = asyncio.sleep
        original_retries = main.RETRIES
        main.RETRIES = retries

        async def fake_sleep(seconds):
            slept.append(seconds)

        asyncio.sleep = fake_sleep
        try:
            session = FakeSession(FakeResponse(status, [b"x"], headers))
            result = asyncio.run(main.download_one(session, TASK))
        finally:
            asyncio.sleep = original_sleep
            main.RETRIES = original_retries
        return session, result, slept

    def test_404_not_retried(self):
        _s, _r, slept = self.retry_with(404)
        assert slept == []

    def test_408_is_retried(self):
        _s, _r, slept = self.retry_with(408)
        assert len(slept) == 2

    def test_425_is_retried(self):
        _s, _r, slept = self.retry_with(425)
        assert len(slept) == 2

    def test_429_is_retried(self):
        _s, _r, slept = self.retry_with(429)
        assert len(slept) == 2

    def test_503_is_retried(self):
        _s, _r, slept = self.retry_with(503)
        assert len(slept) == 2

    def test_retry_after_seconds_honoured(self):
        _s, _r, slept = self.retry_with(429, {"Retry-After": "30"})
        assert slept == [30, 30]

    def test_retry_after_is_capped(self):
        _s, _r, slept = self.retry_with(429, {"Retry-After": "3600"})
        assert slept == [main.MAX_RETRY_AFTER, main.MAX_RETRY_AFTER]

    def test_retry_after_zero_means_no_wait(self):
        _s, _r, slept = self.retry_with(503, {"Retry-After": "0"})
        assert slept == []

    def test_retry_after_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        when = datetime.now(timezone.utc) + timedelta(seconds=20)
        _s, _r, slept = self.retry_with(429, {"Retry-After": format_datetime(when)})
        assert all(15 <= s <= 21 for s in slept), slept

    def test_invalid_retry_after_falls_back(self):
        _s, _r, slept = self.retry_with(429, {"Retry-After": "not-a-date"})
        assert slept == [1, 2]


class TestAuthHeaderAllowlist:
    def test_trusted_github_hosts(self):
        for url in (
            "https://raw.githubusercontent.com/a/b.txt",
            "https://github.com/a/b/raw/x.txt",
            "https://objects.githubusercontent.com/x",
            "https://api.github.com/repos/x",
        ):
            assert main.is_trusted_host(url) is True, url

    def test_untrusted_hosts_rejected(self):
        for url in (
            "https://evil.example.com/x.txt",
            "https://raw.githubusercontent.com.evil.com/x.txt",
            "https://notgithub.com/x.txt",
            "https://github.com.evil.net/x.txt",
            "not-a-url",
        ):
            assert main.is_trusted_host(url) is False, url

    def test_token_only_sent_to_trusted_hosts(self):
        import os

        os.environ["GH_TOKEN"] = "test-token-value"
        try:
            assert main.auth_headers("https://raw.githubusercontent.com/a") == {
                "Authorization": "Bearer test-token-value"
            }
            assert main.auth_headers("https://evil.example.com/a") is None
        finally:
            os.environ.pop("GH_TOKEN", None)

    def test_no_token_means_no_header(self):
        import os

        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        assert main.auth_headers("https://raw.githubusercontent.com/a") is None

    def test_parse_retry_after_variants(self):
        assert main.parse_retry_after("45", 1) == 45
        assert main.parse_retry_after("99999", 1) == main.MAX_RETRY_AFTER
        assert main.parse_retry_after("", 7) == 7
        assert main.parse_retry_after(None, 7) == 7
        assert main.parse_retry_after("garbage", 7) == 7

    def test_parse_retry_after_unicode_digits_do_not_raise(self):
        for weird in ("²", "³", "①", "٣٠"):
            assert main.parse_retry_after(weird, 7) == 7, weird

    def test_parse_retry_after_negative_and_compound(self):
        assert main.parse_retry_after("-5", 7) == 7
        assert main.parse_retry_after("30, 60", 7) == 7
        assert main.parse_retry_after("+30", 7) == 7


class TestBackoffBudget:
    def test_budget_stops_endless_retries(self):
        slept = []
        original_sleep = asyncio.sleep
        original_retries = main.RETRIES
        main.RETRIES = 5

        async def fake_sleep(seconds):
            slept.append(seconds)

        asyncio.sleep = fake_sleep
        try:
            session = FakeSession(FakeResponse(429, [b"x"], {"Retry-After": "3600"}))
            _t, content, err = asyncio.run(main.download_one(session, TASK))
        finally:
            asyncio.sleep = original_sleep
            main.RETRIES = original_retries
        assert content is None
        assert "退避预算" in (err or "")
        assert sum(slept) <= main.MAX_RETRY_AFTER * 2


class TestSourceRepoSlug:

    def test_known_hosts_extract_owner_repo(self):
        assert main.source_repo_slug(
            "https://raw.githubusercontent.com/Owner/repo/release/gfw.txt") == "Owner__repo"
        assert main.source_repo_slug(
            "https://github.com/Owner/repo/raw/x/gfw.txt") == "Owner__repo"
        assert main.source_repo_slug(
            "https://cdn.jsdelivr.net/gh/Owner/repo@ver/gfw.txt") == "Owner__repo"

    def test_lookalike_host_is_not_treated_as_github(self):
        slug = main.source_repo_slug(
            "https://evil-github-cdn.example.com/Owner/repo/release/gfw.txt")
        assert "Owner__repo" != slug
        assert slug == "evil-github-cdn_example_com"

    def test_unknown_host_falls_back_to_hostname_not_empty(self):
        assert main.source_repo_slug("https://mirror.example.org/a/b/gfw.txt") == \
            "mirror_example_org"

    def _task(self, url):
        return {"url": url, "policy": "policy", "type": "domain"}

    def test_mirror_cannot_merge_into_mirrored_base_path(self):
        urls = [
            "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/gfw.txt",
            "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/gfw.txt",
            "https://github-mirror.example.com/Loyalsoldier__clash-rules/clash-rules/release/gfw.txt",
        ]
        groups = main.plan_groups([self._task(u) for u in urls])
        for g in groups:
            slugs = {main.source_repo_slug(t["url"]) for _i, t in g["members"]}
            assert len(g["members"]) == 1 or len(slugs) == 1, \
                f"多来源共享输出: {g['path']} <- {slugs}"

    def test_lookalike_host_not_merged_with_real_source(self):
        urls = [
            "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/gfw.txt",
            "https://evil-github-cdn.example.com/Loyalsoldier/clash-rules/release/gfw.txt",
        ]
        groups = main.plan_groups([self._task(u) for u in urls])
        assert len(groups) == 2, f"应拆成两个输出，实际 {len(groups)}"

    def test_real_source_list_keeps_other_urls_stable(self):
        groups = main.plan_groups(main.parse_sources())
        names = {g["path"].replace("\\", "/") for g in groups}
        assert "rulesets/policy/domain/Loyalsoldier/proxy.txt" in names
        assert "rulesets/direct/domain/Loyalsoldier/private.txt" in names
        assert "rulesets/block/domain/Loyalsoldier/reject.txt" in names
        changed = {n for n in names if "__" in n}
        assert changed == {
            "rulesets/policy/domain/Loyalsoldier__clash-rules/gfw.txt",
            "rulesets/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.txt",
        }


class TestProcessGroupIpRouting:
    def test_classical_ip_prefix_stripped_in_ipcidr_group(self):
        group = {
            "path": "rulesets/direct/ipcidr/Owner/cn.txt",
            "policy": "direct",
            "type": "ipcidr",
            "members": [(0, {"url": "https://raw.githubusercontent.com/o/r/cn.txt"})],
        }
        raw = b"IP-CIDR,1.0.1.0/24\n1.0.2.0/23\n"
        original = main.atomic_write
        written = {}
        main.atomic_write = lambda path, content: written.update({path: content})
        try:
            count, errors = main.process_group(group, {0: raw})
        finally:
            main.atomic_write = original
        assert count == 2
        assert written["rulesets/direct/ipcidr/Owner/cn.txt"] == ["1.0.1.0/24", "1.0.2.0/23"]
        assert errors["parse"] == []


class TestProcessGroupUnrecognizedGate:
    def make_group(self):
        return {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": "https://raw.githubusercontent.com/o/r/ads.txt"})],
        }

    def run_group(self, raw):
        original = main.atomic_write
        main.atomic_write = lambda path, content: None
        try:
            return main.process_group(self.make_group(), {0: raw})
        finally:
            main.atomic_write = original

    def test_classical_file_now_produces_rules(self):
        raw = "DOMAIN-SUFFIX,a.com\nDOMAIN-SUFFIX,b.com\n".encode()
        count, errors = self.run_group(raw)
        assert count == 2
        assert errors["parse"] == []

    def test_high_unrecognized_ratio_flagged(self):
        lines = [f"bad line {i}" for i in range(5)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        count, errors = self.run_group(raw)
        assert count == 1
        assert any("未识别行占比" in reason for _src, reason in errors["parse"])

    def test_low_unrecognized_ratio_not_flagged(self):
        lines = ["bad line"] + [f"good{i}.com" for i in range(50)]
        raw = ("\n".join(lines) + "\n").encode()
        _count, errors = self.run_group(raw)
        assert not any("未识别行占比" in reason for _src, reason in errors["parse"])
