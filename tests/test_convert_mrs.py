import os

import convert_mrs

API_LATEST = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"


def asset(name):
    return {"name": name, "browser_download_url": f"https://example.com/{name}"}


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
        url = convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.30.gz", "v1.19.30")
        assert url == "https://example.com/mihomo-linux-amd64-v1.19.30.gz"

    def test_exact_asset_name_missing_returns_none(self):
        assets = [asset("mihomo-linux-amd64-v1.19.30.gz")]
        assert convert_mrs.select_kernel_asset(assets, "mihomo-linux-amd64-v1.19.31.gz", "v1.19.31") is None

    def test_prefers_exact_name_over_variants(self):
        assets = [
            asset("mihomo-linux-amd64-go1.24-v1.19.32.gz"),
            asset("mihomo-linux-amd64-compatible-v1.19.32.gz"),
            asset("mihomo-linux-amd64-v1.19.32.gz"),
        ]
        url = convert_mrs.select_kernel_asset(assets, "", "v1.19.32")
        assert url == "https://example.com/mihomo-linux-amd64-v1.19.32.gz"

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
        url = convert_mrs.select_kernel_asset(assets, "", "v1.19.32")
        assert url.endswith("mihomo-linux-amd64-v1.19.32.gz")

    def test_without_pinned_version_picks_sorted_first(self):
        assets = [
            asset("mihomo-linux-amd64-v1.19.31.gz"),
            asset("mihomo-linux-amd64-v1.19.30.gz"),
        ]
        url = convert_mrs.select_kernel_asset(assets, "", "")
        assert url.endswith("v1.19.30.gz")


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
