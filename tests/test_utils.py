import hashlib

import utils


class TestDedupDomainSuffix:
    def test_child_covered_by_parent(self):
        kept, removed = utils.dedup_domain_suffix({"google.com", "ads.google.com", "youtube.com"})
        assert kept == ["google.com", "youtube.com"]
        assert removed == 1

    def test_exact_parent_not_removed_by_itself(self):
        kept, removed = utils.dedup_domain_suffix({"google.com", "google.com"})
        assert kept == ["google.com"]
        assert removed == 0

    def test_empty(self):
        assert utils.dedup_domain_suffix(set()) == ([], 0)

    def test_sorted_output(self):
        kept, _ = utils.dedup_domain_suffix({"b.com", "a.com"})
        assert kept == ["a.com", "b.com"]


class TestFlattenIpCidr:
    def test_collapse_and_order(self):
        result, errors = utils.flatten_ip_cidr(["1.0.0.0/24", "1.0.1.0/24", "2001:db8::/32"])
        assert result == ["1.0.0.0/23", "2001:db8::/32"]
        assert errors == []

    def test_invalid_collected_as_errors(self):
        result, errors = utils.flatten_ip_cidr(["bad-cidr", "10.0.0.0/8"])
        assert result == ["10.0.0.0/8"]
        assert len(errors) == 1
        assert errors[0][0] == "bad-cidr"

    def test_blank_lines_skipped(self):
        result, errors = utils.flatten_ip_cidr(["", "  ", "10.0.0.0/8"])
        assert errors == []
        assert result == ["10.0.0.0/8"]


class TestNormalize:
    def test_policy(self):
        assert utils.normalize_policy("REJECT") == "block"
        assert utils.normalize_policy("adblock") == "block"
        assert utils.normalize_policy("direct") == "direct"
        assert utils.normalize_policy("no-proxy") == "direct"
        assert utils.normalize_policy("gfw") == "policy"
        assert utils.normalize_policy("proxy") == "policy"
        assert utils.normalize_policy("") == "proxy"
        assert utils.normalize_policy("custom") == "custom"

    def test_type(self):
        assert utils.normalize_type("IP") == "ipcidr"
        assert utils.normalize_type("cidr") == "ipcidr"
        assert utils.normalize_type("ipcIdr") == "ipcidr"
        assert utils.normalize_type("domain") == "domain"

    def test_owner_from_url(self):
        assert utils.get_owner_from_url("https://github.com/OWNER/repo/raw/x/f.txt") == "OWNER"
        assert utils.get_owner_from_url("https://raw.githubusercontent.com/OWNER/repo/branch/f.txt") == "OWNER"
        assert utils.get_owner_from_url("https://cdn.jsdelivr.net/gh/OWNER/repo@ver/f.txt") == "OWNER"
        assert utils.get_owner_from_url("https://example.com/a/b") == "example.com"
        assert utils.get_owner_from_url("https://github.com") == "github"


class TestAtomicWrite:
    def test_list_joined_with_trailing_newline(self, tmp_path):
        target = tmp_path / "sub" / "out.txt"
        utils.atomic_write(str(target), ["a", "b"])
        assert target.read_text(encoding="utf-8") == "a\nb\n"

    def test_str_gets_trailing_newline(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "content")
        assert target.read_text(encoding="utf-8") == "content\n"

    def test_overwrite_no_tmp_left(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "v1")
        utils.atomic_write(str(target), "v2")
        assert target.read_text(encoding="utf-8") == "v2\n"
        assert list(tmp_path.glob("*.tmp")) == []


class TestDirHash:
    def test_empty_dir(self, tmp_path):
        assert utils.dir_hash(str(tmp_path)) == ("", 0)

    def test_missing_dir(self, tmp_path):
        assert utils.dir_hash(str(tmp_path / "nope")) == ("", 0)

    def test_skip_comments_ignores_metadata(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("# Date: 2024-01-01\nrule-one.com\n", encoding="utf-8")
        b.write_text("# Date: 2025-12-31\nrule-one.com\n", encoding="utf-8")
        ha, _ = utils.dir_hash(str(tmp_path), "*.txt", skip_comments=True)
        assert ha != ""
        assert utils.dir_hash(str(tmp_path), "*.txt", skip_comments=True)[0] == ha

    def test_full_hash_differs_when_comments_differ(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("# Date: 2024-01-01\nrule-one.com\n", encoding="utf-8")
        b.write_text("# Date: 2025-12-31\nrule-one.com\n", encoding="utf-8")
        h1, c1 = utils.dir_hash(str(tmp_path), "*.txt")
        assert c1 == 2
        assert h1 == hashlib.sha256(
            (utils.file_sha256(str(a)) + utils.file_sha256(str(b))).encode()
        ).hexdigest()


class TestCleanDirectory:
    def test_removes_contents_keeps_root(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "g.txt").write_text("y", encoding="utf-8")
        utils.clean_directory(str(tmp_path))
        assert tmp_path.exists()
        assert list(tmp_path.iterdir()) == []

    def test_creates_missing_root(self, tmp_path):
        target = tmp_path / "newdir"
        utils.clean_directory(str(target))
        assert target.is_dir()
