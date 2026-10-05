import asyncio

import main


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, chunks):
        self.status = status
        self.content = FakeStream(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, url, timeout=None):
        return self._response


TASK = {"url": "https://example.com/list.txt", "policy": "block", "type": "domain"}


def download(chunks, status=200):
    session = FakeSession(FakeResponse(status, chunks))
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
