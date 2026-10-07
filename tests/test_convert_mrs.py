import inspect
import os
import subprocess
import sys
from pathlib import Path

import config_loader
import convert_mrs
import pytest

API_LATEST = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"


def asset(name, digest="sha256:" + "0" * 64):
    return {
        "name": name,
        "browser_download_url": f"https://example.com/{name}",
        "digest": digest,
    }


class TestWriteSummary:

    def test_writes_utf8_step_summary(self, work_dir, monkeypatch):
        target = work_dir / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        convert_mrs.write_summary(
            {"success": 2, "failed": 1, "skipped": 0, "total": 3}, 1.23)
        text = target.read_text(encoding="utf-8")
        assert "MRS 转换报告" in text
        assert "1.23" in text

    def test_missing_summary_env_writes_nothing(self, work_dir, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        convert_mrs.write_summary(
            {"success": 0, "failed": 0, "skipped": 0, "total": 0}, 0.5)
        assert not list(work_dir.iterdir())


class TestReleaseApiUrl:
    def test_pinned_version_uses_tags(self):
        assert convert_mrs.release_api_url("v1.19.30", API_LATEST) == \
            "https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.30"

    def test_empty_version_follows_latest(self):
        assert convert_mrs.release_api_url("", API_LATEST) == API_LATEST

    def test_root_without_latest_suffix(self):
        root = "https://api.github.com/repos/X/Y/releases"
        assert convert_mrs.release_api_url("v1.0", root) == f"{root}/tags/v1.0"
        assert convert_mrs.release_api_url("", root) == f"{root}/latest"


class TestSelectKernelAsset:
    def test_matches_derived_asset_name(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz")]
        got = convert_mrs.select_kernel_asset(assets, "v1.19.30")
        assert got["url"] == "https://example.com/mihomo-linux-amd64-v1.19.30.gz"
        assert got["name"] == "mihomo-linux-amd64-v1.19.30.gz"
        assert got["digest"].startswith("sha256:")

    def test_digest_is_carried_through(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz", digest="sha256:deadbeef")]
        got = convert_mrs.select_kernel_asset(assets, "v1.19.30")
        assert got["digest"] == "sha256:deadbeef"

    def test_missing_digest_becomes_empty_string(self):
        assets = [{"name": "mihomo-linux-amd64-v1.19.30.gz",
                   "browser_download_url": "https://example.com/x.gz"}]
        got = convert_mrs.select_kernel_asset(assets, "v1.19.30")
        assert got["digest"] == ""

    def test_other_version_returns_none(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz")]
        assert convert_mrs.select_kernel_asset(assets, "v1.19.31") is None

    def test_variants_are_never_selected(self):
        assets = [
            asset("mihomo-linux-amd64-go1.24-v1.19.32.gz"),
            asset("mihomo-linux-amd64-compatible-v1.19.32.gz"),
            asset("mihomo-linux-amd64-v3-v1.19.32.gz"),
        ]
        assert convert_mrs.select_kernel_asset(assets, "v1.19.32") is None

    def test_exact_name_wins_among_variants(self):
        assets = [
            asset("mihomo-linux-amd64-go1.24-v1.19.32.gz"),
            asset("mihomo-linux-amd64-compatible-v1.19.32.gz"),
            asset("mihomo-linux-amd64-v1.19.32.gz"),
        ]
        got = convert_mrs.select_kernel_asset(assets, "v1.19.32")
        assert got["url"] == "https://example.com/mihomo-linux-amd64-v1.19.32.gz"

    def test_empty_tag_returns_none(self):
        assert convert_mrs.select_kernel_asset([asset("mihomo-linux-amd64-v1.19.32.gz")], "") is None


class TestDownloadKernelDigest:

    def _payload(self, body=b"ELF-BODY-CONTENT"):
        import gzip
        import io

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(body)
        return buf.getvalue()

    def _run(self, work_dir, expected_digest, payload=None):
        import io as _io
        import urllib.request as _ur

        payload = payload if payload is not None else self._payload()
        kernel_dir = work_dir / "k"
        kernel_dir.mkdir(parents=True, exist_ok=True)
        target = kernel_dir / "kernel"

        class FakeResp(_io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original_urlopen = _ur.urlopen
        original_bin = convert_mrs.KERNEL_BIN
        _ur.urlopen = lambda req, timeout=None: FakeResp(payload)
        convert_mrs.KERNEL_BIN = str(target)
        try:
            convert_mrs._download_kernel(
                "https://example.com/k.gz", expected_digest=expected_digest, max_retries=1
            )
            return True, target.read_bytes() if target.exists() else None
        except Exception as e:
            return False, e
        finally:
            _ur.urlopen = original_urlopen
            convert_mrs.KERNEL_BIN = original_bin

    def test_correct_digest_accepted(self, work_dir):
        import hashlib

        payload = self._payload()
        ok, data = self._run(work_dir, "sha256:" + hashlib.sha256(payload).hexdigest())
        assert ok is True
        assert data == b"ELF-BODY-CONTENT"

    def test_wrong_digest_rejected_and_not_written(self, work_dir):
        ok, err = self._run(work_dir, "sha256:" + "0" * 64)
        assert ok is False
        assert "摘要不匹配" in str(err)
        kernel = work_dir / "k" / "kernel"
        assert not kernel.exists(), f"校验失败不得落盘，但 {kernel} 存在"

    def test_uppercase_algorithm_accepted(self, work_dir):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(work_dir, "SHA256:" + hashlib.sha256(payload).hexdigest())
        assert ok is True

    def test_digest_without_algorithm_prefix_accepted(self, work_dir):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(work_dir, hashlib.sha256(payload).hexdigest())
        assert ok is True

    def test_trailing_whitespace_tolerated(self, work_dir):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(work_dir, "sha256:" + hashlib.sha256(payload).hexdigest() + " ")
        assert ok is True, "digest 首尾空白应被 strip"

    def test_other_algorithm_fails_closed(self, work_dir):
        ok, _err = self._run(work_dir, "sha512:" + "0" * 128)
        assert ok is False, "未知算法必须响亮失败，不得静默通过"

    def test_empty_digest_is_refused(self, work_dir):
        ok, err = self._run(work_dir, "")
        assert ok is False, "上游未提供 digest 时必须拒绝下载，不得降级为仅校验解压后哈希"
        assert "digest" in str(err)

    def test_compressed_stream_size_capped(self, work_dir):
        import os as _os

        original = convert_mrs.MAX_KERNEL_BYTES
        convert_mrs.MAX_KERNEL_BYTES = 64
        try:
            ok, err = self._run(work_dir, "", payload=self._payload(_os.urandom(4096)))
        finally:
            convert_mrs.MAX_KERNEL_BYTES = original
        assert ok is False
        assert "压缩包超过上限" in str(err)


class TestVerifyKernelRequireSha:
    def test_missing_sha_rejected_when_required(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        try:
            convert_mrs.verify_kernel_file(str(target), "", require_sha=True)
        except ValueError as e:
            assert "缺少 kernel_sha256" in str(e)
            return
        raise AssertionError("require_sha=True 时缺少哈希应报错")

    def test_missing_sha_allowed_when_not_required(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        actual = convert_mrs.verify_kernel_file(str(target), "", require_sha=False)
        assert len(actual) == 64

    def test_wrong_magic_rejected(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(b"NOTELF" + b"\x00" * 32)
        try:
            convert_mrs.verify_kernel_file(str(target), "")
        except ValueError as e:
            assert "ELF" in str(e)
            return
        raise AssertionError("非 ELF 应报错")

    def test_sha_mismatch_rejected(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        try:
            convert_mrs.verify_kernel_file(str(target), "0" * 64)
        except ValueError as e:
            assert "哈希不匹配" in str(e)
            return
        raise AssertionError("哈希不匹配应报错")


class TestSetConfigField:
    def test_replaces_single_occurrence(self):
        text = 'mihomo:\n  pinned_version: "v1.19.30"\n'
        out = convert_mrs._set_config_field(text, "pinned_version", "v1.19.32")
        assert '"v1.19.32"' in out
        assert "v1.19.30" not in out

    def test_missing_key_exits(self):
        try:
            convert_mrs._set_config_field("mihomo:\n", "pinned_version", "v1")
        except SystemExit:
            return
        raise AssertionError("字段缺失应退出")

    def test_duplicate_key_exits(self):
        text = 'pinned_version: "a"\npinned_version: "b"\n'
        try:
            convert_mrs._set_config_field(text, "pinned_version", "c")
        except SystemExit:
            return
        raise AssertionError("字段重复应退出")


class TestGetRuleType:
    def test_ipcidr(self):
        assert convert_mrs.get_rule_type(["direct", "ipcidr", "Owner", "cn.txt"]) == "ipcidr"

    def test_domain(self):
        assert convert_mrs.get_rule_type(["block", "domain", "Owner", "ads.txt"]) == "domain"

    def test_unknown(self):
        assert convert_mrs.get_rule_type(["misc", "Owner", "x.txt"]) is None


class TestHasValidContent:
    def test_only_comments_is_invalid(self, work_dir):
        target = work_dir / "only-comments.txt"
        target.write_text("# header\n\n# another\n", encoding="utf-8")
        assert convert_mrs.has_valid_content(str(target)) is False

    def test_rule_present_is_valid(self, work_dir):
        target = work_dir / "rules.txt"
        target.write_text("# header\nexample.com\n", encoding="utf-8")
        assert convert_mrs.has_valid_content(str(target)) is True

    def test_missing_file_is_invalid(self, work_dir):
        assert convert_mrs.has_valid_content(str(work_dir / "nope.txt")) is False


class TestBumpConfig:
    def run_with(self, work_dir, tag, assets, config_text, fake_fetch=True):
        cfg = work_dir / "config.yaml"
        cfg.write_text(config_text, encoding="utf-8")
        original_fetch = convert_mrs._fetch_latest_release_info
        original_cwd = os.getcwd()
        try:
            if fake_fetch:
                convert_mrs._fetch_latest_release_info = (
                    lambda max_retries=3, pinned=None: {"tag_name": tag, "assets": assets}
                )
            os.chdir(work_dir)
            convert_mrs.bump_config()
        finally:
            os.chdir(original_cwd)
            convert_mrs._fetch_latest_release_info = original_fetch
        return cfg.read_text(encoding="utf-8")

    def test_noop_when_already_latest(self, work_dir):
        text = 'mihomo:\n  pinned_version: "v9.9.9"\n'
        assert self.run_with(work_dir, "v9.9.9", [], text) == text

    def test_missing_asset_exits(self, work_dir):
        try:
            self.run_with(work_dir, "v9.9.9", [], 'mihomo:\n  pinned_version: "v1.0.0"\n')
        except SystemExit:
            return
        raise AssertionError("无匹配资产应退出")

    def test_missing_digest_exits(self, work_dir):
        assets = [{"name": "mihomo-linux-amd64-v9.9.9.gz",
                   "browser_download_url": "https://example.com/k.gz"}]
        try:
            self.run_with(work_dir, "v9.9.9", assets, 'mihomo:\n  pinned_version: "v1.0.0"\n')
        except SystemExit:
            return
        raise AssertionError("缺少资产 digest 应退出")


class TestBumpConfigExactAsset:
    CONFIG = (
        'mihomo:\n'
        '  pinned_version: "v1.0.0"\n'
        '  kernel_sha256: "' + "a" * 64 + '"\n'
    )

    def _bump(self, monkeypatch, work_dir, tag, assets):
        (work_dir / "config.yaml").write_text(self.CONFIG, encoding="utf-8")
        monkeypatch.setattr(
            convert_mrs, "_fetch_latest_release_info",
            lambda max_retries=3, pinned=None: {"tag_name": tag, "assets": assets},
        )
        monkeypatch.chdir(work_dir)
        convert_mrs.bump_config()

    def test_asset_name_derived_from_tag(self):
        assert convert_mrs.expected_kernel_asset_name("v1.19.32") == \
            "mihomo-linux-amd64-v1.19.32.gz"

    def test_no_guess_when_exact_asset_absent(self, monkeypatch, work_dir):
        assets = [
            asset("mihomo-linux-amd64-v9.9.8.gz"),
            asset("mihomo-linux-amd64-compatible-v9.9.9.gz"),
        ]
        with pytest.raises(SystemExit) as ei:
            self._bump(monkeypatch, work_dir, "v9.9.9", assets)
        assert ei.value.code == 1

    def test_selection_receives_the_target_tag(self, monkeypatch, work_dir):
        seen = {}

        def fake_select(assets, tag_name):
            seen["tag_name"] = tag_name
            return None

        monkeypatch.setattr(convert_mrs, "select_kernel_asset", fake_select)
        with pytest.raises(SystemExit):
            self._bump(monkeypatch, work_dir, "v9.9.9", [asset("mihomo-linux-amd64-v9.9.9.gz")])
        assert seen == {"tag_name": "v9.9.9"}

    def test_exact_name_selects_that_asset(self):
        assets = [asset("mihomo-linux-amd64-v9.9.8.gz"), asset("mihomo-linux-amd64-v9.9.9.gz")]
        got = convert_mrs.select_kernel_asset(assets, "v9.9.9")
        assert got["name"] == "mihomo-linux-amd64-v9.9.9.gz"


class TestNoTokenInjection:
    def test_source_carries_no_token_injection(self):
        src = Path(convert_mrs.__file__).read_text(encoding="utf-8")
        assert "GH_TOKEN" not in src
        assert "Authorization" not in src
        assert "Bearer" not in src

    def test_request_helpers_take_no_headers(self):
        for fn in (convert_mrs._fetch_latest_release_info, convert_mrs._download_kernel):
            names = list(inspect.signature(fn).parameters)
            assert not any(
                k in n for n in names for k in ("header", "auth", "token")
            ), f"{fn.__name__} 不应再接受鉴权参数: {names}"


class TestConfigGuard:
    def break_yaml(self, monkeypatch, work_dir, exists=True):
        cfg = work_dir / "config.yaml"
        if exists:
            cfg.write_text(
                'mihomo:\n  pinned_version: "v1.19.30"\n',
                encoding="utf-8",
            )
        monkeypatch.setattr(config_loader, "_HAS_YAML", False)
        monkeypatch.setattr(config_loader, "_CONFIG_FILE", cfg)
        monkeypatch.setattr(config_loader, "_CONFIG", None)
        monkeypatch.chdir(work_dir)
        return cfg

    def test_config_without_pyyaml_is_not_silently_applied(self, monkeypatch, work_dir):
        self.break_yaml(monkeypatch, work_dir)
        try:
            pinned = config_loader.get("mihomo", "pinned_version", default="")
        except Exception:
            pinned = ""
        assert pinned == "", "PyYAML 缺失时 config.yaml 的钉扎值不得被应用"

    def test_missing_pyyaml_hard_fails(self, monkeypatch, work_dir):
        self.break_yaml(monkeypatch, work_dir)
        with pytest.raises(SystemExit) as ei:
            convert_mrs.ensure_config_usable()
        assert ei.value.code == 1

    def test_main_aborts_before_bump_when_config_ignored(self, monkeypatch, work_dir):
        self.break_yaml(monkeypatch, work_dir)
        called = []
        monkeypatch.setattr(convert_mrs, "bump_config", lambda: called.append(True))
        monkeypatch.setattr(sys, "argv", ["convert_mrs.py", "--bump-config"])
        with pytest.raises(SystemExit) as ei:
            convert_mrs.main()
        assert ei.value.code == 1
        assert called == [], "配置未生效时不得进入 --bump-config"

    def test_no_config_file_is_not_an_error(self, monkeypatch, work_dir):
        self.break_yaml(monkeypatch, work_dir, exists=False)
        convert_mrs.ensure_config_usable()

    def test_yaml_available_is_not_an_error(self, monkeypatch, work_dir):
        cfg = work_dir / "config.yaml"
        cfg.write_text('mihomo:\n  pinned_version: "v1"\n', encoding="utf-8")
        monkeypatch.setattr(config_loader, "_CONFIG_FILE", cfg)
        convert_mrs.ensure_config_usable()


class TestBumpQueriesLatest:

    def test_bump_fetches_latest_not_the_pinned_tag(self, monkeypatch, work_dir):
        seen = []

        def fake_fetch(max_retries=3, pinned=None):
            seen.append(pinned)
            raise RuntimeError("stop-after-recording")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", fake_fetch)
        monkeypatch.chdir(work_dir)
        (work_dir / "config.yaml").write_text(
            'mihomo:\n  pinned_version: "v1.19.30"\n  kernel_sha256: "00"\n',
            encoding="utf-8")
        with pytest.raises(RuntimeError):
            convert_mrs.bump_config()
        assert seen == [""], "bump 路径必须用 /latest（pinned=\"\"）"

    def test_latest_url_differs_from_pinned_url(self):
        assert convert_mrs.release_api_url("", "https://api.github.com/x") == \
            "https://api.github.com/x/latest"
        assert convert_mrs.release_api_url("v1", "https://api.github.com/x") == \
            "https://api.github.com/x/tags/v1"

    def test_bump_actually_requests_the_latest_url(self, monkeypatch, work_dir):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            raise RuntimeError("stop-after-recording")

        monkeypatch.setattr(convert_mrs.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(convert_mrs.time, "sleep", lambda _s: None)
        monkeypatch.chdir(work_dir)
        (work_dir / "config.yaml").write_text(
            'mihomo:\n  pinned_version: "v1.19.30"\n  kernel_sha256: "00"\n',
            encoding="utf-8")
        with pytest.raises(RuntimeError):
            convert_mrs.bump_config()
        assert seen, "bump 路径没有发出任何请求"
        assert seen[0].endswith("/releases/latest"), seen[0]
        assert "/tags/" not in seen[0]


class TestGetLatestMihomo:

    def setup_kernel(self, monkeypatch, work_dir, *, cached_version=None):
        cache = work_dir / "kernel"
        bin_path = cache / "mihomo"
        version_file = cache / "version.txt"
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", cache)
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(bin_path))
        monkeypatch.setattr(convert_mrs, "VERSION_FILE", version_file)
        monkeypatch.setattr(convert_mrs, "EXPECTED_SHA", "a" * 64)
        monkeypatch.setattr(convert_mrs, "PINNED_VERSION", "v1.2.3")
        for name in ("info", "warning", "error", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)
        cache.mkdir(parents=True, exist_ok=True)
        if cached_version is not None:
            version_file.write_text(cached_version, encoding="utf-8")
            bin_path.write_bytes(b"\x7fELF")
        return bin_path, version_file

    def release(self, tag="v1.2.3"):
        return {"tag_name": tag, "assets": [asset("mihomo-linux-amd64-v1.2.3.gz")]}

    def downloaded(self, monkeypatch, bin_path):
        monkeypatch.setattr(convert_mrs, "select_kernel_asset", lambda assets, tag_name: {
            "name": "mihomo-linux-amd64-v1.2.3.gz",
            "url": "https://example.com/kernel.gz",
            "digest": "sha256:" + "b" * 64,
        })
        monkeypatch.setattr(
            convert_mrs, "_download_kernel",
            lambda url, expected_digest=None: bin_path.write_bytes(b"\x7fELF"))

    def test_cache_hit_reuses_kernel_without_download(self, monkeypatch, work_dir):
        _bin_path, _version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: self.release())
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: "Mihomo Meta v1.2.3")
        downloads = []
        monkeypatch.setattr(convert_mrs, "_download_kernel",
                            lambda *a, **k: downloads.append(True))

        convert_mrs.get_latest_mihomo()

        assert downloads == []

    def test_broken_cache_is_replaced_by_download(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(
            monkeypatch, work_dir, cached_version="v1.2.3")
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)
        verifications = []

        def fake_verify(path, expected_sha, require_sha=False):
            verifications.append(require_sha)
            if len(verifications) == 1:
                raise ValueError("缓存损坏")
            return "a" * 64

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

        assert verifications == [True, True]
        assert version_file.read_text(encoding="utf-8") == "v1.2.3"

    def test_fresh_download_installs_kernel(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

        assert version_file.read_text(encoding="utf-8") == "v1.2.3"

    def test_post_download_sha_mismatch_exits(self, monkeypatch, work_dir):
        bin_path, _version_file = self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)

        def fake_verify(*_a, **_k):
            raise ValueError("内核哈希不匹配")

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)

        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1

    def test_fetch_failure_degrades_to_verified_cache(self, monkeypatch, work_dir):
        self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")

        def fake_fetch(*_a, **_k):
            raise RuntimeError("网络不可用")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", fake_fetch)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

    def test_fetch_failure_without_usable_cache_exits(self, monkeypatch, work_dir):
        self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")

        def fake_fetch(*_a, **_k):
            raise RuntimeError("网络不可用")

        def fake_verify(*_a, **_k):
            raise ValueError("缓存校验失败")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", fake_fetch)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)

        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1

    def test_missing_asset_exits(self, monkeypatch, work_dir):
        self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: {"tag_name": "v1.2.3", "assets": []})
        monkeypatch.setattr(convert_mrs, "select_kernel_asset", lambda *a, **k: None)

        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1

    def test_platform_guard_exits_off_linux(self, monkeypatch):
        monkeypatch.setattr(convert_mrs.sys, "platform", "win32")

        with pytest.raises(SystemExit) as exc:
            convert_mrs.ensure_kernel_platform()

        assert exc.value.code == 1

    def test_print_kernel_hash_mode(self, monkeypatch, work_dir, capsys):
        seen = []
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "ensure_config_usable", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo",
                            lambda skip_hash_check=False: seen.append(skip_hash_check))
        monkeypatch.setattr(convert_mrs, "file_sha256", lambda path: "f" * 64)
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", "kernel")
        monkeypatch.setattr(sys, "argv", ["convert_mrs.py", "--print-kernel-hash"])

        convert_mrs.main()

        assert seen == [True]
        assert "f" * 64 in capsys.readouterr().out


class TestSmokeConvert:

    def prepare(self, monkeypatch, work_dir):
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", work_dir)
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(work_dir / "mihomo"))
        for name in ("info", "warning", "error", "success", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)

    def test_success_cleans_up_temp_files(self, monkeypatch, work_dir):
        self.prepare(monkeypatch, work_dir)

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_text("mrs", encoding="utf-8")

        monkeypatch.setattr(convert_mrs.subprocess, "run", fake_run)

        convert_mrs._smoke_convert()

        assert not (work_dir / "smoke-input.txt").exists()
        assert not (work_dir / "smoke-output.mrs").exists()

    def test_kernel_failure_exits(self, monkeypatch, work_dir):
        self.prepare(monkeypatch, work_dir)

        def fake_run(cmd, **_kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")

        monkeypatch.setattr(convert_mrs.subprocess, "run", fake_run)

        with pytest.raises(SystemExit) as exc:
            convert_mrs._smoke_convert()

        assert exc.value.code == 1

    def test_missing_output_exits(self, monkeypatch, work_dir):
        self.prepare(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs.subprocess, "run", lambda cmd, **k: None)

        with pytest.raises(SystemExit) as exc:
            convert_mrs._smoke_convert()

        assert exc.value.code == 1


class TestBumpConfigSuccess:

    def test_full_bump_writes_pin_and_outputs(self, monkeypatch, work_dir):
        cfg = work_dir / "config.yaml"
        cfg.write_text(
            'pinned_version: "v1.0.0"\n'
            'kernel_sha256: "old"\n',
            encoding="utf-8",
        )
        gh_output = work_dir / "gh_output.txt"
        monkeypatch.chdir(work_dir)
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", work_dir / "cache")
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(work_dir / "cache" / "mihomo"))
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info",
                            lambda *a, **k: {"tag_name": "v9.9.9", "assets": []})
        monkeypatch.setattr(convert_mrs, "select_kernel_asset", lambda assets, tag_name: {
            "name": "mihomo-linux-amd64-v9.9.9.gz",
            "url": "https://example.com/kernel.gz",
            "digest": "sha256:" + "c" * 64,
        })
        downloads = []
        monkeypatch.setattr(
            convert_mrs, "_download_kernel",
            lambda url, expected_digest=None: downloads.append((url, expected_digest)))
        verifications = []
        monkeypatch.setattr(
            convert_mrs, "verify_kernel_file",
            lambda path, expected_sha, *a, **k: verifications.append(
                (str(path), expected_sha)) or "d" * 64)
        monkeypatch.setattr(convert_mrs, "file_sha256", lambda path: "e" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: "Mihomo Meta v9.9.9")
        monkeypatch.setattr(convert_mrs, "_smoke_convert", lambda: None)
        for name in ("info", "warning", "error", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)

        convert_mrs.bump_config()

        text = cfg.read_text(encoding="utf-8")
        assert 'pinned_version: "v9.9.9"' in text
        assert f'kernel_sha256: "{"e" * 64}"' in text
        assert gh_output.read_text(encoding="utf-8") == "changed=true\ntag=v9.9.9\n"
        assert downloads == [("https://example.com/kernel.gz", "sha256:" + "c" * 64)]
        assert verifications == [(str(work_dir / "cache" / "mihomo"), "")]
