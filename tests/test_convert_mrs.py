import os

import convert_mrs

API_LATEST = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"


def asset(name, digest="sha256:" + "0" * 64):
    return {
        "name": name,
        "browser_download_url": f"https://example.com/{name}",
        "digest": digest,
    }


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
    def test_exact_asset_name_match(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz")]
        got = convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.30.gz", "v1.19.30")
        assert got["url"] == "https://example.com/mihomo-linux-amd64-v1.19.30.gz"
        assert got["name"] == "mihomo-linux-amd64-v1.19.30.gz"
        assert got["digest"].startswith("sha256:")

    def test_digest_is_carried_through(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz", digest="sha256:deadbeef")]
        got = convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.30.gz", "v1.19.30")
        assert got["digest"] == "sha256:deadbeef"

    def test_missing_digest_becomes_empty_string(self):
        assets = [{"name": "mihomo-linux-amd64-v1.19.30.gz",
                   "browser_download_url": "https://example.com/x.gz"}]
        got = convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.30.gz", "v1.19.30")
        assert got["digest"] == ""

    def test_exact_asset_name_missing_returns_none(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz")]
        assert convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.31.gz", "v1.19.31") is None

    def test_prefers_exact_name_over_variants(self):
        assets = [
            asset("mihomo-linux-amd64-go1.24-v1.19.32.gz"),
            asset("mihomo-linux-amd64-compatible-v1.19.32.gz"),
            asset("mihomo-linux-amd64-v1.19.32.gz"),
        ]
        got = convert_mrs.select_kernel_asset(assets, "", "v1.19.32")
        assert got["url"] == "https://example.com/mihomo-linux-amd64-v1.19.32.gz"

    def test_only_variants_returns_none(self):
        assets = [
            asset("mihomo-linux-amd64-go1.24-v1.19.32.gz"),
            asset("mihomo-linux-amd64-compatible-v1.19.32.gz"),
            asset("mihomo-linux-amd64-v3-v1.19.32.gz"),
        ]
        assert convert_mrs.select_kernel_asset(assets, "", "v1.19.32") is None

    def test_ignores_non_linux_amd64_and_non_gz(self):
        assets = [
            asset("mihomo-linux-arm64-v1.19.32.gz"),
            asset("mihomo-windows-amd64-v1.19.32.zip"),
            asset("mihomo-linux-amd64-v1.19.32.gz"),
        ]
        got = convert_mrs.select_kernel_asset(assets, "", "v1.19.32")
        assert got["url"].endswith("mihomo-linux-amd64-v1.19.32.gz")

    def test_without_pinned_version_picks_sorted_first(self):
        assets = [
            asset("mihomo-linux-amd64-v1.19.31.gz"),
            asset("mihomo-linux-amd64-v1.19.30.gz"),
        ]
        got = convert_mrs.select_kernel_asset(assets, "", "")
        assert got["url"].endswith("v1.19.30.gz")


class TestDownloadKernelDigest:

    def _payload(self, body=b"ELF-BODY-CONTENT"):
        import gzip
        import io

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(body)
        return buf.getvalue()

    def _run(self, tmp_path, expected_digest, payload=None):
        import io as _io
        import urllib.request as _ur

        payload = payload if payload is not None else self._payload()
        kernel_dir = tmp_path / "k"
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
                "https://example.com/k.gz", {}, expected_digest=expected_digest, max_retries=1
            )
            return True, target.read_bytes() if target.exists() else None
        except Exception as e:
            return False, e
        finally:
            _ur.urlopen = original_urlopen
            convert_mrs.KERNEL_BIN = original_bin

    def test_correct_digest_accepted(self, tmp_path):
        import hashlib

        payload = self._payload()
        ok, data = self._run(tmp_path, "sha256:" + hashlib.sha256(payload).hexdigest())
        assert ok is True
        assert data == b"ELF-BODY-CONTENT"

    def test_wrong_digest_rejected_and_not_written(self, tmp_path):
        ok, err = self._run(tmp_path, "sha256:" + "0" * 64)
        assert ok is False
        assert "摘要不匹配" in str(err)
        assert not (tmp_path / "kernel").exists(), "校验失败不得落盘"

    def test_uppercase_algorithm_accepted(self, tmp_path):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(tmp_path, "SHA256:" + hashlib.sha256(payload).hexdigest())
        assert ok is True

    def test_digest_without_algorithm_prefix_accepted(self, tmp_path):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(tmp_path, hashlib.sha256(payload).hexdigest())
        assert ok is True

    def test_trailing_whitespace_tolerated(self, tmp_path):
        import hashlib

        payload = self._payload()
        ok, _ = self._run(tmp_path, "sha256:" + hashlib.sha256(payload).hexdigest() + " ")
        assert ok is True, "digest 首尾空白应被 strip"

    def test_other_algorithm_fails_closed(self, tmp_path):
        ok, _err = self._run(tmp_path, "sha512:" + "0" * 128)
        assert ok is False, "未知算法必须响亮失败，不得静默通过"

    def test_empty_digest_skips_compressed_check(self, tmp_path):
        ok, _ = self._run(tmp_path, "")
        assert ok is True

    def test_compressed_stream_size_capped(self, tmp_path):
        import os as _os

        original = convert_mrs.MAX_KERNEL_BYTES
        convert_mrs.MAX_KERNEL_BYTES = 64
        try:
            ok, err = self._run(tmp_path, "", payload=self._payload(_os.urandom(4096)))
        finally:
            convert_mrs.MAX_KERNEL_BYTES = original
        assert ok is False
        assert "压缩包超过上限" in str(err)


class TestVerifyKernelRequireSha:
    def test_missing_sha_rejected_when_required(self, tmp_path):
        target = tmp_path / "kernel"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        try:
            convert_mrs.verify_kernel_file(str(target), "", require_sha=True)
        except ValueError as e:
            assert "缺少 kernel_sha256" in str(e)
            return
        raise AssertionError("require_sha=True 时缺少哈希应报错")

    def test_missing_sha_allowed_when_not_required(self, tmp_path):
        target = tmp_path / "kernel"
        target.write_bytes(b"\x7fELF" + b"\x00" * 32)
        actual = convert_mrs.verify_kernel_file(str(target), "", require_sha=False)
        assert len(actual) == 64

    def test_wrong_magic_rejected(self, tmp_path):
        target = tmp_path / "kernel"
        target.write_bytes(b"NOTELF" + b"\x00" * 32)
        try:
            convert_mrs.verify_kernel_file(str(target), "")
        except ValueError as e:
            assert "ELF" in str(e)
            return
        raise AssertionError("非 ELF 应报错")

    def test_sha_mismatch_rejected(self, tmp_path):
        target = tmp_path / "kernel"
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
    def test_only_comments_is_invalid(self, tmp_path):
        target = tmp_path / "only-comments.txt"
        target.write_text("# header\n\n# another\n", encoding="utf-8")
        assert convert_mrs.has_valid_content(str(target)) is False

    def test_rule_present_is_valid(self, tmp_path):
        target = tmp_path / "rules.txt"
        target.write_text("# header\nexample.com\n", encoding="utf-8")
        assert convert_mrs.has_valid_content(str(target)) is True

    def test_missing_file_is_invalid(self, tmp_path):
        assert convert_mrs.has_valid_content(str(tmp_path / "nope.txt")) is False


class TestBumpConfig:
    def run_with(self, tmp_path, tag, assets, config_text, fake_fetch=True):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(config_text, encoding="utf-8")
        original_fetch = convert_mrs._fetch_latest_release_info
        original_cwd = os.getcwd()
        try:
            if fake_fetch:
                convert_mrs._fetch_latest_release_info = (
                    lambda headers, max_retries=3: {"tag_name": tag, "assets": assets}
                )
            os.chdir(tmp_path)
            convert_mrs.bump_config()
        finally:
            os.chdir(original_cwd)
            convert_mrs._fetch_latest_release_info = original_fetch
        return cfg.read_text(encoding="utf-8")

    def test_noop_when_already_latest(self, tmp_path):
        text = 'mihomo:\n  pinned_version: "v9.9.9"\n'
        assert self.run_with(tmp_path, "v9.9.9", [], text) == text

    def test_missing_asset_exits(self, tmp_path):
        try:
            self.run_with(tmp_path, "v9.9.9", [], 'mihomo:\n  pinned_version: "v1.0.0"\n')
        except SystemExit:
            return
        raise AssertionError("无匹配资产应退出")

    def test_missing_digest_exits(self, tmp_path):
        assets = [{"name": "mihomo-linux-amd64-v9.9.9.gz",
                   "browser_download_url": "https://example.com/k.gz"}]
        try:
            self.run_with(tmp_path, "v9.9.9", assets, 'mihomo:\n  pinned_version: "v1.0.0"\n')
        except SystemExit:
            return
        raise AssertionError("缺少资产 digest 应退出")
