import hashlib
import os

import utils


class TestIsSafeComponent:
    def test_accepts_single_segment_names(self):
        for value in ("block", "rksk102", "all-adblock.txt", "a_b-c.d1"):
            assert utils.is_safe_component(value) is True

    def test_rejects_path_and_dot_tricks(self):
        for value in ("../x", "..", ".", "a/../b", "a\\b", "/abs", "C:/x", "",
                      "a b", "a..b", ".hidden", None, 5, ["a"]):
            assert utils.is_safe_component(value) is False, value


class TestAtomicWrite:
    def test_list_joined_with_trailing_newline(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), ["a.com", "b.com"])
        assert target.read_text(encoding="utf-8") == "a.com\nb.com\n"

    def test_string_without_newline_gets_one(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "a.com")
        assert target.read_text(encoding="utf-8") == "a.com\n"

    def test_existing_trailing_newline_not_duplicated(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "a.com\n")
        assert target.read_text(encoding="utf-8") == "a.com\n"

    def test_empty_string_becomes_single_newline(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "")
        assert target.read_text(encoding="utf-8") == "\n"

    def test_parent_dirs_created(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "out.txt"
        utils.atomic_write(str(target), "x")
        assert target.exists()

    def test_overwrites_existing_content(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old-and-long\n", encoding="utf-8")
        utils.atomic_write(str(target), "new")
        assert target.read_text(encoding="utf-8") == "new\n"

    def test_no_tmp_file_left_behind(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), "x")
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

    def test_failure_cleans_tmp_and_propagates(self, tmp_path):
        target = tmp_path / "out.txt"
        original = os.replace

        def boom(src, dst):
            raise OSError("disk full")

        os.replace = boom
        try:
            try:
                utils.atomic_write(str(target), "x")
            except OSError:
                pass
            else:
                raise AssertionError("应向上抛出 OSError")
        finally:
            os.replace = original
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
        assert not target.exists(), "失败时不得留下目标文件"

    def test_unicode_roundtrip(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write(str(target), ["+.中文域名.com", "广告.example.com"])
        assert "中文域名" in target.read_text(encoding="utf-8")


class TestAtomicWriteWithHeader:
    def test_header_lines_and_title_casing(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write_with_header(str(target), ["a.com"], {"sources": "u", "count": 1})
        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# " + "-" * 40
        assert lines[1] == "# Sources: u"
        assert lines[2] == "# Count: 1"
        assert lines[3] == "# " + "-" * 40
        assert lines[4] == "a.com"

    def test_empty_metadata_still_brackets(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write_with_header(str(target), ["a.com"], {})
        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[0] == lines[1] == "# " + "-" * 40

    def test_empty_rules_produces_only_header(self, tmp_path):
        target = tmp_path / "out.txt"
        utils.atomic_write_with_header(str(target), [], {"k": "v"})
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3


class TestFileSha256:
    def test_matches_hashlib(self, tmp_path):
        target = tmp_path / "f.bin"
        payload = b"hello world" * 1000
        target.write_bytes(payload)
        assert utils.file_sha256(str(target)) == hashlib.sha256(payload).hexdigest()

    def test_empty_file(self, tmp_path):
        target = tmp_path / "f.bin"
        target.write_bytes(b"")
        assert utils.file_sha256(str(target)) == hashlib.sha256(b"").hexdigest()

    def test_differs_on_content_change(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"x")
        b.write_bytes(b"y")
        assert utils.file_sha256(str(a)) != utils.file_sha256(str(b))


class TestHashFileBody:
    def test_comments_and_blanks_ignored(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("# header\na.com\n\nb.com\n", encoding="utf-8")
        b.write_text("a.com\nb.com\n", encoding="utf-8")
        assert utils._hash_file_body(str(a)) == utils._hash_file_body(str(b))

    def test_whitespace_stripped(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("  a.com  \n", encoding="utf-8")
        b.write_text("a.com\n", encoding="utf-8")
        assert utils._hash_file_body(str(a)) == utils._hash_file_body(str(b))

    def test_content_change_detected(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("a.com\n", encoding="utf-8")
        b.write_text("b.com\n", encoding="utf-8")
        assert utils._hash_file_body(str(a)) != utils._hash_file_body(str(b))

    def test_order_matters(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("a.com\nb.com\n", encoding="utf-8")
        b.write_text("b.com\na.com\n", encoding="utf-8")
        assert utils._hash_file_body(str(a)) != utils._hash_file_body(str(b))


class TestDirHash:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert utils.dir_hash(str(tmp_path / "nope")) == ("", 0)

    def test_empty_dir_returns_empty(self, tmp_path):
        assert utils.dir_hash(str(tmp_path)) == ("", 0)

    def test_counts_only_matching_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.mrs").write_text("b", encoding="utf-8")
        digest, count = utils.dir_hash(str(tmp_path), "*.txt")
        assert count == 1
        assert digest

    def test_hidden_files_skipped(self, tmp_path):
        (tmp_path / ".hidden.txt").write_text("h", encoding="utf-8")
        (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
        _digest, count = utils.dir_hash(str(tmp_path), "*.txt")
        assert count == 1

    def test_nested_dirs_included(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "b" / "x.txt").write_text("x", encoding="utf-8")
        _digest, count = utils.dir_hash(str(tmp_path), "*.txt")
        assert count == 1

    def test_stable_across_calls(self, tmp_path):
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        assert utils.dir_hash(str(tmp_path), "*.txt") == utils.dir_hash(str(tmp_path), "*.txt")

    def test_content_change_changes_digest(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("a", encoding="utf-8")
        first = utils.dir_hash(str(tmp_path), "*.txt")[0]
        f.write_text("b", encoding="utf-8")
        assert utils.dir_hash(str(tmp_path), "*.txt")[0] != first

    def test_skip_comments_ignores_header_only_diff(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("# one\na.com\n", encoding="utf-8")
        first = utils.dir_hash(str(tmp_path), "*.txt", skip_comments=True)[0]
        f.write_text("# two\na.com\n", encoding="utf-8")
        assert utils.dir_hash(str(tmp_path), "*.txt", skip_comments=True)[0] == first

    def test_without_skip_comments_header_diff_matters(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("# one\na.com\n", encoding="utf-8")
        first = utils.dir_hash(str(tmp_path), "*.txt")[0]
        f.write_text("# two\na.com\n", encoding="utf-8")
        assert utils.dir_hash(str(tmp_path), "*.txt")[0] != first


class TestCombinedProductsHash:
    def test_shape_is_pipe_joined(self, tmp_path):
        txt = tmp_path / "t"
        mrs = tmp_path / "m"
        txt.mkdir()
        mrs.mkdir()
        (txt / "a.txt").write_text("a.com\n", encoding="utf-8")
        (mrs / "a.mrs").write_text("MRS", encoding="utf-8")
        combined, c1, c2 = utils.combined_products_hash(str(txt), str(mrs))
        assert (c1, c2) == (1, 1)
        assert combined.count("|") == 3
        assert combined.endswith("|1|1")

    def test_header_change_does_not_trigger_release(self, tmp_path):
        txt = tmp_path / "t"
        mrs = tmp_path / "m"
        txt.mkdir()
        mrs.mkdir()
        (mrs / "a.mrs").write_text("MRS", encoding="utf-8")
        f = txt / "a.txt"
        f.write_text("# Sources: x\na.com\n", encoding="utf-8")
        first, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        f.write_text("# Sources: y\na.com\n", encoding="utf-8")
        second, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        assert first == second

    def test_rule_change_triggers_release(self, tmp_path):
        txt = tmp_path / "t"
        mrs = tmp_path / "m"
        txt.mkdir()
        mrs.mkdir()
        (mrs / "a.mrs").write_text("MRS", encoding="utf-8")
        f = txt / "a.txt"
        f.write_text("a.com\n", encoding="utf-8")
        first, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        f.write_text("b.com\n", encoding="utf-8")
        second, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        assert first != second

    def test_mrs_change_triggers_release(self, tmp_path):
        txt = tmp_path / "t"
        mrs = tmp_path / "m"
        txt.mkdir()
        mrs.mkdir()
        (txt / "a.txt").write_text("a.com\n", encoding="utf-8")
        f = mrs / "a.mrs"
        f.write_text("MRS1", encoding="utf-8")
        first, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        f.write_text("MRS2", encoding="utf-8")
        second, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        assert first != second

    def test_missing_dirs_give_zero_counts(self, tmp_path):
        combined, c1, c2 = utils.combined_products_hash(
            str(tmp_path / "no-txt"), str(tmp_path / "no-mrs"))
        assert (c1, c2) == (0, 0)
        assert combined.count("|") == 3


class TestHashStatePersistence:
    def test_load_missing_returns_none(self, tmp_path):
        assert utils.load_last_hash(str(tmp_path / "absent.sha256")) is None

    def test_save_then_load_roundtrip(self, tmp_path):
        target = tmp_path / "state" / "release.sha256"
        utils.save_last_hash("abc123", str(target))
        assert utils.load_last_hash(str(target)) == "abc123"

    def test_load_strips_whitespace(self, tmp_path):
        target = tmp_path / "h.sha256"
        target.write_text("  abc  \n", encoding="utf-8")
        assert utils.load_last_hash(str(target)) == "abc"

    def test_save_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "h.sha256"
        utils.save_last_hash("x", str(target))
        assert target.exists()

    def test_save_overwrites(self, tmp_path):
        target = tmp_path / "h.sha256"
        utils.save_last_hash("one", str(target))
        utils.save_last_hash("two", str(target))
        assert utils.load_last_hash(str(target)) == "two"

    def test_save_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "h.sha256"
        utils.save_last_hash("abc", str(target))
        assert [p.name for p in tmp_path.iterdir()] == ["h.sha256"]


class TestNormalizePolicy:
    def test_block_synonyms(self):
        for raw in ["reject", "reject-list", "block", "deny", "ads", "adblock", "REJECT"]:
            assert utils.normalize_policy(raw) == "block", raw

    def test_direct_synonyms(self):
        for raw in ["direct", "bypass", "no-proxy", "DIRECT"]:
            assert utils.normalize_policy(raw) == "direct", raw

    def test_policy_synonyms(self):
        for raw in ["proxy", "proxy-list", "gfw", "POLICY"]:
            assert utils.normalize_policy(raw) == "policy", raw

    def test_empty_falls_back_to_proxy(self):
        assert utils.normalize_policy("") == "proxy"

    def test_block_wins_over_proxy_when_both_present(self):
        assert utils.normalize_policy("ads-proxy") == "block"

    def test_direct_wins_over_policy(self):
        assert utils.normalize_policy("direct-gfw") == "direct"

    def test_substring_match_is_intentional(self):
        assert utils.normalize_policy("adservice") == "block"

    def test_unknown_policy_passes_through(self):
        assert utils.normalize_policy("custom") == "custom"


class TestNormalizeType:
    def test_ip_variants(self):
        for raw in ["ip", "ipcidr", "IP-CIDR", "cidr", "IPCidr"]:
            assert utils.normalize_type(raw) == "ipcidr", raw

    def test_everything_else_is_domain(self):
        for raw in ["domain", "general", "", "domain-suffix"]:
            assert utils.normalize_type(raw) == "domain", raw


class TestGetOwnerFromUrl:
    def test_github_repo_url(self):
        assert utils.get_owner_from_url(
            "https://github.com/Loyalsoldier/clash-rules/raw/release/reject.txt"
        ) == "Loyalsoldier"

    def test_github_org_only(self):
        assert utils.get_owner_from_url("https://github.com/") == "github"

    def test_jsdelivr_gh_form(self):
        assert utils.get_owner_from_url(
            "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/geo/geosite/x.list"
        ) == "MetaCubeX"

    def test_jsdelivr_non_gh_falls_back_to_label(self):
        assert utils.get_owner_from_url("https://cdn.jsdelivr.net/npm/foo/index.js") == "jsdelivr"

    def test_other_host_returns_hostname(self):
        assert utils.get_owner_from_url("https://example.com/a/b.txt") == "example.com"

    def test_short_url_returns_unknown(self):
        assert utils.get_owner_from_url("nonsense") == "unknown"

    def test_raw_githubusercontent_owner_extracted(self):
        assert utils.get_owner_from_url(
            "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/reject.txt"
        ) == "Loyalsoldier"

    def test_gist_owner_extracted(self):
        assert utils.get_owner_from_url(
            "https://gist.githubusercontent.com/someuser/abc123/raw/x.txt"
        ) == "someuser"


class TestNormalizePath:
    def test_posix_form_from_path_object_has_no_backslash(self):
        import pathlib

        for raw in ("a/b/c.txt", "merged-rules/x/y.txt"):
            assert "\\" not in utils.normalize_path(pathlib.Path(raw))

    def test_plain_path_unchanged(self):
        assert utils.normalize_path("a/b.txt") == "a/b.txt"

    def test_redundant_segments_cleaned(self):
        assert utils.normalize_path("a/./b.txt") == "a/b.txt"
        assert utils.normalize_path("./x.txt") == "x.txt"
        assert utils.normalize_path("a//b.txt") == "a/b.txt"
        assert utils.normalize_path("d/../x.txt") == "d/../x.txt"


class TestBeijingTime:
    def test_offset_is_utc8(self):
        import datetime

        assert utils.beijing_now().utcoffset() == datetime.timedelta(hours=8)

    def test_timestamp_format(self):
        ts = utils.beijing_timestamp()
        assert len(ts) == len("YYYY-MM-DD HH:MM:SS")
        assert ts[4] == "-" and ts[10] == " " and ts[13] == ":"

    def test_timestamp_close_to_now(self):
        import datetime

        now = utils.beijing_now()
        parsed = datetime.datetime.strptime(utils.beijing_timestamp(), "%Y-%m-%d %H:%M:%S")
        assert abs((parsed - now.replace(tzinfo=None)).total_seconds()) < 5


class TestCleanDirectory:
    def test_missing_dir_created_when_keep_root(self, tmp_path):
        target = tmp_path / "new"
        assert utils.clean_directory(str(target), keep_root=True) == []
        assert target.is_dir()

    def test_missing_dir_not_created_when_not_keep_root(self, tmp_path):
        target = tmp_path / "new"
        assert utils.clean_directory(str(target), keep_root=False) == []
        assert not target.exists()

    def test_removes_files_and_subdirs(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
        (tmp_path / "top.txt").write_text("y", encoding="utf-8")
        assert utils.clean_directory(str(tmp_path)) == []
        assert list(tmp_path.iterdir()) == []

    def test_returns_failures_instead_of_swallowing(self, tmp_path):
        target = tmp_path / "blocked.txt"
        target.write_text("x", encoding="utf-8")
        original = os.unlink

        def boom(path):
            raise PermissionError("in use")

        os.unlink = boom
        try:
            failed = utils.clean_directory(str(tmp_path))
        finally:
            os.unlink = original
        assert len(failed) == 1
        assert "blocked.txt" in failed[0][0]
        assert "in use" in failed[0][1]


class TestDedupDomainSuffix:
    def test_suffix_parent_covers_suffix_child(self):
        kept, removed = utils.dedup_domain_suffix({"+.google.com", "+.ads.google.com"})
        assert kept == ["+.google.com"]
        assert removed == 1

    def test_bare_domain_does_not_cover_child(self):
        kept, removed = utils.dedup_domain_suffix({"google.com", "ads.google.com"})
        assert kept == ["ads.google.com", "google.com"]
        assert removed == 0

    def test_suffix_parent_covers_exact_child(self):
        kept, removed = utils.dedup_domain_suffix({"+.google.com", "ads.google.com"})
        assert kept == ["+.google.com"]
        assert removed == 1

    def test_same_name_suffix_subsumes_exact(self):
        kept, removed = utils.dedup_domain_suffix({"+.google.com", "google.com"})
        assert kept == ["+.google.com"]
        assert removed == 1

    def test_suffix_covers_same_name_subdomain(self):
        kept, removed = utils.dedup_domain_suffix({"+.google.com", ".google.com"})
        assert kept == ["+.google.com"]
        assert removed == 1

    def test_subdomain_and_exact_coexist(self):
        kept, removed = utils.dedup_domain_suffix({".google.com", "google.com"})
        assert kept == [".google.com", "google.com"]
        assert removed == 0

    def test_subdomain_entries_do_not_cover_each_other(self):
        kept, removed = utils.dedup_domain_suffix({".google.com", ".ads.google.com"})
        assert kept == [".ads.google.com", ".google.com"]
        assert removed == 0

    def test_suffix_covers_subdomain_child(self):
        kept, removed = utils.dedup_domain_suffix({"+.google.com", ".ads.google.com"})
        assert kept == ["+.google.com"]
        assert removed == 1

    def test_single_exact_entry_kept(self):
        kept, removed = utils.dedup_domain_suffix({"google.com"})
        assert kept == ["google.com"]
        assert removed == 0

    def test_multi_level_suffix_chain(self):
        kept, removed = utils.dedup_domain_suffix({"+.com", "+.google.com", "+.ads.google.com"})
        assert kept == ["+.com"]
        assert removed == 2

    def test_order_independent(self):
        a, _ = utils.dedup_domain_suffix({"+.google.com", "+.ads.google.com"})
        b, _ = utils.dedup_domain_suffix({"+.ads.google.com", "+.google.com"})
        assert a == b

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


class TestDomainTrie:
    def test_covering_parent_returns_strict_ancestor(self):
        trie = utils.DomainTrie()
        trie.add("google.com", utils.DomainTrie.SUFFIX)
        assert trie.covering_parent("ads.google.com") == "google.com"

    def test_covering_parent_none_for_self(self):
        trie = utils.DomainTrie()
        trie.add("google.com", utils.DomainTrie.SUFFIX)
        assert trie.covering_parent("google.com") is None

    def test_covering_parent_none_for_unrelated(self):
        trie = utils.DomainTrie()
        trie.add("google.com", utils.DomainTrie.SUFFIX)
        assert trie.covering_parent("youtube.com") is None

    def test_covering_parent_returns_outermost_marked_ancestor(self):
        trie = utils.DomainTrie()
        trie.add("com", utils.DomainTrie.SUFFIX)
        trie.add("google.com", utils.DomainTrie.SUFFIX)
        assert trie.covering_parent("ads.google.com") == "com"

    def test_has_marked_ancestor_includes_self(self):
        trie = utils.DomainTrie()
        trie.add("google.com")
        assert trie.has_marked_ancestor("google.com") is True
        assert trie.has_marked_ancestor("ads.google.com") is True
        assert trie.has_marked_ancestor("example.org") is False

    def test_suffix_and_exact_marks_are_distinguishable(self):
        trie = utils.DomainTrie()
        trie.add("markedsuffix.com", utils.DomainTrie.SUFFIX)
        trie.add("markedexact.com", utils.DomainTrie.EXACT)
        assert trie.has_marked_ancestor("a.markedsuffix.com", utils.DomainTrie.SUFFIX) is True
        assert trie.has_marked_ancestor("a.markedexact.com", utils.DomainTrie.SUFFIX) is False
        assert trie.has_marked_ancestor("a.markedexact.com", utils.DomainTrie.EXACT) is True

    def test_covering_parent_respects_kind(self):
        trie = utils.DomainTrie()
        trie.add("exactonly.com", utils.DomainTrie.EXACT)
        assert trie.covering_parent("sub.exactonly.com", utils.DomainTrie.SUFFIX) is None
        assert trie.covering_parent("sub.exactonly.com") == "exactonly.com"

    def test_child_does_not_mark_parent(self):
        trie = utils.DomainTrie()
        trie.add("ads.google.com")
        assert trie.has_marked_ancestor("google.com") is False
        assert trie.covering_parent("google.com") is None


class TestFlattenIpDefaultRoute:
    def test_ipv4_default_route_dropped(self):
        result, _errors = utils.flatten_ip_cidr(["9.0.0.0/8", "0.0.0.0/0"])
        assert result == ["9.0.0.0/8"]

    def test_dropped_default_routes_are_reported(self):
        dropped = []
        result, _errors = utils.flatten_ip_cidr(
            ["1.0.0.0/24", "0.0.0.0/0", "::/0"], dropped_default_routes=dropped)
        assert result == ["1.0.0.0/24"]
        assert dropped == ["0.0.0.0/0", "::/0"]

    def test_ipv4_default_route_does_not_swallow_others(self):
        result, _errors = utils.flatten_ip_cidr(["1.0.0.0/24", "0.0.0.0/0", "10.0.0.0/8"])
        assert result == ["1.0.0.0/24", "10.0.0.0/8"]

    def test_ipv6_default_route_dropped(self):
        result, _errors = utils.flatten_ip_cidr(["2001:db8::/32", "::/0"])
        assert result == ["2001:db8::/32"]


class TestFlattenIpModes:
    def test_strict_mode_rejects_trailing_text(self):
        result, errors = utils.flatten_ip_cidr(["1.2.3.0/24 # comment"])
        assert result == []
        assert len(errors) == 1

    def test_extract_mode_reads_trailing_text(self):
        result, errors = utils.flatten_ip_cidr(["1.2.3.0/24 # comment"], extract=True)
        assert result == ["1.2.3.0/24"]
        assert errors == []

    def test_extract_mode_matches_processor_pipeline(self):
        import processor

        lines = ["  1.2.3.0/24 # note", "10.0.0.0/8"]
        result, errors, _stats = processor.process_ip_detailed(lines)
        assert (result, errors) == utils.flatten_ip_cidr(lines, extract=True)


class TestFlattenIpOrdering:
    def test_lexical_within_family(self):
        result, _errors = utils.flatten_ip_cidr(["1.0.8.0/24", "1.0.32.0/24"])
        assert result == ["1.0.32.0/24", "1.0.8.0/24"]

    def test_v4_block_before_v6_block(self):
        result, _errors = utils.flatten_ip_cidr(["2001:db8::/32", "10.0.0.0/8"])
        assert result == ["10.0.0.0/8", "2001:db8::/32"]
