import hashlib
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


def elf_binary(machine):
    data = bytearray(20)
    data[:4] = b"\x7fELF"
    data[18:20] = machine.to_bytes(2, "little")
    return bytes(data)


class TestProductsAnchored:
    def test_outside_repo_is_rejected(self, monkeypatch):
        monkeypatch.setattr(convert_mrs, "error", lambda *a, **k: None)
        monkeypatch.setattr(convert_mrs, "gh_error", lambda *a, **k: None)
        # 受限环境 TMP 不可写时 gettempdir() 会回退到仓库根，改用仓库上级目录保证在仓库外
        outside = Path(__file__).resolve().parents[2] / "ruleset-foundry-outside-repo"
        monkeypatch.setattr(convert_mrs, "SRC_ROOT", str(outside))
        monkeypatch.setattr(convert_mrs, "DST_ROOT", str(outside))
        with pytest.raises(SystemExit) as exc:
            convert_mrs.ensure_products_anchored()
        assert exc.value.code == 1

    def test_subdir_is_accepted(self, monkeypatch):
        repo = Path(__file__).resolve().parent.parent
        monkeypatch.setattr(convert_mrs, "SRC_ROOT", str(repo / "merged-rules"))
        monkeypatch.setattr(convert_mrs, "DST_ROOT", str(repo / "merged-rules-mrs"))
        convert_mrs.ensure_products_anchored()


class TestWriteSummary:
    def test_writes_utf8_step_summary(self, work_dir, monkeypatch):
        target = work_dir / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        convert_mrs.write_summary({"success": 2, "failed": 1, "skipped": 0, "total": 3}, 1.23)
        text = target.read_text(encoding="utf-8")
        assert "MRS 转换报告" in text
        assert "1.23" in text

    def test_missing_summary_env_writes_nothing(self, work_dir, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        convert_mrs.write_summary({"success": 0, "failed": 0, "skipped": 0, "total": 0}, 0.5)
        assert not list(work_dir.iterdir())


class TestReleaseApiUrl:
    def test_pinned_version_uses_tags(self):
        assert (
            convert_mrs.release_api_url("v1.19.30", API_LATEST)
            == "https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.30"
        )

    def test_empty_version_follows_latest(self):
        assert convert_mrs.release_api_url("", API_LATEST) == API_LATEST

    def test_root_without_latest_suffix(self):
        root = "https://api.github.com/repos/X/Y/releases"
        assert convert_mrs.release_api_url("v1.0", root) == f"{root}/tags/v1.0"
        assert convert_mrs.release_api_url("", root) == f"{root}/latest"


class TestReleaseApiHttps:
    def test_http_redirect_is_rejected(self, monkeypatch):
        class FakeResp:
            url = "http://api.github.com/repos/MetaCubeX/mihomo/releases/latest"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _size=-1):
                return b"{}"

        monkeypatch.setattr(convert_mrs, "_open_url", lambda req, timeout=None: FakeResp())
        with pytest.raises(ValueError, match="https"):
            convert_mrs._fetch_latest_release_info(max_retries=1)

    def test_http_request_url_is_rejected_before_fetch(self, monkeypatch):
        called = []
        monkeypatch.setattr(convert_mrs, "_open_url", lambda *a, **k: called.append(True))
        monkeypatch.setattr(convert_mrs, "REPO_API", "http://api.github.com/repos/X/Y/releases")
        with pytest.raises(ValueError, match="https"):
            convert_mrs._fetch_latest_release_info(max_retries=1)
        assert called == []


class TestHttpsRedirectHandler:
    def test_opener_uses_https_only_redirect_handler(self):
        assert any(
            isinstance(handler, convert_mrs._HttpsOnlyRedirectHandler) for handler in convert_mrs._HTTPS_OPENER.handlers
        )

    def test_http_redirect_target_is_rejected(self):
        handler = convert_mrs._HttpsOnlyRedirectHandler()
        req = convert_mrs.urllib.request.Request("https://example.com/a")
        with pytest.raises(ValueError, match="https"):
            handler.redirect_request(req, None, 302, "Found", {}, "http://example.com/b")

    def test_https_redirect_target_is_allowed(self):
        handler = convert_mrs._HttpsOnlyRedirectHandler()
        req = convert_mrs.urllib.request.Request("https://example.com/a")
        redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/b")
        assert redirected.full_url == "https://example.com/b"


class TestKernelArchitecture:
    @pytest.mark.parametrize(
        "machine, expected",
        [("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"), ("ARM64", "arm64")],
    )
    def test_machine_names_are_normalized(self, machine, expected):
        assert convert_mrs.normalize_kernel_arch(machine) == expected

    def test_unsupported_machine_is_rejected(self):
        assert convert_mrs.normalize_kernel_arch("riscv64") is None

    def test_asset_name_encodes_architecture(self):
        assert convert_mrs.expected_kernel_asset_name("v1.19.32", "amd64") == "mihomo-linux-amd64-v1.19.32.gz"
        assert convert_mrs.expected_kernel_asset_name("v1.19.32", "arm64") == "mihomo-linux-arm64-v1.19.32.gz"

    def test_arm64_asset_is_selected(self):
        assets = [
            asset("mihomo-linux-amd64-v1.19.32.gz"),
            asset("mihomo-linux-arm64-v1.19.32.gz"),
        ]
        got = convert_mrs.select_kernel_asset(assets, "v1.19.32", "arm64")
        assert got["name"] == "mihomo-linux-arm64-v1.19.32.gz"

    def test_linux_arm64_is_supported(self, monkeypatch):
        monkeypatch.setattr(convert_mrs.sys, "platform", "linux")
        monkeypatch.setattr(convert_mrs.platform, "machine", lambda: "aarch64")
        convert_mrs.ensure_kernel_platform()

    def test_unsupported_linux_architecture_fails_before_download(self, monkeypatch):
        monkeypatch.setattr(convert_mrs.sys, "platform", "linux")
        monkeypatch.setattr(convert_mrs.platform, "machine", lambda: "riscv64")
        with pytest.raises(SystemExit) as exc:
            convert_mrs.ensure_kernel_platform()
        assert exc.value.code == 1

    def test_kernel_hash_key_keeps_amd64_compatibility(self):
        assert convert_mrs.kernel_sha_key("amd64") == "kernel_sha256"
        assert convert_mrs.kernel_sha_key("arm64") == "kernel_sha256_arm64"

    def test_verify_current_kernel_runs_version_and_smoke_checks(self, monkeypatch, work_dir):
        calls = []
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "ensure_config_usable", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo", lambda: calls.append("load"))
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a, **k: calls.append("version") or "Mihomo")
        monkeypatch.setattr(convert_mrs, "_smoke_convert", lambda *a, **k: calls.append("smoke"))
        monkeypatch.setattr(convert_mrs.sys, "argv", ["convert_mrs.py", "--verify-current-kernel"])

        convert_mrs.main()

        assert calls == ["load", "version", "smoke"]


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
        assets = [{"name": "mihomo-linux-amd64-v1.19.30.gz", "browser_download_url": "https://example.com/x.gz"}]
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

    def _run(
        self,
        work_dir,
        expected_digest,
        payload=None,
        *,
        request_url="https://example.com/k.gz",
        response_url=None,
        existing=None,
        on_read=None,
    ):
        import io as _io

        payload = payload if payload is not None else self._payload()
        kernel_dir = work_dir / "k"
        kernel_dir.mkdir(parents=True, exist_ok=True)
        target = kernel_dir / "kernel"
        if existing is not None:
            target.write_bytes(existing)

        class FakeResp(_io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *args):
                if on_read is not None:
                    on_read()
                return super().read(*args)

        if response_url is not None:
            FakeResp.url = response_url

        original_open = convert_mrs._open_url
        original_bin = convert_mrs.KERNEL_BIN
        convert_mrs._open_url = lambda req, timeout=None: FakeResp(payload)
        convert_mrs.KERNEL_BIN = str(target)
        try:
            convert_mrs._download_kernel(request_url, expected_digest=expected_digest, max_retries=1)
            return True, target.read_bytes() if target.exists() else None
        except Exception as e:
            return False, e
        finally:
            convert_mrs._open_url = original_open
            convert_mrs.KERNEL_BIN = original_bin

    def test_correct_digest_accepted(self, work_dir):
        import hashlib

        payload = self._payload()
        ok, data = self._run(work_dir, "sha256:" + hashlib.sha256(payload).hexdigest())
        assert ok is True
        assert data == b"ELF-BODY-CONTENT"

    def test_http_response_url_is_rejected(self, work_dir):
        ok, err = self._run(
            work_dir,
            "sha256:" + "0" * 64,
            response_url="http://mirror.example.com/k.gz",
        )
        assert ok is False
        assert "https" in str(err).lower()

    def test_http_request_url_is_rejected_before_fetch(self, work_dir):
        called = []
        original_open = convert_mrs._open_url
        convert_mrs._open_url = lambda *a, **k: called.append(True)
        try:
            with pytest.raises(ValueError, match="https"):
                convert_mrs._download_kernel(
                    "http://example.com/k.gz",
                    expected_digest="sha256:" + "0" * 64,
                    max_retries=1,
                )
        finally:
            convert_mrs._open_url = original_open
        assert called == []

    def test_wrong_digest_rejected_and_not_written(self, work_dir):
        ok, err = self._run(work_dir, "sha256:" + "0" * 64)
        assert ok is False
        assert "摘要不匹配" in str(err)
        kernel = work_dir / "k" / "kernel"
        assert not kernel.exists(), f"校验失败不得落盘，但 {kernel} 存在"

    def test_failure_preserves_concurrently_replaced_target(self, work_dir):
        kernel = work_dir / "k" / "kernel"
        concurrent = b"valid-kernel-from-another-process"
        ok, err = self._run(
            work_dir,
            "sha256:" + "0" * 64,
            existing=b"old-kernel",
            on_read=lambda: kernel.write_bytes(concurrent),
        )
        assert ok is False
        assert "摘要不匹配" in str(err)
        assert kernel.read_bytes() == concurrent
        assert list(kernel.parent.iterdir()) == [kernel]

    @pytest.mark.parametrize("variant", ("uppercase-algo", "no-prefix", "padded"))
    def test_digest_format_variants_rejected(self, work_dir, variant):
        import hashlib

        payload = self._payload()
        hexdigest = hashlib.sha256(payload).hexdigest()
        expected = {
            "uppercase-algo": "SHA256:" + hexdigest,
            "no-prefix": hexdigest,
            "padded": "sha256:" + hexdigest + " ",
        }[variant]
        ok, err = self._run(work_dir, expected)
        assert ok is False
        assert "digest" in str(err)

    def test_other_algorithm_fails_closed(self, work_dir):
        ok, _err = self._run(work_dir, "sha512:" + "0" * 128)
        assert ok is False, "未知算法必须响亮失败，不得静默通过"

    def test_empty_digest_is_refused(self, work_dir):
        ok, err = self._run(work_dir, "")
        assert ok is False, "上游未提供 digest 时必须拒绝下载，不得降级为仅校验解压后哈希"
        assert "digest" in str(err)

    def test_compressed_stream_size_capped(self, work_dir):
        import hashlib
        import os as _os

        payload = self._payload(_os.urandom(4096))
        original = convert_mrs.MAX_KERNEL_BYTES
        convert_mrs.MAX_KERNEL_BYTES = 64
        try:
            ok, err = self._run(work_dir, "sha256:" + hashlib.sha256(payload).hexdigest(), payload=payload)
        finally:
            convert_mrs.MAX_KERNEL_BYTES = original
        assert ok is False
        assert "压缩包超过上限" in str(err)


class TestNetworkTotalDeadline:
    def test_release_read_is_bounded_by_total_deadline(self, monkeypatch):
        clock = [0.0]

        class SlowResp:
            url = API_LATEST

            def __init__(self):
                self.sent = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _size=-1):
                if self.sent:
                    return b""
                self.sent = True
                clock[0] += 1000
                return b"{}"

        monkeypatch.setattr(convert_mrs.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(convert_mrs, "_open_url", lambda req, timeout=None: SlowResp())
        with pytest.raises(TimeoutError, match="总时限"):
            convert_mrs._fetch_latest_release_info(max_retries=1)

    def test_each_network_read_uses_remaining_socket_timeout(self, monkeypatch):
        clock = [0.0]

        class Socket:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        class Raw:
            def __init__(self, sock):
                self._sock = sock

        class Fp:
            def __init__(self, sock):
                self.raw = Raw(sock)

        class SlowResp:
            url = API_LATEST

            def __init__(self):
                self.sock = Socket()
                self.fp = Fp(self.sock)
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _size=-1):
                self.reads += 1
                clock[0] += 40 if self.reads == 1 else 1000
                return b"{}" if self.reads == 1 else b""

        response = SlowResp()
        monkeypatch.setattr(convert_mrs.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(convert_mrs, "_open_url", lambda req, timeout=None: response)
        with pytest.raises(TimeoutError, match="总时限"):
            convert_mrs._fetch_latest_release_info(max_retries=1)
        assert response.sock.timeouts == [90, 50]

    def test_download_read_is_bounded_and_preserves_existing_target(self, monkeypatch, work_dir):
        clock = [0.0]
        target = work_dir / "kernel"
        target.write_bytes(b"old-kernel")

        class SlowResp:
            url = "https://example.com/k.gz"

            def __init__(self):
                self.sent = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _size=-1):
                if self.sent:
                    return b""
                self.sent = True
                clock[0] += 1000
                return b"slow-chunk"

        monkeypatch.setattr(convert_mrs.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(convert_mrs, "_open_url", lambda req, timeout=None: SlowResp())
        with pytest.raises(TimeoutError, match="总时限"):
            convert_mrs._download_kernel(
                "https://example.com/k.gz",
                expected_digest="sha256:" + "0" * 64,
                max_retries=1,
                output_path=target,
            )
        assert target.read_bytes() == b"old-kernel"
        assert list(work_dir.iterdir()) == [target]


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


class TestVerifyKernelArchitecture:
    def test_expected_arch_mismatch_rejected(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(elf_binary(0xB7))
        with pytest.raises(ValueError, match="架构"):
            convert_mrs.verify_kernel_file(str(target), "", expected_arch="amd64", require_sha=False)

    def test_expected_arch_match_accepted(self, work_dir):
        target = work_dir / "kernel"
        target.write_bytes(elf_binary(0xB7))
        actual = convert_mrs.verify_kernel_file(str(target), "", expected_arch="arm64", require_sha=False)
        assert len(actual) == 64


class TestSetConfigField:
    def test_replaces_single_occurrence(self):
        text = 'mihomo:\n  pinned_version: "v1.19.30"\n'
        out = convert_mrs._set_config_field(text, "pinned_version", "v1.19.32")
        assert '"v1.19.32"' in out
        assert "v1.19.30" not in out

    def test_accepts_unquoted_values(self):
        text = "mihomo:\n  pinned_version: v1.0\n  kernel_sha256: 'aa'\n"
        assert 'pinned_version: "v2.0"' in convert_mrs._set_config_field(text, "pinned_version", "v2.0")
        assert 'kernel_sha256: "bb"' in convert_mrs._set_config_field(text, "kernel_sha256", "bb")

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

    def test_policy_segment_with_ip_substring_does_not_leak(self):
        assert convert_mrs.get_rule_type(["skip", "domain", "Owner", "ads.txt"]) == "domain"
        assert convert_mrs.get_rule_type(["skip", "ipcidr", "Owner", "cn.txt"]) == "ipcidr"


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
    def run_with(self, work_dir, tag, assets, config_text):
        cfg = work_dir / "config.yaml"
        cfg.write_text(config_text, encoding="utf-8")
        original_fetch = convert_mrs._fetch_latest_release_info
        original_cwd = os.getcwd()
        try:
            convert_mrs._fetch_latest_release_info = lambda max_retries=3, pinned=None: {
                "tag_name": tag,
                "assets": assets,
            }
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
        assets = [{"name": "mihomo-linux-amd64-v9.9.9.gz", "browser_download_url": "https://example.com/k.gz"}]
        try:
            self.run_with(work_dir, "v9.9.9", assets, 'mihomo:\n  pinned_version: "v1.0.0"\n')
        except SystemExit:
            return
        raise AssertionError("缺少资产 digest 应退出")


class TestBumpConfigExactAsset:
    CONFIG = (
        "mihomo:\n"
        '  pinned_version: "v1.0.0"\n'
        '  kernel_sha256: "' + "a" * 64 + '"\n'
        '  kernel_sha256_arm64: "' + "b" * 64 + '"\n'
    )

    def _bump(self, monkeypatch, work_dir, tag, assets):
        (work_dir / "config.yaml").write_text(self.CONFIG, encoding="utf-8")
        monkeypatch.setattr(
            convert_mrs,
            "_fetch_latest_release_info",
            lambda max_retries=3, pinned=None: {"tag_name": tag, "assets": assets},
        )
        monkeypatch.chdir(work_dir)
        convert_mrs.bump_config()

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

        def fake_select(assets, tag_name, arch="amd64"):
            seen.setdefault("calls", []).append((tag_name, arch))
            return None

        monkeypatch.setattr(convert_mrs, "select_kernel_asset", fake_select)
        with pytest.raises(SystemExit):
            self._bump(monkeypatch, work_dir, "v9.9.9", [asset("mihomo-linux-amd64-v9.9.9.gz")])
        assert seen == {"calls": [("v9.9.9", "amd64")]}

    def test_dual_bump_requires_arm64_asset(self, monkeypatch, work_dir):
        assets = [asset("mihomo-linux-amd64-v9.9.9.gz")]
        downloads = []
        monkeypatch.setattr(convert_mrs, "_download_kernel", lambda *a, **k: downloads.append(a))
        with pytest.raises(SystemExit) as exc:
            self._bump(monkeypatch, work_dir, "v9.9.9", assets)
        assert exc.value.code == 1
        assert downloads == []


class TestBumpConfigTagValidation:
    def test_unsafe_tag_is_rejected(self, monkeypatch, work_dir):
        monkeypatch.chdir(work_dir)
        monkeypatch.setattr(
            convert_mrs,
            "_fetch_latest_release_info",
            lambda *a, **k: {"tag_name": "bad\n::error::x", "assets": []},
        )
        monkeypatch.setattr(convert_mrs, "error", lambda *a, **k: None)
        monkeypatch.setattr(convert_mrs, "gh_error", lambda *a, **k: None)
        with pytest.raises(SystemExit) as exc:
            convert_mrs.bump_config()
        assert exc.value.code == 1


class TestNoTokenInjection:
    def capture_requests(self, monkeypatch):
        requests = []

        def stop_after_capture(request, timeout=None):
            requests.append(request)
            raise RuntimeError("capture-only")

        monkeypatch.setattr(convert_mrs, "_open_url", stop_after_capture)
        monkeypatch.setattr(convert_mrs.time, "sleep", lambda _seconds: None)
        return requests

    def assert_unauthenticated(self, requests):
        assert requests
        for request in requests:
            headers = {key.lower() for key, _value in request.header_items()}
            assert "authorization" not in headers
            assert "proxy-authorization" not in headers

    def test_release_request_has_no_auth_headers(self, monkeypatch):
        requests = self.capture_requests(monkeypatch)
        with pytest.raises(RuntimeError, match="capture-only"):
            convert_mrs._fetch_latest_release_info(max_retries=1)
        self.assert_unauthenticated(requests)

    def test_download_request_has_no_auth_headers(self, monkeypatch):
        requests = self.capture_requests(monkeypatch)
        with pytest.raises(RuntimeError, match="capture-only"):
            convert_mrs._download_kernel(
                "https://example.com/kernel.gz", expected_digest="sha256:" + "0" * 64, max_retries=1
            )
        self.assert_unauthenticated(requests)


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
    def test_bump_actually_requests_the_latest_url(self, monkeypatch, work_dir):
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            raise RuntimeError("stop-after-recording")

        monkeypatch.setattr(convert_mrs, "_open_url", fake_urlopen)
        monkeypatch.setattr(convert_mrs.time, "sleep", lambda _s: None)
        monkeypatch.chdir(work_dir)
        (work_dir / "config.yaml").write_text(
            'mihomo:\n  pinned_version: "v1.19.30"\n  kernel_sha256: "00"\n', encoding="utf-8"
        )
        with pytest.raises(RuntimeError):
            convert_mrs.bump_config()
        assert seen, "bump 路径没有发出任何请求"
        assert seen[0].endswith("/releases/latest"), seen[0]
        assert "/tags/" not in seen[0]


class TestGetLatestMihomo:
    def setup_kernel(self, monkeypatch, work_dir, *, cached_version=None):
        cache = work_dir / "kernel"
        bin_path = cache / "amd64" / "mihomo"
        version_file = cache / "amd64" / "version.txt"
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", cache)
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(bin_path))
        monkeypatch.setattr(convert_mrs, "VERSION_FILE", version_file)
        monkeypatch.setattr(convert_mrs, "EXPECTED_SHA", "a" * 64)
        monkeypatch.setattr(convert_mrs, "PINNED_VERSION", "v1.2.3")
        monkeypatch.setattr(convert_mrs, "current_kernel_arch", lambda: "amd64")
        for name in ("info", "warning", "error", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        if cached_version is not None:
            version_file.write_text(cached_version, encoding="utf-8")
            bin_path.write_bytes(b"\x7fELF")
        return bin_path, version_file

    def test_cache_paths_are_isolated_by_architecture(self, monkeypatch, work_dir):
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", work_dir / "kernel")
        amd64_bin, amd64_version = convert_mrs._kernel_cache_paths("amd64")
        arm64_bin, arm64_version = convert_mrs._kernel_cache_paths("arm64")
        assert amd64_bin == work_dir / "kernel" / "amd64" / "mihomo"
        assert arm64_bin == work_dir / "kernel" / "arm64" / "mihomo"
        assert amd64_version == work_dir / "kernel" / "amd64" / "version.txt"
        assert arm64_version == work_dir / "kernel" / "arm64" / "version.txt"

    def test_legacy_cache_is_migrated_and_reused_without_download(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir)
        cache = work_dir / "kernel"
        legacy_bin = cache / "mihomo"
        legacy_version = cache / "version.txt"
        payload = elf_binary(0x3E)
        legacy_bin.write_bytes(payload)
        legacy_bin.chmod(0o755)
        legacy_version.write_text("v1.2.3", encoding="utf-8")
        monkeypatch.setattr(convert_mrs, "EXPECTED_SHA", hashlib.sha256(payload).hexdigest())
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")

        def unexpected_fetch(*_a, **_k):
            raise AssertionError("有效旧缓存迁移后不应访问网络")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", unexpected_fetch)
        convert_mrs.get_latest_mihomo()

        assert bin_path.read_bytes() == payload
        assert version_file.read_text(encoding="utf-8") == "v1.2.3"
        assert legacy_bin.read_bytes() == payload
        assert bin_path.stat().st_mode == legacy_bin.stat().st_mode

    def release(self, tag="v1.2.3"):
        return {"tag_name": tag, "assets": [asset("mihomo-linux-amd64-v1.2.3.gz")]}

    def downloaded(self, monkeypatch, bin_path):
        monkeypatch.setattr(
            convert_mrs,
            "select_kernel_asset",
            lambda assets, tag_name, arch="amd64": {
                "name": "mihomo-linux-amd64-v1.2.3.gz",
                "url": "https://example.com/kernel.gz",
                "digest": "sha256:" + "b" * 64,
            },
        )
        monkeypatch.setattr(
            convert_mrs,
            "_download_kernel",
            lambda url, expected_digest=None, output_path=None: Path(output_path or bin_path).write_bytes(b"\x7fELF"),
        )

    def test_cache_version_change_during_validation_is_not_executed(self, monkeypatch, work_dir):
        _bin_path, version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")

        def change_version(*_a, **_k):
            version_file.write_text("v9.9.9", encoding="utf-8")
            return "a" * 64

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", change_version)
        executions = []
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda: executions.append(True) or "Mihomo")
        assert convert_mrs._cached_kernel_usable("v1.2.3", "a" * 64, "amd64") is None
        assert executions == []

    def test_cache_hit_reuses_kernel_without_download(self, monkeypatch, work_dir):
        _bin_path, _version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")
        fetch_calls = []
        monkeypatch.setattr(
            convert_mrs, "_fetch_latest_release_info", lambda *a, **k: fetch_calls.append(True) or self.release()
        )
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")
        downloads = []
        monkeypatch.setattr(convert_mrs, "_download_kernel", lambda *a, **k: downloads.append(True))

        convert_mrs.get_latest_mihomo()

        assert downloads == []
        assert fetch_calls == []

    def test_print_hash_mode_preserves_cached_kernel(self, monkeypatch, work_dir):
        bin_path, _version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        verifications = []

        def fake_verify(path, expected_sha, require_sha=False, **_kwargs):
            verifications.append((expected_sha, require_sha))
            return "a" * 64

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")
        downloads = []
        monkeypatch.setattr(convert_mrs, "_download_kernel", lambda *a, **k: downloads.append(True))

        convert_mrs.get_latest_mihomo(skip_hash_check=True)

        assert verifications == [("", False)]
        assert downloads == []
        assert bin_path.exists()

    def test_broken_cache_is_replaced_by_download(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)
        verifications = []

        def fake_verify(path, expected_sha, require_sha=False, **_kwargs):
            verifications.append(require_sha)
            if len(verifications) == 1:
                raise ValueError("缓存损坏")
            return "a" * 64

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

        assert verifications == [True, True]
        assert version_file.read_text(encoding="utf-8") == "v1.2.3"

    def test_fresh_download_installs_kernel(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

        assert version_file.read_text(encoding="utf-8") == "v1.2.3"

    def test_post_download_sha_mismatch_exits(self, monkeypatch, work_dir):
        bin_path, _version_file = self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)

        def fake_verify(*_a, **_k):
            raise ValueError("内核哈希不匹配")

        monkeypatch.setattr(convert_mrs, "verify_kernel_file", fake_verify)

        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1
        assert not bin_path.exists()

    def test_failed_candidate_cannot_become_fallback_cache(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir)
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        self.downloaded(monkeypatch, bin_path)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        calls = []

        def fake_verify_kernel(kernel_bin=None):
            calls.append(Path(kernel_bin or convert_mrs.KERNEL_BIN))
            return "unknown" if len(calls) == 1 else "Mihomo cached"

        monkeypatch.setattr(convert_mrs, "_verify_kernel", fake_verify_kernel)
        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1
        assert len(calls) == 1
        assert calls[0] != bin_path
        assert not bin_path.exists()
        assert not version_file.exists()

    def test_failed_candidate_preserves_previous_cache_for_fallback(self, monkeypatch, work_dir):
        bin_path, version_file = self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.2")
        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", lambda *a, **k: self.release())
        monkeypatch.setattr(
            convert_mrs,
            "_download_kernel",
            lambda url, expected_digest=None, output_path=None: Path(output_path or bin_path).write_bytes(
                b"new-candidate"
            ),
        )
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        calls = []

        def fake_verify_kernel(kernel_bin=None):
            calls.append(Path(kernel_bin or convert_mrs.KERNEL_BIN))
            return "unknown" if len(calls) == 1 else "Mihomo cached"

        monkeypatch.setattr(convert_mrs, "_verify_kernel", fake_verify_kernel)
        convert_mrs.get_latest_mihomo()

        assert len(calls) == 2
        assert calls[0] != bin_path
        assert calls[1] == bin_path
        assert bin_path.read_bytes() == b"\x7fELF"
        assert version_file.read_text(encoding="utf-8") == "v1.2.2"

    def test_fetch_failure_degrades_to_verified_cache(self, monkeypatch, work_dir):
        self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")

        def fake_fetch(*_a, **_k):
            raise RuntimeError("网络不可用")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", fake_fetch)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a: "Mihomo Meta v1.2.3")

        convert_mrs.get_latest_mihomo()

    def test_fallback_rejects_version_output_without_mihomo(self, monkeypatch, work_dir):
        self.setup_kernel(monkeypatch, work_dir, cached_version="v1.2.3")

        def fake_fetch(*_a, **_k):
            raise RuntimeError("网络不可用")

        monkeypatch.setattr(convert_mrs, "_fetch_latest_release_info", fake_fetch)
        monkeypatch.setattr(convert_mrs, "verify_kernel_file", lambda *a, **k: "a" * 64)
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a, **k: "unknown")
        with pytest.raises(SystemExit) as exc:
            convert_mrs.get_latest_mihomo()

        assert exc.value.code == 1

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
        monkeypatch.setattr(
            convert_mrs, "_fetch_latest_release_info", lambda *a, **k: {"tag_name": "v1.2.3", "assets": []}
        )
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
        monkeypatch.setattr(
            convert_mrs, "get_latest_mihomo", lambda skip_hash_check=False: seen.append(skip_hash_check)
        )
        monkeypatch.setattr(convert_mrs, "file_sha256", lambda path: "f" * 64)
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", "kernel")
        monkeypatch.setattr(sys, "argv", ["convert_mrs.py", "--print-kernel-hash"])

        convert_mrs.main()

        assert seen == [True]
        assert "f" * 64 in capsys.readouterr().out


class TestConvertOutput:
    def run_conversion(self, monkeypatch, work_dir, output):
        src = work_dir / "src"
        dst = work_dir / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "rules.txt").write_text("example.com\n", encoding="utf-8")
        monkeypatch.setattr(convert_mrs, "SRC_ROOT", str(src))
        monkeypatch.setattr(convert_mrs, "DST_ROOT", str(dst))
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "ensure_config_usable", lambda: None)
        monkeypatch.setattr(convert_mrs, "ensure_products_anchored", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_rule_type", lambda _parts: "domain")
        monkeypatch.setattr(convert_mrs, "write_summary", lambda *a, **k: None)
        monkeypatch.setattr(convert_mrs.subprocess, "run", lambda cmd, **k: Path(cmd[-1]).write_bytes(output))
        monkeypatch.setattr(sys, "argv", ["convert_mrs.py"])
        for name in ("info", "warning", "error", "success", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)

    def test_zero_exit_with_empty_output_is_failure(self, monkeypatch, work_dir):
        self.run_conversion(monkeypatch, work_dir, b"")
        with pytest.raises(SystemExit) as exc:
            convert_mrs.main()
        assert exc.value.code == 1

    def test_zero_exit_with_nonempty_file_succeeds(self, monkeypatch, work_dir):
        self.run_conversion(monkeypatch, work_dir, b"valid-mrs")
        with pytest.raises(SystemExit) as exc:
            convert_mrs.main()
        assert exc.value.code == 0

    def test_stale_output_is_not_credit_for_current_conversion(self, monkeypatch, work_dir):
        self.run_conversion(monkeypatch, work_dir, b"ignored")
        stale = work_dir / "dst" / "rules.mrs"
        stale.write_bytes(b"stale-mrs")
        monkeypatch.setattr(convert_mrs, "clean_directory", lambda _path: [(str(stale), "locked")])
        monkeypatch.setattr(convert_mrs.subprocess, "run", lambda *_a, **_k: None)

        with pytest.raises(SystemExit) as exc:
            convert_mrs.main()

        assert exc.value.code == 1
        assert stale.read_bytes() == b"stale-mrs"


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

    def test_stale_output_does_not_pass_smoke(self, monkeypatch, work_dir):
        self.prepare(monkeypatch, work_dir)
        (work_dir / "smoke-output.mrs").write_bytes(b"stale")
        monkeypatch.setattr(convert_mrs.subprocess, "run", lambda cmd, **k: None)

        with pytest.raises(SystemExit) as exc:
            convert_mrs._smoke_convert()

        assert exc.value.code == 1


class TestBumpConfigSuccess:
    def test_full_bump_writes_pin_and_outputs(self, monkeypatch, work_dir):
        cfg = work_dir / "config.yaml"
        cfg.write_text(
            'pinned_version: "v1.0.0"\nkernel_sha256: "old"\nkernel_sha256_arm64: "old-arm"\n',
            encoding="utf-8",
        )
        gh_output = work_dir / "gh_output.txt"
        monkeypatch.chdir(work_dir)
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))
        monkeypatch.setattr(convert_mrs, "KERNEL_CACHE_DIR", work_dir / "cache")
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(work_dir / "cache" / "mihomo"))
        monkeypatch.setattr(
            convert_mrs, "_fetch_latest_release_info", lambda *a, **k: {"tag_name": "v9.9.9", "assets": []}
        )

        def fake_select(_assets, _tag_name, arch="amd64"):
            return {
                "name": f"mihomo-linux-{arch}-v9.9.9.gz",
                "url": f"https://example.com/{arch}-kernel.gz",
                "digest": "sha256:" + "c" * 64,
            }

        monkeypatch.setattr(convert_mrs, "select_kernel_asset", fake_select)
        downloads = []
        monkeypatch.setattr(
            convert_mrs,
            "_download_kernel",
            lambda url, expected_digest=None, output_path=None: downloads.append(
                (url, expected_digest, str(output_path))
            ),
        )
        verifications = []
        monkeypatch.setattr(
            convert_mrs,
            "verify_kernel_file",
            lambda path, expected_sha, *a, expected_arch=None, **k: (
                verifications.append((str(path), expected_arch)) or "d" * 64
            ),
        )
        hashes = {
            str(work_dir / "cache" / "mihomo-amd64"): "e" * 64,
            str(work_dir / "cache" / "mihomo-arm64"): "f" * 64,
        }
        monkeypatch.setattr(convert_mrs, "file_sha256", lambda path: hashes[str(path)])
        monkeypatch.setattr(convert_mrs, "_verify_kernel", lambda *a, **k: "Mihomo Meta v9.9.9")
        monkeypatch.setattr(convert_mrs, "_smoke_convert", lambda *a, **k: None)
        monkeypatch.setattr(convert_mrs, "current_kernel_arch", lambda: "amd64")
        for name in ("info", "warning", "error", "group_start", "group_end"):
            monkeypatch.setattr(convert_mrs, name, lambda *a, **k: None)

        convert_mrs.bump_config()

        text = cfg.read_text(encoding="utf-8")
        assert 'pinned_version: "v9.9.9"' in text
        assert f'kernel_sha256: "{"e" * 64}"' in text
        assert f'kernel_sha256_arm64: "{"f" * 64}"' in text
        assert gh_output.read_text(encoding="utf-8") == "changed=true\ntag=v9.9.9\n"
        assert downloads == [
            ("https://example.com/amd64-kernel.gz", "sha256:" + "c" * 64, str(work_dir / "cache" / "mihomo-amd64")),
            ("https://example.com/arm64-kernel.gz", "sha256:" + "c" * 64, str(work_dir / "cache" / "mihomo-arm64")),
        ]
        assert verifications == [
            (str(work_dir / "cache" / "mihomo-amd64"), "amd64"),
            (str(work_dir / "cache" / "mihomo-arm64"), "arm64"),
        ]
