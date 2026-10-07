import os

import main


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

    def test_subdomain_of_trusted_is_trusted(self):
        assert main.is_trusted_host("https://api.github.com/x") is True

    def test_missing_or_invalid_host(self):
        assert main.is_trusted_host("") is False
        assert main.is_trusted_host("not a url") is False
        assert main.is_trusted_host("https:///nohost") is False

    def test_case_insensitive(self):
        assert main.is_trusted_host("https://GitHub.COM/a/b") is True


class TestProductPathStability:

    def test_displayable_chars_survive(self):
        task = {
            "url": "https://github.com/MetaCubeX/meta-rules-dat/raw/refs/heads/meta/"
                   "geo/geosite/category-ai-!cn.list",
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


class TestAuthHeaders:
    def with_token(self, value="tok"):
        original = os.environ.get("GH_TOKEN")
        os.environ["GH_TOKEN"] = value
        return original

    def restore(self, original):
        if original is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = original

    def test_token_sent_to_trusted_host(self):
        original = self.with_token()
        try:
            assert main.auth_headers("https://raw.githubusercontent.com/a/b/x") == {
                "Authorization": "Bearer tok"}
        finally:
            self.restore(original)

    def test_token_withheld_from_untrusted_host(self):
        original = self.with_token()
        try:
            assert main.auth_headers("https://example.com/x") is None
            assert main.auth_headers("https://ghproxy.net/https://raw.githubusercontent.com/a/b") is None
        finally:
            self.restore(original)

    def test_token_withheld_from_plaintext_and_user_content(self):
        original = self.with_token()
        try:
            assert main.auth_headers("http://raw.githubusercontent.com/a/b/x") is None
            assert main.auth_headers("https://someone.github.io/x") is None
        finally:
            self.restore(original)

    def test_no_token_means_no_header(self):
        original = os.environ.pop("GH_TOKEN", None)
        github_token = os.environ.pop("GITHUB_TOKEN", None)
        try:
            assert main.auth_headers("https://github.com/a/b") is None
        finally:
            if original is not None:
                os.environ["GH_TOKEN"] = original
            if github_token is not None:
                os.environ["GITHUB_TOKEN"] = github_token

    def test_github_token_used_as_fallback(self):
        gh = os.environ.pop("GH_TOKEN", None)
        prev = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "fallback"
        try:
            assert main.auth_headers("https://github.com/a/b") == {
                "Authorization": "Bearer fallback"}
        finally:
            if gh is not None:
                os.environ["GH_TOKEN"] = gh
            if prev is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = prev


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
        assert main.source_repo_slug(
            "https://raw.githubusercontent.com/a/b/main/x.txt") == "a__b"

    def test_codeload(self):
        assert main.source_repo_slug("https://codeload.github.com/a/b/tar.gz/main") == "a__b"

    def test_github_io_uses_first_two_path_segments(self):
        assert main.source_repo_slug("https://user.github.io/a/b/x.txt") == "a__b"
        assert main.source_repo_slug("https://user.github.io/repo/x.txt") == "repo__x.txt"

    def test_github_io_bare_host(self):
        assert main.source_repo_slug("https://github.io/x") == ""

    def test_jsdelivr_gh_form(self):
        assert main.source_repo_slug(
            "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/x.list"
        ) == "MetaCubeX__meta-rules-dat"

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
        for url in ["https://example.com/x", "https://mirror.example.org/a/b",
                    "https://ghproxy.net/https://raw.githubusercontent.com/a/b"]:
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


class TestCollidingOutputPaths:
    def task(self, url, policy="policy", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def posix_plan(self, plan):
        return {main.Path(k).as_posix(): v for k, v in plan.items()}

    def test_same_owner_path_same_repo_not_flagged(self):
        plan = main.colliding_output_paths([
            self.task("https://github.com/Loyalsoldier/x/raw/r/gfw.txt"),
            self.task("https://github.com/Loyalsoldier/x/raw/r2/gfw.txt"),
        ])
        assert plan == {}

    def test_different_repos_same_owner_and_filename_flagged(self):
        plan = self.posix_plan(main.colliding_output_paths([
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
            self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt"),
        ]))
        assert list(plan) == ["policy/domain/Loyalsoldier/gfw.txt"]
        assert plan["policy/domain/Loyalsoldier/gfw.txt"] == [
            "Loyalsoldier__clash-rules", "Loyalsoldier__v2ray-rules-dat"]

    def test_mirror_host_spoofing_flagged(self):
        plan = self.posix_plan(main.colliding_output_paths([
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
            self.task("https://raw.githubusercontent.com/Loyalsoldier/other/main/gfw.txt"),
        ]))
        assert "policy/domain/Loyalsoldier/gfw.txt" in plan

    def test_same_url_repeated_not_flagged(self):
        t = self.task("https://github.com/o/r/raw/m/gfw.txt")
        assert main.colliding_output_paths([t, dict(t)]) == {}

    def test_different_policy_not_colliding(self):
        plan = main.colliding_output_paths([
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt", policy="block"),
            self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt", policy="policy"),
        ])
        assert plan == {}


class TestBuildFilepath:
    def task(self, url, policy="policy", type_="domain"):
        return {"url": url, "policy": policy, "type": type_}

    def test_default_owner_used_when_no_collision(self):
        owner, name, rel, abs_path = main.build_filepath(
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/reject.txt"))
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
        main.build_filepath(
            self.task("https://github.com/o/r/raw/m/x.txt"), None, taken)
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

    def test_same_url_twice_merges_into_one_group(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        groups = main.plan_groups([self.task(url), self.task(url)])
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 2

    def test_same_owner_different_repos_split_to_avoid_overwrite(self):
        groups = main.plan_groups([
            self.task("https://github.com/o/r/raw/m/x.txt"),
            self.task("https://raw.githubusercontent.com/o/r2/main/x.txt"),
        ])
        assert len(groups) == 2
        paths = sorted(str(g["path"]).replace("\\", "/") for g in groups)
        assert paths == ["rulesets/policy/domain/o__r/x.txt",
                         "rulesets/policy/domain/o__r2/x.txt"]

    def test_collision_splits_into_separate_groups(self):
        groups = main.plan_groups([
            self.task("https://github.com/Loyalsoldier/clash-rules/raw/r/gfw.txt"),
            self.task("https://github.com/Loyalsoldier/v2ray-rules-dat/raw/r/gfw.txt"),
        ])
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
        groups = main.plan_groups([
            self.task("https://github.com/b/b/raw/m/x.txt"),
            self.task("https://github.com/a/a/raw/m/x.txt"),
        ])
        paths = [str(g["path"]).replace("\\", "/") for g in groups]
        assert paths == sorted(paths)

    def test_member_indices_reference_input(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        tasks = [self.task(url), self.task(url)]
        groups = main.plan_groups(tasks)
        indices = [i for i, _t in groups[0]["members"]]
        assert indices == [0, 1]
        for i, t in groups[0]["members"]:
            assert t is tasks[i]

    def test_all_sources_recorded(self):
        url = "https://github.com/o/r/raw/m/x.txt"
        groups = main.plan_groups([self.task(url), self.task(url)])
        assert groups[0]["sources"] == [url, url]

    def test_empty_input(self):
        assert main.plan_groups([]) == []


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
        assert tasks == [{"policy": "policy", "type": "domain", "domain_kind": "exact",
                          "url": "https://github.com/a/b/raw/m/x.txt"}]

    def test_policy_and_type_markers_apply_to_following_lines(self, tmp_path):
        tasks = self.load(tmp_path, "\n".join([
            "[policy:block]",
            "[type:domain]",
            "https://github.com/a/b/raw/m/a.txt",
            "[policy:direct]",
            "https://github.com/a/b/raw/m/b.txt",
            "[policy:reject]",
            "https://github.com/a/b/raw/m/c.txt",
        ]) + "\n")
        assert [t["policy"] for t in tasks] == ["block", "direct", "block"]
        assert [t["url"].rsplit("/", 1)[1] for t in tasks] == ["a.txt", "b.txt", "c.txt"]

    def test_type_marker_switches_to_ipcidr(self, tmp_path):
        tasks = self.load(tmp_path, "\n".join([
            "[policy:direct]", "[type:ipcidr]",
            "https://github.com/a/b/raw/m/cn.txt",
            "[type:domain]",
            "https://github.com/a/b/raw/m/domain.txt",
        ]) + "\n")
        assert [t["type"] for t in tasks] == ["ipcidr", "domain"]

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        tasks = self.load(tmp_path, "\n".join([
            "# comment",
            "",
            "   ",
            "https://github.com/a/b/raw/m/x.txt",
        ]) + "\n")
        assert len(tasks) == 1

    def test_trailing_comment_after_url_ignored(self, tmp_path):
        tasks = self.load(tmp_path, "https://github.com/a/b/raw/m/x.txt # 说明\n")
        assert tasks[0]["url"] == "https://github.com/a/b/raw/m/x.txt"

    def test_bom_stripped(self, tmp_path):
        tasks = self.load(tmp_path, "\ufeffhttps://github.com/a/b/raw/m/x.txt\n")
        assert len(tasks) == 1
        assert tasks[0]["url"].startswith("https://")

    def test_line_without_url_skipped(self, tmp_path):
        tasks = self.load(tmp_path, "[policy:block]\n不是链接\nhttps://github.com/a/b/raw/m/x.txt\n")
        assert len(tasks) == 1

    def test_marker_regex_anchored(self, tmp_path):
        tasks = self.load(tmp_path, "前缀 [policy:block] 后缀\nhttps://github.com/a/b/raw/m/x.txt\n")
        assert tasks[0]["policy"] == "policy"

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

    def test_real_sources_file_is_parseable(self):
        tasks = main.parse_sources()
        assert len(tasks) > 5
        for t in tasks:
            assert t["policy"] in ("block", "direct", "policy")
            assert t["type"] in ("domain", "ipcidr")
            assert t["url"].startswith("http")


class TestProcessIpGroup:

    def test_default_route_drop_is_reported(self):
        result, stats = main._process_ip_group(["0.0.0.0/0", "1.0.0.0/24"])

        assert result == ["1.0.0.0/24"]
        assert stats["dropped_default_route"] == 1


