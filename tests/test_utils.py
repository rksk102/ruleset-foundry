import hashlib

import utils


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

    def test_exact_parent_not_removed_when_no_suffix_ancestor(self):
        kept, removed = utils.dedup_domain_suffix({"google.com", "ads.google.com"})
        assert kept == ["ads.google.com", "google.com"]
        assert removed == 0

    def test_duplicates_dropped(self):
        kept, removed = utils.dedup_domain_suffix({"google.com", "google.com"})
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

    def test_extract_mode_matches_legacy_process_ip(self):
        import processor

        lines = ["IP-CIDR,x", "  1.2.3.0/24 # note", "10.0.0.0/8"]
        assert processor.process_ip(lines)[0] == utils.flatten_ip_cidr(lines, extract=True)[0]


class TestFlattenIpOrdering:
    def test_lexical_within_family(self):
        result, _errors = utils.flatten_ip_cidr(["1.0.8.0/24", "1.0.32.0/24"])
        assert result == ["1.0.32.0/24", "1.0.8.0/24"]

    def test_v4_block_before_v6_block(self):
        result, _errors = utils.flatten_ip_cidr(["2001:db8::/32", "10.0.0.0/8"])
        assert result == ["10.0.0.0/8", "2001:db8::/32"]
