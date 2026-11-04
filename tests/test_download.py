import asyncio
import socket
from datetime import UTC

import main
import pytest


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, chunks, headers=None, url=None, history=None):
        self.status = status
        self.content = FakeStream(chunks)
        self.headers = headers or {}
        self.url = url or "https://example.com/list.txt"
        self.history = history or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


PUBLIC_IPV4 = "93.184.216.34"


class FakeConnector:
    """模拟连接器 resolver：默认把所有主机名解析成一个公网地址，可按主机名覆盖。"""

    def __init__(self, resolved=None):
        self.resolved = resolved
        self.queries = []

    def addresses_for(self, host):
        if isinstance(self.resolved, dict):
            return list(self.resolved.get(host, [PUBLIC_IPV4]))
        if self.resolved is None:
            return [PUBLIC_IPV4]
        return list(self.resolved)

    async def _resolve_host(self, host, port):
        self.queries.append((host, port))
        return [{"host": address, "hostname": host, "port": port} for address in self.addresses_for(host)]


class FakeSession:
    def __init__(self, response, resolved=None):
        self._responses = list(response) if isinstance(response, (list, tuple)) else [response]
        self.calls = []
        self.connector = FakeConnector(resolved)

    def get(self, url, timeout=None, headers=None, **kw):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "allow_redirects": kw.get("allow_redirects"),
                "timeout": timeout,
            }
        )
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


TASK = {"url": "https://example.com/list.txt", "policy": "block", "type": "domain"}


def resolve_result(address, hostname="example.com", port=443):
    return {
        "host": address,
        "hostname": hostname,
        "port": port,
        "family": socket.AF_INET,
        "proto": socket.IPPROTO_TCP,
        "flags": 0,
    }


class _InnerResolver:
    """按批次返回解析结果，用于模拟“预检公网、建连私网”的 rebinding。"""

    def __init__(self, batches):
        self._batches = [list(batch) for batch in batches]
        self.calls = []
        self.closed = False

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.calls.append((host, port))
        index = min(len(self.calls) - 1, len(self._batches) - 1)
        return [dict(item) for item in self._batches[index]]

    async def close(self):
        self.closed = True


def download(chunks, status=200, headers=None):
    session = FakeSession(FakeResponse(status, chunks, headers))
    return asyncio.run(main.download_one(session, TASK))


class TestHttpsRedirects:
    def test_final_http_url_is_rejected(self):
        session = FakeSession(FakeResponse(200, [b"example.com\n"], url="http://mirror.example.com/list.txt"))
        _task, content, err = asyncio.run(main.download_one(session, TASK))
        assert content is None
        assert "HTTPS" in err

    def test_http_hop_in_history_is_rejected(self):
        history = [FakeResponse(302, [], url="http://mirror.example.com/list.txt")]
        session = FakeSession(FakeResponse(200, [b"example.com\n"], history=history))
        _task, content, err = asyncio.run(main.download_one(session, TASK))
        assert content is None
        assert "HTTPS" in err

    def test_https_to_http_redirect_never_requests_http(self):
        session = FakeSession(FakeResponse(302, [], {"Location": "http://mirror.example.com/list.txt"}))

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "HTTPS" in err
        assert [call["url"] for call in session.calls] == [TASK["url"]]
        assert session.calls[0]["allow_redirects"] is False

    def test_relative_redirect_re_applies_auth_headers(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        target = "https://raw.githubusercontent.com/o/r/main/list.txt"
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": "/o/r/main/list.txt"}),
                FakeResponse(200, [b"example.com\n"], url=target),
            ]
        )
        task = {**TASK, "url": "https://raw.githubusercontent.com/start/list.txt"}

        _task, content, err = asyncio.run(main.download_one(session, task))

        assert err is None
        assert content == b"example.com\n"
        assert [call["url"] for call in session.calls] == [task["url"], target]
        assert all(call["headers"]["Authorization"] == "Bearer tok" for call in session.calls)
        assert all(call["allow_redirects"] is False for call in session.calls)

    def test_cross_host_redirect_drops_auth_headers(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        target = "https://release-assets.githubusercontent.com/github-production-release-asset/file"
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": target}),
                FakeResponse(200, [b"example.com\n"], url=target),
            ]
        )
        task = {
            **TASK,
            "url": "https://github.com/o/r/releases/download/tag/list.txt",
        }

        _task, content, err = asyncio.run(main.download_one(session, task))

        assert err is None
        assert content == b"example.com\n"
        assert session.calls[0]["headers"]["Authorization"] == "Bearer tok"
        assert "Authorization" not in session.calls[1]["headers"]

    def test_redirect_to_untrusted_host_drops_auth_headers(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": "https://example.com/next.txt"}),
                FakeResponse(200, [b"example.com\n"], url="https://example.com/next.txt"),
            ]
        )
        task = {**TASK, "url": "https://raw.githubusercontent.com/o/r/main/list.txt"}

        _task, content, err = asyncio.run(main.download_one(session, task))

        assert err is None
        assert content == b"example.com\n"
        assert session.calls[0]["headers"]["Authorization"] == "Bearer tok"
        assert "Authorization" not in session.calls[1]["headers"]

    def test_redirect_hops_are_bounded(self):
        responses = [FakeResponse(302, [], {"Location": f"/hop-{i}.txt"}) for i in range(20)]
        session = FakeSession(responses)

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "重定向" in err
        assert len(session.calls) == main.MAX_REDIRECTS + 1


class TestTargetAddressValidation:
    """每一跳请求前按 IP 属性/端口/userinfo 校验，且主机名必须由连接器 resolver 解析后再判定。"""

    def redirect_to(self, location, resolved=None):
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": location}),
                FakeResponse(200, [b"example.com\n"], url=location),
            ],
            resolved=resolved,
        )
        _task, content, err = asyncio.run(main.download_one(session, TASK))
        return session, content, err

    @pytest.mark.parametrize(
        "target",
        [
            "https://127.0.0.1/list.txt",
            "https://10.0.0.5/list.txt",
            "https://172.16.3.4/list.txt",
            "https://192.168.1.10/list.txt",
            "https://169.254.169.254/latest/meta-data/",
            "https://0.0.0.0/list.txt",
            "https://[::1]/list.txt",
            "https://[fe80::1]/list.txt",
            "https://[fc00::1]/list.txt",
        ],
    )
    def test_private_loopback_link_local_redirect_never_requested(self, target):
        session, content, err = self.redirect_to(target)

        assert content is None
        assert [call["url"] for call in session.calls] == [TASK["url"]]
        assert "非公网" in err

    @pytest.mark.parametrize("url", ["https://127.0.0.1/list.txt", "https://[::1]/list.txt"])
    def test_source_ip_literal_rejected_before_request(self, url):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))

        _task, content, err = asyncio.run(main.download_one(session, {**TASK, "url": url}))

        assert content is None
        assert "非公网" in err
        assert session.calls == []

    def test_unexpected_port_rejected(self):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))

        task = {**TASK, "url": "https://example.com:8443/list.txt"}
        _task, content, err = asyncio.run(main.download_one(session, task))

        assert content is None
        assert "端口" in err
        assert session.calls == []

    def test_redirect_to_unexpected_port_rejected(self):
        session, content, err = self.redirect_to("https://cdn.example.net:8443/list.txt")

        assert content is None
        assert "端口" in err
        assert [call["url"] for call in session.calls] == [TASK["url"]]

    def test_hostname_resolving_to_private_address_rejected(self):
        session, content, err = self.redirect_to(
            "https://internal.example/list.txt", resolved={"internal.example": ["10.0.0.7"]}
        )

        assert content is None
        assert "非公网" in err
        assert [call["url"] for call in session.calls] == [TASK["url"]]

    def test_mixed_public_and_private_resolution_rejected(self):
        session, content, err = self.redirect_to(
            "https://rebind.example/list.txt",
            resolved={"rebind.example": [PUBLIC_IPV4, "127.0.0.1"]},
        )

        assert content is None
        assert "非公网" in err
        assert [call["url"] for call in session.calls] == [TASK["url"]]

    def test_unresolvable_target_fails_closed(self):
        session, content, err = self.redirect_to(
            "https://internal.example/list.txt", resolved={"internal.example": []}
        )

        assert content is None
        assert "fail-closed" in err
        assert [call["url"] for call in session.calls] == [TASK["url"]]

    def test_session_without_connector_fails_closed(self):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))
        session.connector = None

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "fail-closed" in err
        assert session.calls == []

    def test_ambient_proxy_does_not_skip_local_validation(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        session = FakeSession(FakeResponse(200, [b"example.com\n"]), resolved=["10.0.0.7"])

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "非公网" in err
        assert session.calls == []

    def test_proxy_env_cannot_bypass_private_literal(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))

        _task, content, err = asyncio.run(main.download_one(session, {**TASK, "url": "https://10.0.0.5/list.txt"}))

        assert content is None
        assert "非公网" in err
        assert session.calls == []

    def test_idna_hostname_is_resolved_as_ascii(self):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))
        task = {**TASK, "url": "https://bücher.example/list.txt"}

        _task, content, err = asyncio.run(main.download_one(session, task))

        assert err is None
        assert content == b"example.com\n"
        # 与 yarl 的 raw_host / 连接器 DNS 缓存键保持一致
        assert session.connector.queries == [("xn--bcher-kva.example", 443)]

    def test_ascii_hostname_is_passed_through_unchanged(self):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))
        task = {**TASK, "url": "https://raw_source.example/list.txt"}

        _task, _content, err = asyncio.run(main.download_one(session, task))

        assert err is None
        assert session.connector.queries == [("raw_source.example", 443)]

    def test_connect_time_resolver_rejection_is_reported_without_retry(self):
        class RebindingSession:
            connector = FakeConnector()

            def __init__(self):
                self.calls = []

            def get(self, url, **kw):
                self.calls.append(url)
                raise main.TargetResolutionRejected("建连时解析到非公网地址")

        session = RebindingSession()

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "拒绝目标地址" in err
        assert "非公网" in err
        assert len(session.calls) == 1

    def test_public_cross_host_redirect_still_allowed(self):
        target = "https://cdn.example.net/list.txt"
        session, content, err = self.redirect_to(target)

        assert err is None
        assert content == b"example.com\n"
        assert [call["url"] for call in session.calls] == [TASK["url"], target]


class TestConnectorResolverPolicy:
    """连接器实际使用的 resolver 必须逐次校验：非公网/混合/空结果一律拒绝。"""

    def resolver(self, batches):
        inner = _InnerResolver(batches)
        return main._PublicTargetResolver(inner), inner

    def test_public_resolution_passes_through(self):
        wrapper, inner = self.resolver([[resolve_result(PUBLIC_IPV4)]])

        results = asyncio.run(wrapper.resolve("example.com", 443))

        assert results == [resolve_result(PUBLIC_IPV4)]
        assert inner.calls == [("example.com", 443)]

    @pytest.mark.parametrize(
        "addresses",
        [[], ["10.0.0.7"], [PUBLIC_IPV4, "127.0.0.1"], ["169.254.169.254"], ["fe80::1"], ["::1"]],
    )
    def test_non_public_mixed_or_empty_resolution_rejected(self, addresses):
        wrapper, _inner = self.resolver([[resolve_result(address) for address in addresses]])

        with pytest.raises(main.TargetResolutionRejected):
            asyncio.run(wrapper.resolve("example.com", 443))

    def test_close_delegates_to_inner_resolver(self):
        wrapper, inner = self.resolver([[resolve_result(PUBLIC_IPV4)]])

        asyncio.run(wrapper.close())

        assert inner.closed is True

    def test_rebinding_between_precheck_and_connect_is_rejected(self):
        wrapper, inner = self.resolver(
            [
                [resolve_result(PUBLIC_IPV4)],
                [resolve_result("127.0.0.1")],
            ]
        )

        async def run():
            connector = main.aiohttp.TCPConnector(resolver=wrapper, use_dns_cache=False)
            try:
                first = await connector._resolve_host("example.com", 443)
                with pytest.raises(main.TargetResolutionRejected):
                    await connector._resolve_host("example.com", 443)
                return first
            finally:
                await connector.close()

        first = asyncio.run(run())

        assert [item["host"] for item in first] == [PUBLIC_IPV4]
        assert len(inner.calls) == 2


class TestDownloadAllNetworkPolicy:
    def test_env_proxy_disabled_and_validating_resolver_installed(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
        captured = {}

        class Session:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(main.aiohttp, "ClientSession", Session)

        asyncio.run(main.download_all([]))

        assert captured["trust_env"] is False
        assert isinstance(captured["connector"]._resolver, main._PublicTargetResolver)


class TestUserinfoNeverRequestedOrEchoed:
    USERINFO_URL = "https://acct:" + "s3cr3t-pass" + "@localhost/list.txt"

    def test_source_userinfo_rejected_without_echoing_password(self, capsys):
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))

        _task, content, err = asyncio.run(main.download_one(session, {**TASK, "url": self.USERINFO_URL}))

        assert content is None
        assert "userinfo" in err
        assert "s3cr3t-pass" not in err
        assert "s3cr3t-pass" not in capsys.readouterr().out
        assert "localhost/list.txt" in err
        assert session.calls == []

    def test_redirect_userinfo_rejected_without_echoing_password(self, capsys):
        target = self.USERINFO_URL
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": target}),
                FakeResponse(200, [b"example.com\n"], url=target),
            ]
        )

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "userinfo" in err
        assert "s3cr3t-pass" not in err
        assert "s3cr3t-pass" not in capsys.readouterr().out
        assert [call["url"] for call in session.calls] == [TASK["url"]]


class TestSourceTotalDeadline:
    def test_deadline_is_ninety_seconds(self):
        assert main.SOURCE_TOTAL_TIMEOUT == 90.0

    def test_expired_budget_makes_no_request(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 0.0)
        session = FakeSession(FakeResponse(200, [b"example.com\n"]))

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "总时限" in err
        assert session.calls == []

    def test_target_validation_is_bounded_by_remaining_budget(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 0.05)

        class BlockingConnector:
            async def _resolve_host(self, host, port):
                await asyncio.sleep(10)
                return [resolve_result(PUBLIC_IPV4, hostname=host, port=port)]

        session = FakeSession(FakeResponse(200, [b"example.com\n"]))
        session.connector = BlockingConnector()

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "总时限" in err
        assert session.calls == []

    def test_budget_is_recomputed_after_target_validation(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 5.0)

        class SlowConnector:
            async def _resolve_host(self, host, port):
                await asyncio.sleep(0.3)
                return [resolve_result(PUBLIC_IPV4, hostname=host, port=port)]

        session = FakeSession(FakeResponse(200, [b"example.com\n"]))
        session.connector = SlowConnector()

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert err is None
        assert content == b"example.com\n"
        # 校验耗时已扣减：ClientTimeout 不能还是校验前的 5s
        assert session.calls[0]["timeout"].total <= main.SOURCE_TOTAL_TIMEOUT - 0.2

    def test_request_timeout_uses_remaining_budget(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 5.0)
        session = FakeSession(
            [
                FakeResponse(302, [], {"Location": "/next.txt"}),
                FakeResponse(200, [b"example.com\n"], url="https://example.com/next.txt"),
            ]
        )

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert err is None
        assert content == b"example.com\n"
        assert len(session.calls) == 2
        for call in session.calls:
            timeout = call["timeout"]
            assert 0 < timeout.total <= main.SOURCE_TOTAL_TIMEOUT
            # 单次连接/读取仍受 network.timeout_seconds 上限约束，总超时只由剩余预算决定
            assert timeout.sock_connect == timeout.sock_read == min(main.TIMEOUT, timeout.total)

    def test_retry_after_beyond_deadline_is_not_waited(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 5.0)
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
        session = FakeSession(FakeResponse(429, [b"x"], {"Retry-After": "30"}))

        _task, content, err = asyncio.run(main.download_one(session, TASK))

        assert content is None
        assert "总时限" in err
        assert slept == []
        assert len(session.calls) == 1

    def test_transport_retry_wait_respects_deadline(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCE_TOTAL_TIMEOUT", 1.0)
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        class BoomSession:
            connector = FakeConnector()

            def get(self, *args, **kw):
                raise main.aiohttp.ClientError("boom")

        monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

        _task, content, err = asyncio.run(main.download_one(BoomSession(), TASK))

        assert content is None
        assert "总时限" in err
        assert slept == []


def test_request_includes_explicit_user_agent():
    session = FakeSession(FakeResponse(200, [b"example.com\n"]))

    asyncio.run(main.download_one(session, TASK))

    assert session.calls[0]["headers"]["User-Agent"] == main.USER_AGENT


def test_download_session_accepts_large_github_headers(monkeypatch):
    captured = {}

    class Session:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main.aiohttp, "ClientSession", Session)

    asyncio.run(main.download_all([]))

    assert captured["max_field_size"] > 9217


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
        _task, content, err = download([b"a\0b\0c"])
        assert content is None
        assert err == "非文本响应"

    def test_nul_beyond_sample_window_still_rejected(self):
        _task, content, err = download([b"a" * 70000, b"\0"])
        assert content is None
        assert err == "非文本响应"


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
        _s, (_task, content, err), slept = self.retry_with(404)
        assert slept == []
        assert content is None
        assert err == "HTTP 404"

    @pytest.mark.parametrize("status", [408, 425, 429, 503])
    def test_retryable_status_is_retried(self, status):
        _s, _r, slept = self.retry_with(status)
        assert len(slept) == 2

    def test_retry_after_seconds_honoured(self):
        _s, _r, slept = self.retry_with(429, {"Retry-After": "30"})
        assert slept == [30, 30]

    def test_retry_after_over_budget_is_not_retried(self):
        session, (_task, content, err), slept = self.retry_with(429, {"Retry-After": "3600"})
        assert len(session.calls) == 1
        assert slept == []
        assert content is None
        assert "Retry-After" in (err or "")

    def test_retry_after_zero_means_no_wait(self):
        _s, _r, slept = self.retry_with(503, {"Retry-After": "0"})
        assert slept == []

    def test_retry_after_http_date(self):
        from datetime import datetime, timedelta
        from email.utils import format_datetime

        when = datetime.now(UTC) + timedelta(seconds=20)
        _s, _r, slept = self.retry_with(429, {"Retry-After": format_datetime(when)})
        assert all(15 <= s <= 21 for s in slept), slept

    def test_invalid_retry_after_falls_back(self):
        _s, _r, slept = self.retry_with(429, {"Retry-After": "not-a-date"})
        assert slept == [1, 2]


class TestSourceDisambiguation:
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
            assert len(g["members"]) == 1 or len(slugs) == 1, f"多来源共享输出: {g['path']} <- {slugs}"

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
        assert written[main.ensure_within_rulesets(group["path"])] == [
            "# Source: https://raw.githubusercontent.com/o/r/cn.txt",
            "1.0.1.0/24",
            "1.0.2.0/23",
        ]
        assert errors["parse"] == []


class TestProcessGroupUnrecognizedGate:
    def make_group(self):
        return {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": "https://raw.githubusercontent.com/o/r/ads.txt"})],
            "sources": ["https://raw.githubusercontent.com/o/r/ads.txt"],
        }

    def run_group(self, raw):
        original = main.atomic_write
        main.atomic_write = lambda path, content: None
        try:
            return main.process_group(self.make_group(), {0: raw})
        finally:
            main.atomic_write = original

    def test_classical_file_now_produces_rules(self):
        raw = b"DOMAIN-SUFFIX,a.com\nDOMAIN-SUFFIX,b.com\n"
        count, errors = self.run_group(raw)
        assert count == 2
        assert errors["parse"] == []

    def test_large_unrecognized_count_flagged(self):
        lines = [f"bad line {i}" for i in range(25)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        count, errors = self.run_group(raw)
        assert count is None
        assert any("丢弃行合计占比" in reason for _src, reason in errors["parse"])

    def test_unexpressible_rules_fail_gate_without_writing(self, monkeypatch):
        lines = [f"DOMAIN-KEYWORD,ads-{i}.example.com" for i in range(25)] + ["good.com"]
        writes = []
        monkeypatch.setattr(main, "atomic_write", lambda *args: writes.append(args))

        count, errors = main.process_group(self.make_group(), {0: ("\n".join(lines) + "\n").encode()})

        assert count is None
        assert any("丢弃行合计占比" in reason for _src, reason in errors["parse"])
        assert writes == []

    def test_small_unexpressible_count_only_warns(self):
        lines = [f"DOMAIN-KEYWORD,ads-{i}.example.com" for i in range(5)] + ["good.com"]

        count, errors = self.run_group(("\n".join(lines) + "\n").encode())

        assert count == 1
        assert not any("丢弃行合计占比" in reason for _src, reason in errors["parse"])

    def test_gate_failure_does_not_write_output(self):
        lines = [f"bad line {i}" for i in range(25)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        written = []
        original = main.atomic_write
        main.atomic_write = lambda path, content: written.append(path)
        try:
            count, _errors = main.process_group(self.make_group(), {0: raw})
        finally:
            main.atomic_write = original
        assert count is None
        assert written == []

    def test_unrecognized_gate_entry_uses_source_url_not_path(self):
        lines = [f"bad line {i}" for i in range(25)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        _count, errors = self.run_group(raw)
        flagged = [src for src, reason in errors["parse"] if "丢弃行合计占比" in reason]
        assert flagged == ["https://raw.githubusercontent.com/o/r/ads.txt"]

    def test_unrecognized_gate_label_uses_contributing_member(self):
        dead = "https://raw.githubusercontent.com/o/r/dead.txt"
        alive = "https://raw.githubusercontent.com/o/r/ads.txt"
        group = {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": dead}), (1, {"url": alive})],
            "sources": [dead, alive],
        }
        lines = [f"bad line {i}" for i in range(25)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        original = main.atomic_write
        main.atomic_write = lambda path, content: None
        try:
            count, errors = main.process_group(group, {1: raw})
        finally:
            main.atomic_write = original
        assert count is None
        assert errors["download"] == [(dead, "下载失败")]
        flagged = [src for src, reason in errors["parse"] if "丢弃行合计占比" in reason]
        assert flagged == [alive]

    def test_header_only_lists_contributing_sources(self):
        dead = "https://raw.githubusercontent.com/o/r/dead.txt"
        alive = "https://raw.githubusercontent.com/o/r/ads.txt"
        group = {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": dead}), (1, {"url": alive})],
            "sources": [dead, alive],
        }
        written = {}
        original = main.atomic_write
        main.atomic_write = lambda path, content: written.update({path: content})
        try:
            count, errors = main.process_group(group, {1: b"good.com\n"})
        finally:
            main.atomic_write = original
        assert count == 1
        assert written[main.ensure_within_rulesets(group["path"])] == ["# Source: " + alive, "good.com"]
        assert errors["download"] == [(dead, "下载失败")]

    def test_header_skips_source_with_no_parsed_lines(self):
        dead = "https://raw.githubusercontent.com/o/r/empty.txt"
        alive = "https://raw.githubusercontent.com/o/r/ads.txt"
        group = {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": dead}), (1, {"url": alive})],
            "sources": [dead, alive],
        }
        written = {}
        original = main.atomic_write
        main.atomic_write = lambda path, content: written.update({path: content})
        try:
            count, errors = main.process_group(group, {0: b"# comment only\n", 1: b"good.com\n"})
        finally:
            main.atomic_write = original
        assert count == 1
        assert written[main.ensure_within_rulesets(group["path"])] == ["# Source: " + alive, "good.com"]
        # 非空 raw 但解析为空必须显式失败一次，不再静默通过
        assert errors["parse"] == [(dead, "未解析出任何行（疑似空规则集或格式变更）")]

    def test_download_error_reason_is_preserved(self):
        group = self.make_group()
        original = main.atomic_write
        main.atomic_write = lambda path, content: None
        try:
            count, errors = main.process_group(group, {}, {0: "HTTP 404"})
        finally:
            main.atomic_write = original
        assert count is None
        assert errors["download"] == [("https://raw.githubusercontent.com/o/r/ads.txt", "HTTP 404")]

    def test_small_sample_ratio_only_warns(self):
        lines = [f"bad line {i}" for i in range(5)] + ["good.com"]
        raw = ("\n".join(lines) + "\n").encode()
        count, errors = self.run_group(raw)
        assert count == 1
        assert not any("丢弃行合计占比" in reason for _src, reason in errors["parse"])

    def test_low_unrecognized_ratio_not_flagged(self):
        lines = ["bad line"] + [f"good{i}.com" for i in range(50)]
        raw = ("\n".join(lines) + "\n").encode()
        _count, errors = self.run_group(raw)
        assert not any("丢弃行合计占比" in reason for _src, reason in errors["parse"])


class TestProcessGroupIpUnrecognizedGate:
    def make_group(self):
        return {
            "path": "rulesets/direct/ipcidr/Owner/cn.txt",
            "policy": "direct",
            "type": "ipcidr",
            "members": [(0, {"url": "https://raw.githubusercontent.com/o/r/cn.txt"})],
            "sources": ["https://raw.githubusercontent.com/o/r/cn.txt"],
        }

    def run_group(self, raw):
        original = main.atomic_write
        main.atomic_write = lambda path, content: None
        try:
            return main.process_group(self.make_group(), {0: raw})
        finally:
            main.atomic_write = original

    def test_large_invalid_cidr_count_flagged(self):
        lines = [f"garbage row {i}" for i in range(25)] + ["1.0.1.0/24"]
        raw = ("\n".join(lines) + "\n").encode()
        count, errors = self.run_group(raw)
        assert count is None
        flagged = [src for src, reason in errors["parse"] if "丢弃行合计占比" in reason]
        assert flagged == ["https://raw.githubusercontent.com/o/r/cn.txt"]

    def test_unexpressible_rules_fail_gate_without_writing(self, monkeypatch):
        lines = [f"DOMAIN-SUFFIX,ads-{i}.example.com" for i in range(25)] + ["1.0.1.0/24"]
        writes = []
        monkeypatch.setattr(main, "atomic_write", lambda *args: writes.append(args))

        count, errors = main.process_group(self.make_group(), {0: ("\n".join(lines) + "\n").encode()})

        assert count is None
        assert any("丢弃行合计占比" in reason for _src, reason in errors["parse"])
        assert writes == []

    def test_default_routes_are_not_unexpected_drops(self):
        lines = ["0.0.0.0/0"] * 25 + ["1.0.1.0/24"]

        count, errors = self.run_group(("\n".join(lines) + "\n").encode())

        assert count == 1
        assert not any("丢弃行合计占比" in reason for _src, reason in errors["parse"])

    def test_low_invalid_ratio_not_flagged(self):
        lines = ["garbage row"] + [f"10.{i}.0.0/16" for i in range(0, 100, 2)]
        raw = ("\n".join(lines) + "\n").encode()
        count, errors = self.run_group(raw)
        assert count == 50
        assert not any("丢弃行合计占比" in reason for _src, reason in errors["parse"])


class _Boom(BaseException):
    pass


class TestDownloadAll:
    def test_normal_results_preserved(self, monkeypatch):
        async def fake_download_one(_session, task):
            return (task, b"ok", None)

        monkeypatch.setattr(main, "download_one", fake_download_one)
        tasks = [{"url": "https://example.com/a.txt"}, {"url": "https://example.com/b.txt"}]

        results = asyncio.run(main.download_all(tasks))

        assert results == [(tasks[0], b"ok", None), (tasks[1], b"ok", None)]

    def test_base_exception_is_normalized(self, monkeypatch):
        async def fake_download_one(_session, task):
            if task["url"].endswith("bad.txt"):
                raise _Boom("boom")
            return (task, b"ok", None)

        monkeypatch.setattr(main, "download_one", fake_download_one)
        tasks = [{"url": "https://example.com/good.txt"}, {"url": "https://example.com/bad.txt"}]

        results = asyncio.run(main.download_all(tasks))

        assert results[0] == (tasks[0], b"ok", None)
        assert results[1][0] is tasks[1]
        assert results[1][1] is None
        assert results[1][2] == "_Boom: boom"


class TestEmptySourceParseErrors:
    """ISS-15：raw 非空但解析为空的成员各记一次 parse error；全空时不重复、空 raw 只汇总一次。"""

    DEAD = "https://raw.githubusercontent.com/o/r/empty.txt"
    ALIVE = "https://raw.githubusercontent.com/o/r/ads.txt"
    REASON = "未解析出任何行（疑似空规则集或格式变更）"

    def group(self):
        return {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "members": [(0, {"url": self.DEAD}), (1, {"url": self.ALIVE})],
            "sources": [self.DEAD, self.ALIVE],
        }

    def run_group(self, raw_by_index, monkeypatch):
        monkeypatch.setattr(main, "atomic_write", lambda *args: None)
        return main.process_group(self.group(), raw_by_index)

    def test_one_empty_source_is_reported_while_other_is_kept(self, monkeypatch):
        count, errors = self.run_group({0: b"# only comment\n", 1: b"good.com\n"}, monkeypatch)

        assert count == 1
        assert errors["parse"] == [(self.DEAD, self.REASON)]

    def test_all_sources_empty_report_each_member_exactly_once(self, monkeypatch):
        count, errors = self.run_group({0: b"# only comment\n", 1: b"! adblock comment\n"}, monkeypatch)

        assert count is None
        assert errors["parse"] == [(self.DEAD, self.REASON), (self.ALIVE, self.REASON)]

    def test_empty_raw_payloads_fail_once_without_duplicates(self, monkeypatch):
        count, errors = self.run_group({0: b"", 1: b""}, monkeypatch)

        assert count is None
        assert errors["parse"] == [(self.DEAD, self.REASON)]
