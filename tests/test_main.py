import main
import pytest
import utils


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def key(path):
    """build_filepath 用 str(Path(...)) 作 taken 的键，Windows 上是反斜杠。"""
    return str(main.Path(path))


class TestIsTrustedHost:
    def test_github_domains_trusted(self):
        for url in [
            "https://github.com/a/b/raw/x.txt",
            "https://api.github.com/repos/a/b",
            "https://raw.githubusercontent.com/a/b/x.txt",
            "https://objects.githubusercontent.com/a/b",
            "https://codeload.github.com/a/b/tar.gz/x",
            "https://gist.githubusercontent.com/u/i/raw/x",
            "https://github.com",
        ]:
            assert main.is_trusted_host(url) is True, url

    def test_user_content_hosts_are_not_trusted(self):
        for url in [
            "https://user.github.io/repo/x.txt",
            "https://anything.githubassets.com/x.txt",
        ]:
            assert main.is_trusted_host(url) is False, url

    def test_plaintext_http_is_not_trusted(self):
        for url in [
            "http://raw.githubusercontent.com/a/b/x.txt",
            "http://github.com/a/b",
        ]:
            assert main.is_trusted_host(url) is False, url

    def test_third_party_cdn_never_trusted(self):
        assert main.is_trusted_host("https://cdn.jsdelivr.net/gh/a/b@m/x.list") is False
        assert main.is_trusted_host("https://ghproxy.net/x") is False

    def test_untrusted_domains_rejected(self):
        for url in [
            "https://example.com/x.txt",
            "https://evil.com/github.com",
            "https://ghproxy.net/https://raw.githubusercontent.com/a/b/x",
            "https://gitee.com/a/b/raw/x",
        ]:
            assert main.is_trusted_host(url) is False, url

    def test_suffix_confusion_rejected(self):
        for url in [
            "https://notgithub.com/x",
            "https://github.com.evil.com/x",
            "https://fake-raw.githubusercontent.com.evil.io/x",
        ]:
            assert main.is_trusted_host(url) is False, url

    def test_missing_or_invalid_host(self):
        assert main.is_trusted_host("") is False
        assert main.is_trusted_host("not a url") is False
        assert main.is_trusted_host("https:///nohost") is False

    def test_case_insensitive(self):
        assert main.is_trusted_host("https://GitHub.COM/a/b") is True


class TestProductPathStability:
    def test_displayable_chars_survive(self):
        task = {
            "url": "https://github.com/MetaCubeX/meta-rules-dat/raw/refs/heads/meta/geo/geosite/category-ai-!cn.list",
            "policy": "policy",
            "type": "domain",
            "domain_kind": "exact",
        }
        owner, name, rel, _abs_path = main.build_filepath(task)
        assert (owner, name) == ("MetaCubeX", "category-ai-!cn.txt")
        assert rel.as_posix() == "policy/domain/MetaCubeX/category-ai-!cn.txt"

    def test_separators_and_reserved_chars_still_cleaned(self):
        assert main.clean_path_component("a/b\\c:d*e?f", "x") == "a_b_c_d_e_f"
        assert main.clean_path_component("..", "x") == "x"

    def test_trailing_dot_and_reserved_names_fall_back(self):
        assert main.clean_path_component("foo.", "rules") == "foo"
        assert main.clean_path_component("CON", "rules") == "rules"
        assert main.clean_path_component("CON.txt", "rules") == "rules"


class TestAuthHeaders:
    def test_token_sent_to_trusted_host(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        assert main.auth_headers("https://raw.githubusercontent.com/a/b/x") == {"Authorization": "Bearer tok"}

    def test_token_withheld_from_untrusted_host(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        assert main.auth_headers("https://example.com/x") is None
        assert main.auth_headers("https://ghproxy.net/https://raw.githubusercontent.com/a/b") is None

    def test_token_withheld_from_plaintext_and_user_content(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok")
        assert main.auth_headers("http://raw.githubusercontent.com/a/b/x") is None
        assert main.auth_headers("https://someone.github.io/x") is None

    def test_no_token_means_no_header(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert main.auth_headers("https://github.com/a/b") is None

    def test_github_token_used_as_fallback(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "fallback")
        assert main.auth_headers("https://github.com/a/b") == {"Authorization": "Bearer fallback"}


class TestParseRetryAfter:
    def test_missing_returns_default(self):
        assert main.parse_retry_after(None, 5) == 5
        assert main.parse_retry_after("", 5) == 5

    def test_plain_seconds(self):
        assert main.parse_retry_after("30", 5) == 30

    def test_whitespace_tolerated(self):
        assert main.parse_retry_after("  30  ", 5) == 30

    def test_zero_seconds(self):
        assert main.parse_retry_after("0", 5) == 0

    def test_capped_at_max(self):
        assert main.parse_retry_after(str(main.MAX_RETRY_AFTER + 1000), 5) == main.MAX_RETRY_AFTER

    def test_uncapped_parse_keeps_server_request(self):
        assert (
            main.parse_retry_after(str(main.MAX_RETRY_AFTER + 1000), 5, max_seconds=None) == main.MAX_RETRY_AFTER + 1000
        )

    def test_negative_rejected_as_non_decimal(self):
        assert main.parse_retry_after("-5", 5) == 5
        assert main.parse_retry_after("  -5  ", 5) == 5

    def test_unicode_digits_rejected(self):
        assert main.parse_retry_after("３０", 7) == 7

    def test_http_date_parsed(self):
        import datetime
        from email.utils import format_datetime

        when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=20)
        parsed = main.parse_retry_after(format_datetime(when), 5)
        assert 15 <= parsed <= 21

    def test_http_date_in_past_clamped(self):
        parsed = main.parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT", 5)
        assert parsed == 0

    def test_http_date_far_future_capped(self):
        parsed = main.parse_retry_after("Fri, 01 Jan 2100 00:00:00 GMT", 5)
        assert parsed == main.MAX_RETRY_AFTER

    def test_garbage_returns_default(self):
        assert main.parse_retry_after("not-a-date", 9) == 9


class TestSourceRepoSlug:
    def test_github_repo(self):
        assert main.source_repo_slug("https://github.com/a/b/raw/main/x.txt") == "a__b"

    def test_raw_githubusercontent(self):
        assert main.source_repo_slug("https://raw.githubusercontent.com/a/b/main/x.txt") == "a__b"

    def test_codeload(self):
        assert main.source_repo_slug("https://codeload.github.com/a/b/tar.gz/main") == "a__b"

    def test_github_io_uses_first_two_path_segments(self):
        assert main.source_repo_slug("https://user.github.io/a/b/x.txt") == "a__b"
        assert main.source_repo_slug("https://user.github.io/repo/x.txt") == "repo__x.txt"

    def test_github_io_bare_host(self):
        assert main.source_repo_slug("https://github.io/x") == ""

    def test_jsdelivr_gh_form(self):
        assert (
            main.source_repo_slug("https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/x.list")
            == "MetaCubeX__meta-rules-dat"
        )

    def test_jsdelivr_strips_version_tag(self):
        assert main.source_repo_slug("https://cdn.jsdelivr.net/gh/o/r@v1.2.3/x") == "o__r"

    def test_jsdelivr_non_gh_form(self):
        assert main.source_repo_slug("https://cdn.jsdelivr.net/npm/pkg@1/x.js") == "npm__pkg"

    def test_unrecognised_host_falls_back_to_hostname(self):
        assert main.source_repo_slug("https://example.com/a/b.txt") == "example_com"

    def test_empty_when_path_lacks_repo_segment(self):
        assert main.source_repo_slug("https://github.com/onlyone") == ""
        assert main.source_repo_slug("https://raw.githubusercontent.com/") == ""
        assert main.source_repo_slug("https://cdn.jsdelivr.net/") == ""

    def test_hostname_fallback_never_empty(self):
        for url in [
            "https://example.com/x",
            "https://mirror.example.org/a/b",
            "https://ghproxy.net/https://raw.githubusercontent.com/a/b",
        ]:
            assert main.source_repo_slug(url) != "", url

    def test_empty_for_hostless_url(self):
        assert main.source_repo_slug("not a url") == ""


class TestBaseRelPath:
    def task(self, url, policy="block", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def test_layout_is_policy_type_owner_filename(self):
        rel = main._base_rel_path(self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/reject.txt"))
        assert rel.as_posix() == "block/domain/Loyalsoldier/reject.txt"

    def test_extension_forced_to_txt(self):
        rel = main._base_rel_path(self.task("https://github.com/o/r/raw/m/geo.list"))
        assert rel.name == "geo.txt"

    def test_query_and_fragment_stripped(self):
        rel = main._base_rel_path(self.task("https://github.com/o/r/raw/m/x.txt?a=1#frag"))
        assert rel.name == "x.txt"

    def test_type_ipcidr(self):
        rel = main._base_rel_path(self.task("https://github.com/o/r/raw/m/x.txt", type_="ipcidr"))
        assert rel.as_posix().startswith("block/ipcidr/")

    EXPECTED_REAL_SOURCE_PATHS = (
        "block/domain/MetaCubeX/category-ads-all.txt",
        "block/domain/DustinWin/ads.txt",
        "block/domain/Loyalsoldier/reject.txt",
        "block/domain/Loyalsoldier/reject-list.txt",
        "block/domain/Loyalsoldier/win-extra.txt",
        "block/domain/Loyalsoldier/win-spy.txt",
        "direct/domain/Loyalsoldier/private.txt",
        "direct/domain/MetaCubeX/geolocation-cn.txt",
        "direct/domain/DustinWin/microsoft-cn.txt",
        "direct/domain/Loyalsoldier/direct-list.txt",
        "policy/domain/Loyalsoldier/proxy.txt",
        "policy/domain/Loyalsoldier/gfw.txt",
        "policy/domain/MetaCubeX/category-ai-!cn.txt",
        "policy/domain/Loyalsoldier/proxy-list.txt",
        "policy/domain/Loyalsoldier/gfw.txt",
        "direct/ipcidr/Loyalsoldier/cncidr.txt",
        "direct/ipcidr/MetaCubeX/cn.txt",
        "direct/ipcidr/Loyalsoldier/lancidr.txt",
    )

    def test_real_sources_output_paths_unchanged(self):
        tasks = main.parse_sources()
        actual = [main._base_rel_path(t).as_posix() for t in tasks]
        assert actual == list(self.EXPECTED_REAL_SOURCE_PATHS)

    def test_multi_dot_filename_keeps_stem(self):
        rel = main._base_rel_path(self.task("https://github.com/o/r/raw/m/foo.bar.list"))
        assert rel.name == "foo.bar.txt"


class TestNormalizeSourceUrl:
    def test_userinfo_is_kept_for_deduplication(self):
        assert main.normalize_source_url("https://user1@localhost/x") != main.normalize_source_url(
            "https://user2@localhost/x"
        )


class TestCollidingOutputPaths:
    def task(self, url, policy="policy", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def posix_plan(self, plan):
        return {main.Path(k).as_posix(): v for k, v in plan.items()}

    def test_same_repo_same_output_path_is_rejected(self):
        with pytest.raises(SystemExit) as exc:
            main.plan_groups(
                [
                    self.task("https://github.com/Loyalsoldier/x/raw/r/gfw.txt"),
                    self.task("https://github.com/Loyalsoldier/x/raw/r2/gfw.txt"),
                ]
            )
        assert exc.value.code == 1

    def test_different_repos_same_owner_and_filename_flagged(self):
        plan = self.posix_plan(
            main.colliding_output_paths(
                [
                    self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
                    self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt"),
                ]
            )
        )
        assert list(plan) == ["policy/domain/Loyalsoldier/gfw.txt"]
        assert plan["policy/domain/Loyalsoldier/gfw.txt"] == [
            "Loyalsoldier__clash-rules",
            "Loyalsoldier__v2ray-rules-dat",
        ]

    def test_mirror_host_spoofing_flagged(self):
        plan = self.posix_plan(
            main.colliding_output_paths(
                [
                    self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
                    self.task("https://raw.githubusercontent.com/Loyalsoldier/other/main/gfw.txt"),
                ]
            )
        )
        assert "policy/domain/Loyalsoldier/gfw.txt" in plan

    def test_same_url_repeated_not_flagged(self):
        t = self.task("https://github.com/o/r/raw/m/gfw.txt")
        assert main.colliding_output_paths([t, dict(t)]) == {}

    def test_different_policy_not_colliding(self):
        plan = main.colliding_output_paths(
            [
                self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt", policy="block"),
                self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt", policy="policy"),
            ]
        )
        assert plan == {}


class TestBuildFilepath:
    def task(self, url, policy="policy", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def test_default_owner_used_when_no_collision(self):
        owner, name, rel, abs_path = main.build_filepath(
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/reject.txt")
        )
        assert owner == "Loyalsoldier"
        assert name == "reject.txt"
        assert rel.as_posix() == "policy/domain/Loyalsoldier/reject.txt"
        assert abs_path == main.RULESETS_DIR / rel

    def test_collision_uses_repo_slug_directory(self):
        task = self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt")
        plan = {key("policy/domain/Loyalsoldier/gfw.txt"): ["Loyalsoldier__clash-rules"]}
        owner, _name, rel, _abs = main.build_filepath(task, plan, {})
        assert owner == "Loyalsoldier__clash-rules"
        assert rel.as_posix() == "policy/domain/Loyalsoldier__clash-rules/gfw.txt"

    def test_disambiguation_avoids_taken_path(self):
        task = self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt")
        plan = {key("policy/domain/Loyalsoldier/gfw.txt"): ["x"]}
        taken = {key("policy/domain/Loyalsoldier__clash-rules/gfw.txt"): "someone-else"}
        owner, _name, rel, _abs = main.build_filepath(task, plan, taken)
        assert owner == "Loyalsoldier__clash-rules_"
        assert rel.as_posix() == "policy/domain/Loyalsoldier__clash-rules_/gfw.txt"

    def test_taken_records_slug(self):
        taken = {}
        main.build_filepath(self.task("https://github.com/o/r/raw/m/x.txt"), None, taken)
        assert taken == {key("policy/domain/o/x.txt"): "o__r"}

    def test_same_slug_does_not_trigger_disambiguation(self):
        task = self.task("https://github.com/o/r/raw/m/x.txt")
        taken = {key("policy/domain/o__r/x.txt"): "o__r"}
        plan = {key("policy/domain/o/x.txt"): ["o__r"]}
        owner, _name, rel, _abs = main.build_filepath(task, plan, taken)
        assert owner == "o__r"
        assert rel.as_posix() == "policy/domain/o__r/x.txt"


class TestPlanGroups:
    def task(self, url, policy="policy", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def test_single_source_single_group(self):
        groups = main.plan_groups([self.task("https://github.com/o/r/raw/m/x.txt")])
        assert len(groups) == 1
        assert groups[0]["sources"] == ["https://github.com/o/r/raw/m/x.txt"]

    def test_same_url_with_conflicting_domain_kind_becomes_mixed(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        groups = main.plan_groups(
            [
                {**self.task(url), "domain_kind": "exact"},
                {**self.task(url), "domain_kind": "suffix"},
            ]
        )
        assert len(groups) == 1
        assert groups[0]["domain_kind"] == "mixed"

    def test_same_url_twice_is_deduped(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        groups = main.plan_groups([self.task(url), self.task(url)])
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 1
        assert groups[0]["sources"] == [url]

    def test_same_url_host_case_is_deduped(self):
        groups = main.plan_groups(
            [
                self.task("https://github.com/o/r/raw/m/x.txt"),
                self.task("https://GitHub.com/o/r/raw/m/x.txt"),
            ]
        )
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 1

    def test_same_repo_collision_fails(self):
        with pytest.raises(SystemExit) as exc:
            main.plan_groups(
                [
                    self.task("https://github.com/o/r/raw/m/x.txt"),
                    self.task("https://github.com/o/r/raw/other/x.txt"),
                ]
            )
        assert exc.value.code == 1

    def test_collision_splits_into_separate_groups(self):
        groups = main.plan_groups(
            [
                self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
                self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt"),
            ]
        )
        assert len(groups) == 2
        paths = sorted(str(g["path"]).replace("\\", "/") for g in groups)
        assert paths == [
            "rulesets/policy/domain/Loyalsoldier__clash-rules/gfw.txt",
            "rulesets/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.txt",
        ]

    def test_every_group_has_unique_output_path(self):
        tasks = [
            self.task("https://github.com/o/r/raw/m/x.txt"),
            self.task("https://raw.githubusercontent.com/o/r2/main/x.txt"),
            self.task("https://github.com/other/thing/raw/m/x.txt"),
        ]
        groups = main.plan_groups(tasks)
        paths = [str(g["path"]).replace("\\", "/") for g in groups]
        assert len(paths) == len(set(paths))

    def test_groups_sorted_by_path(self):
        groups = main.plan_groups(
            [
                self.task("https://github.com/b/b/raw/m/x.txt"),
                self.task("https://github.com/a/a/raw/m/x.txt"),
            ]
        )
        paths = [str(g["path"]).replace("\\", "/") for g in groups]
        assert paths == sorted(paths)

    def test_member_indices_reference_input(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        tasks = [self.task(url), self.task(url)]
        groups = main.plan_groups(tasks)
        indices = [i for i, _t in groups[0]["members"]]
        assert indices == [0]
        for i, t in groups[0]["members"]:
            assert t is tasks[i]

    def test_defaults_to_exact_without_domain_kind(self):
        groups = main.plan_groups([self.task("https://github.com/o/r/raw/m/x.txt")])
        assert groups[0]["domain_kind"] == "exact"

    def test_empty_input(self):
        assert main.plan_groups([]) == []

    def test_casefold_output_collision_is_rejected(self):
        with pytest.raises(SystemExit) as exc:
            main.plan_groups(
                [
                    self.task("https://github.com/Owner/repo/raw/m/Rules.txt"),
                    self.task("https://github.com/owner/repo/raw/m/rules.txt"),
                ]
            )
        assert exc.value.code == 1


class TestParseSources:
    def load(self, tmp_path, content):
        target = tmp_path / "sources.urls"
        write(target, content)
        original = main.SOURCES_FILE
        main.SOURCES_FILE = str(target)
        try:
            return main.parse_sources()
        finally:
            main.SOURCES_FILE = original

    def test_defaults_when_no_markers(self, tmp_path):
        tasks = self.load(tmp_path, "https://github.com/a/b/raw/m/x.txt\n")
        assert tasks == [
            {"policy": "policy", "type": "domain", "domain_kind": "exact", "url": "https://github.com/a/b/raw/m/x.txt"}
        ]

    def test_policy_and_type_markers_apply_to_following_lines(self, tmp_path):
        tasks = self.load(
            tmp_path,
            "\n".join(
                [
                    "[policy:block]",
                    "[type:domain]",
                    "https://github.com/a/b/raw/m/a.txt",
                    "[policy:direct]",
                    "https://github.com/a/b/raw/m/b.txt",
                    "[policy:reject]",
                    "https://github.com/a/b/raw/m/c.txt",
                ]
            )
            + "\n",
        )
        assert [t["policy"] for t in tasks] == ["block", "direct", "block"]
        assert [t["url"].rsplit("/", 1)[1] for t in tasks] == ["a.txt", "b.txt", "c.txt"]

    def test_type_marker_switches_to_ipcidr(self, tmp_path):
        tasks = self.load(
            tmp_path,
            "\n".join(
                [
                    "[policy:direct]",
                    "[type:ipcidr]",
                    "https://github.com/a/b/raw/m/cn.txt",
                    "[type:domain]",
                    "https://github.com/a/b/raw/m/domain.txt",
                ]
            )
            + "\n",
        )
        assert [t["type"] for t in tasks] == ["ipcidr", "domain"]

    def test_unknown_type_marker_rejected(self, tmp_path):
        try:
            self.load(tmp_path, "[type:ip]\nhttps://github.com/a/b/raw/m/x.txt\n")
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("非法 [type:] 标记应 SystemExit(1)")

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        tasks = self.load(
            tmp_path,
            "\n".join(
                [
                    "# comment",
                    "",
                    "   ",
                    "https://github.com/a/b/raw/m/x.txt",
                ]
            )
            + "\n",
        )
        assert len(tasks) == 1

    def test_trailing_comment_after_url_ignored(self, tmp_path):
        tasks = self.load(tmp_path, "https://github.com/a/b/raw/m/x.txt # 说明\n")
        assert tasks[0]["url"] == "https://github.com/a/b/raw/m/x.txt"

    def test_bom_stripped(self, tmp_path):
        tasks = self.load(tmp_path, "\ufeffhttps://github.com/a/b/raw/m/x.txt\n")
        assert len(tasks) == 1
        assert tasks[0]["url"].startswith("https://")

    def test_missing_file_exits(self, tmp_path):
        original = main.SOURCES_FILE
        main.SOURCES_FILE = str(tmp_path / "absent.urls")
        try:
            try:
                main.parse_sources()
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("缺失 sources 文件应 SystemExit(1)")
        finally:
            main.SOURCES_FILE = original

    def test_http_source_rejected(self, tmp_path):
        try:
            self.load(tmp_path, "http://example.com/x.txt\n")
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("http 明文源应 SystemExit(1)")

    def test_non_https_scheme_lines_rejected(self, tmp_path):
        for bad in ("ftp://example.com/x.txt", "HTTP://example.com/x.txt", "htp://example.com/x.txt"):
            try:
                self.load(tmp_path, bad + "\n")
            except SystemExit as e:
                assert e.code == 1, bad
            else:
                raise AssertionError(f"{bad} 不应被静默跳过，应 SystemExit(1)")

    def test_prose_line_without_scheme_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            self.load(tmp_path, "这行没有协议前缀\nhttps://github.com/a/b/raw/m/x.txt\n")
        assert exc.value.code == 1


class TestMixedDomainKind:
    def test_mixed_group_is_parse_error_without_writing(self, monkeypatch):
        group = {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "domain_kind": "mixed",
            "members": [
                (0, {"url": "https://example.com/a.txt", "policy": "block", "type": "domain", "domain_kind": "exact"}),
                (1, {"url": "https://example.com/b.txt", "policy": "block", "type": "domain", "domain_kind": "suffix"}),
            ],
            "sources": ["https://example.com/a.txt", "https://example.com/b.txt"],
        }
        writes = []
        monkeypatch.setattr(main, "atomic_write", lambda *args: writes.append(args))

        count, errors = main.process_group(group, {0: b"a.com\n", 1: b"b.com\n"})

        assert count is None
        assert len(errors["parse"]) == 2
        assert all("mixed" in reason or "混用" in reason for _url, reason in errors["parse"])
        assert writes == []


class TestProcessGroupGates:
    def test_default_routes_do_not_dilute_unexpected_drop_gate(self, monkeypatch):
        group = {
            "path": "rulesets/direct/ipcidr/Owner/cn.txt",
            "policy": "direct",
            "type": "ipcidr",
            "members": [(0, {"url": "https://example.com/cn.txt"})],
        }
        lines = ["DOMAIN-SUFFIX,ads.example.com"] * 20 + ["0.0.0.0/0"] * 180 + ["1.1.1.0/24"]
        writes = []
        monkeypatch.setattr(main, "atomic_write", lambda *args: writes.append(args))

        count, errors = main.process_group(group, {0: ("\n".join(lines) + "\n").encode()})

        assert count is None
        assert errors["parse"]
        assert writes == []

    def test_process_group_writes_the_validated_anchored_path(self, monkeypatch, work_dir):
        group = {
            "path": "rulesets/block/domain/Owner/ads.txt",
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(0, {"url": "https://example.com/ads.txt"})],
        }
        monkeypatch.chdir(work_dir)
        expected = main.ensure_within_rulesets(group["path"])
        writes = []
        monkeypatch.setattr(main, "atomic_write", lambda path, content: writes.append(path))

        count, errors = main.process_group(group, {0: b"good.example.com\n"})

        assert count == 1
        assert errors["parse"] == []
        assert writes == [expected]


class TestProcessIpGroup:
    def test_domain_rules_are_counted_as_dropped_types(self):
        result, stats = main._process_ip_group(["DOMAIN-SUFFIX,ads.example.com"])

        assert result == []
        assert stats["dropped_rule_type"]["DOMAIN-SUFFIX"] == 1

    def test_default_route_drop_is_reported(self):
        result, stats = main._process_ip_group(["0.0.0.0/0", "1.0.0.0/24"])

        assert result == ["1.0.0.0/24"]
        assert stats["dropped_default_route"] == 1


class TestRulesetsRootAnchor:
    def test_repository_root_is_rejected_as_rulesets_dir(self, work_dir, monkeypatch):
        repo = work_dir / "repo"
        repo.mkdir()
        monkeypatch.setattr(main, "REPO_ROOT", repo)
        monkeypatch.setattr(utils, "REPO_ROOT", repo)
        monkeypatch.setattr(main, "RULESETS_DIR", repo)

        with pytest.raises(SystemExit) as exc:
            main._resolved_rulesets_dir()

        assert exc.value.code == 1

    def test_external_symlink_is_rejected_before_orphan_cleanup(self, work_dir, monkeypatch):
        repo = work_dir / "repo"
        repo.mkdir()
        outside = work_dir / "outside"
        outside.mkdir()
        victim = outside / "keep.txt"
        victim.write_text("keep\n", encoding="utf-8")
        link = repo / "rulesets"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as e:
            pytest.skip(f"symlink unavailable: {e}")
        monkeypatch.setattr(main, "REPO_ROOT", repo)
        monkeypatch.setattr(utils, "REPO_ROOT", repo)
        monkeypatch.setattr(main, "RULESETS_DIR", link)
        removed = []
        monkeypatch.setattr(main.os, "remove", lambda path: removed.append(path))

        with pytest.raises(SystemExit) as exc:
            main.clean_orphans([])

        assert exc.value.code == 1
        assert removed == []
        assert victim.read_text(encoding="utf-8") == "keep\n"


class TestStagedSync:
    def _setup(self, work_dir, monkeypatch):
        repo = work_dir / "repo"
        root = repo / "rulesets"
        root.mkdir(parents=True)
        monkeypatch.setattr(main, "REPO_ROOT", repo)
        monkeypatch.setattr(utils, "REPO_ROOT", repo)
        monkeypatch.setattr(main, "RULESETS_DIR", root)
        monkeypatch.setattr(main, "MANIFEST_FILE", root / "products.manifest")
        monkeypatch.setattr(main, "MIN_SUCCESS_RATIO", 0.0)
        monkeypatch.setattr(main, "ALLOW_PARTIAL", False)
        monkeypatch.setattr(main, "generate_summary", lambda *a, **k: None)
        for name in (
            "info",
            "warning",
            "success",
            "debug",
            "group_start",
            "group_end",
            "section",
            "gh_error",
        ):
            monkeypatch.setattr(main, name, lambda *a, **k: None)
        return root

    def _prepare(self, root, monkeypatch, groups, raw):
        tasks = [task for group in groups for _idx, task in group["members"]]
        monkeypatch.setattr(main, "parse_sources", lambda: tasks)
        monkeypatch.setattr(main, "plan_groups", lambda _tasks: groups)

        async def fake_download(_tasks):
            return [
                (group["members"][0][1], data, None)
                for group, data in zip(groups, raw, strict=True)
            ]

        monkeypatch.setattr(main, "download_all", fake_download)

    def _group(self, root, name, index, url):
        return {
            "path": str(root / "block" / "domain" / "Owner" / name),
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(index, {"url": url, "policy": "block", "type": "domain"})],
        }

    def test_success_commits_all_staged_outputs(self, work_dir, monkeypatch):
        root = self._setup(work_dir, monkeypatch)
        groups = [
            self._group(root, "a.txt", 0, "https://example.com/a.txt"),
            self._group(root, "b.txt", 1, "https://example.com/b.txt"),
        ]
        self._prepare(root, monkeypatch, groups, [b"good.com\n", b"other.com\n"])

        main.main()

        assert (root / "block/domain/Owner/a.txt").read_text(encoding="utf-8").splitlines()[-1] == "good.com"
        assert (root / "block/domain/Owner/b.txt").read_text(encoding="utf-8").splitlines()[-1] == "other.com"
        assert (root / "products.manifest").is_file()
        assert not list(root.glob(f"{main._STAGING_PREFIX}*"))

    def test_failed_group_keeps_existing_products_untouched(self, work_dir, monkeypatch):
        root = self._setup(work_dir, monkeypatch)
        old = root / "block" / "domain" / "Owner" / "a.txt"
        old.parent.mkdir(parents=True)
        old.write_text("old.example\n", encoding="utf-8")
        stale = root / f"{main._STAGING_PREFIX}stale"
        stale.mkdir()
        (stale / "leftover.txt").write_text("leftover\n", encoding="utf-8")
        groups = [
            self._group(root, "a.txt", 0, "https://example.com/a.txt"),
            self._group(root, "b.txt", 1, "https://example.com/b.txt"),
        ]
        bad = "\n".join([f"DOMAIN-KEYWORD,ads-{i}.example.com" for i in range(25)] + ["good.com"]) + "\n"
        self._prepare(root, monkeypatch, groups, [b"new.example\n", bad.encode()])

        with pytest.raises(SystemExit) as exc:
            main.main()

        assert exc.value.code == 1
        assert old.read_text(encoding="utf-8") == "old.example\n"
        assert not (root / "block/domain/Owner/b.txt").exists()
        assert not list(root.glob(f"{main._STAGING_PREFIX}*"))


class TestCleanOrphans:
    def test_hidden_files_are_left_alone(self, tmp_path, monkeypatch):
        root = tmp_path / "rulesets"
        (root / ".hidden").mkdir(parents=True)
        (root / ".hidden" / "keep.txt").write_text("x\n", encoding="utf-8")
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        monkeypatch.setattr(main, "RULESETS_DIR", root)
        for name in ("group_start", "group_end", "info", "debug"):
            monkeypatch.setattr(main, name, lambda *a, **k: None)

        main.clean_orphans([root / "a.txt"])

        assert (root / "a.txt").exists()
        assert (root / ".hidden" / "keep.txt").exists()
