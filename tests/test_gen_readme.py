import io
import os
import re
import sys

import gen_readme


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestFormatSize:
    def test_zero(self):
        assert gen_readme.format_size(0) == "0 B"

    def test_bytes_below_kib(self):
        assert gen_readme.format_size(1) == "1.00 B"
        assert gen_readme.format_size(1023) == "1023.00 B"

    def test_exact_unit_boundaries(self):
        assert gen_readme.format_size(1024) == "1.00 KB"
        assert gen_readme.format_size(1024 ** 2) == "1.00 MB"
        assert gen_readme.format_size(1024 ** 3) == "1.00 GB"

    def test_fractional(self):
        assert gen_readme.format_size(1536) == "1.50 KB"
        assert gen_readme.format_size(4_529_923) == "4.32 MB"

    def test_does_not_exceed_last_unit(self):
        """超出 GB 时仍以 GB 表示，不越界索引 units。"""
        out = gen_readme.format_size(1024 ** 5)
        assert out.endswith(" GB")


class TestTimeBadge:
    def test_explicit_time_used_verbatim(self):
        url = gen_readme.get_time_badge("2026--10--05%2006%3A12")
        assert "Updated-2026--10--05%2006%3A12-blue" in url
        assert "style=flat-square" in url

    def test_none_falls_back_to_now(self):
        url = gen_readme.get_time_badge(None)
        m = gen_readme.BADGE_TIME_RE.search(url)
        assert m, f"徽章 URL 应能被 BADGE_TIME_RE 解析: {url}"

    def test_badge_regex_roundtrip(self):
        encoded = "2026--10--05%2006%3A12"
        assert gen_readme.BADGE_TIME_RE.search(
            gen_readme.get_time_badge(encoded)).group(1) == encoded

    def test_badge_regex_rejects_wrong_style(self):
        assert gen_readme.BADGE_TIME_RE.search("Updated-2026--10--05%2006%3A12-green") is None


class TestScanFiles:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert gen_readme.scan_files(str(tmp_path / "nope")) == []

    def test_recursive_and_sorted(self, tmp_path):
        write(tmp_path / "b" / "y.txt", "y")
        write(tmp_path / "a" / "x.txt", "x")
        found = gen_readme.scan_files(str(tmp_path))
        assert [os.path.relpath(f, tmp_path).replace(os.sep, "/") for f in found] == [
            "a/x.txt", "b/y.txt"]
        assert found == sorted(found)

    def test_hidden_files_skipped(self, tmp_path):
        write(tmp_path / ".hidden.txt", "h")
        write(tmp_path / "keep.txt", "k")
        found = gen_readme.scan_files(str(tmp_path))
        assert len(found) == 1 and found[0].endswith("keep.txt")

    def test_empty_dir_not_a_file(self, tmp_path):
        (tmp_path / "emptydir").mkdir()
        assert gen_readme.scan_files(str(tmp_path)) == []


class TestCollectStats:
    def test_counts_and_sums(self, tmp_path):
        write(tmp_path / "a.txt", "abc")
        write(tmp_path / "b.txt", "de")
        count, total = gen_readme.collect_stats(
            [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")])
        assert count == 2
        assert total == 5

    def test_empty(self):
        assert gen_readme.collect_stats([]) == (0, 0)


class TestWriteTableRows:
    def render(self, tmp_path, rel, root_name="merged-rules"):
        root = tmp_path / root_name
        target = root / rel
        write(target, "x" * 10)
        buf = io.StringIO()
        gen_readme.write_table_rows(buf, [str(target)], str(root))
        return buf.getvalue()

    def test_row_has_four_columns(self, tmp_path):
        raw = self.render(tmp_path, "block/domain/Owner/a.txt")
        assert raw.endswith("\n")
        row = raw.rstrip("\n")
        assert row.count("|") == 5
        assert row.startswith("| ") and row.endswith(" |")
        assert "\n" not in row

    def test_links_point_at_artifacts_branch_for_all_three_cdn(self, tmp_path):
        root = tmp_path / "merged-rules"
        target = root / "block" / "domain" / "Owner" / "a.txt"
        write(target, "x" * 10)
        buf = io.StringIO()
        gen_readme.write_table_rows(buf, [str(target)], str(root))
        row = buf.getvalue()
        full = "merged-rules/block/domain/Owner/a.txt"
        assert f"{gen_readme.BASE_RAW}/{full}" in row
        assert f"{gen_readme.BASE_JSDELIVR}/{full}" in row
        # BASE_GHPROXY 自身已包含 BASE_RAW，不要再拼一次
        assert f"{gen_readme.BASE_GHPROXY}/{full}" in row
        assert gen_readme.BASE_GHPROXY.endswith(gen_readme.BASE_RAW)
        assert "/artifacts" in gen_readme.BASE_RAW
        assert "@artifacts" in gen_readme.BASE_JSDELIVR
        assert not re.search(r"/(main|master)/", row)

    def test_category_sub_and_filename_shown(self, tmp_path):
        row = self.render(tmp_path, "block/domain/Owner/a.txt")
        assert "<sub>block/domain/Owner</sub>" in row
        assert "<b>a.txt</b>" in row

    def test_root_level_file_labeled(self, tmp_path):
        row = self.render(tmp_path, "flat.txt")
        assert "<sub>Root</sub>" in row

    def test_size_rendered_with_backticks(self, tmp_path):
        row = self.render(tmp_path, "a.txt")
        assert "`10.00 B`" in row


class TestMakeSection:
    def test_emits_header_table_and_details(self, tmp_path):
        write(tmp_path / "o" / "a.txt", "abc")
        buf = io.StringIO()
        count, size = gen_readme.make_section(
            buf, "标题", "说明", gen_readme.scan_files(str(tmp_path)), str(tmp_path))
        out = buf.getvalue()
        assert count == 1 and size == 3
        assert "### 标题" in out and "*说明*" in out
        assert "1 个文件" in out
        assert "<details open>" in out and "</details>" in out
        assert "| 文件名称 | 大小 | CDN 下载 | 源文件 |" in out

    def test_empty_dir_still_renders(self, tmp_path):
        buf = io.StringIO()
        count, size = gen_readme.make_section(buf, "T", "D", [], str(tmp_path))
        assert (count, size) == (0, 0)
        assert "0 个文件" in buf.getvalue()


class TestResolveBadgeTime:
    def test_disabled_change_detection_returns_none(self, tmp_path, monkeypatch=None):
        original = gen_readme.get
        gen_readme.get = lambda *a, **k: False
        try:
            assert gen_readme.resolve_badge_time() is None
        finally:
            gen_readme.get = original

    def test_missing_readme_returns_none(self, tmp_path):
        original_file = gen_readme.README_FILE
        original_hash = gen_readme.combined_products_hash
        original_last = gen_readme.load_last_hash
        gen_readme.README_FILE = str(tmp_path / "absent.md")
        gen_readme.combined_products_hash = lambda: ("h", 1, 1)
        gen_readme.load_last_hash = lambda: "h"
        try:
            assert gen_readme.resolve_badge_time() is None
        finally:
            gen_readme.README_FILE = original_file
            gen_readme.combined_products_hash = original_hash
            gen_readme.load_last_hash = original_last

    def test_unchanged_products_reuse_existing_badge(self, tmp_path):
        readme = tmp_path / "README.md"
        write(readme, "x\n" + gen_readme.get_time_badge("2026--01--02%2003%3A04") + "\n")
        original_file = gen_readme.README_FILE
        original_hash = gen_readme.combined_products_hash
        original_last = gen_readme.load_last_hash
        gen_readme.README_FILE = str(readme)
        gen_readme.combined_products_hash = lambda: ("same", 1, 1)
        gen_readme.load_last_hash = lambda: "same"
        try:
            assert gen_readme.resolve_badge_time() == "2026--01--02%2003%3A04"
        finally:
            gen_readme.README_FILE = original_file
            gen_readme.combined_products_hash = original_hash
            gen_readme.load_last_hash = original_last

    def test_changed_products_start_fresh(self, tmp_path):
        readme = tmp_path / "README.md"
        write(readme, gen_readme.get_time_badge("2026--01--02%2003%3A04") + "\n")
        original_file = gen_readme.README_FILE
        original_hash = gen_readme.combined_products_hash
        original_last = gen_readme.load_last_hash
        gen_readme.README_FILE = str(readme)
        gen_readme.combined_products_hash = lambda: ("new", 1, 1)
        gen_readme.load_last_hash = lambda: "old"
        try:
            assert gen_readme.resolve_badge_time() is None
        finally:
            gen_readme.README_FILE = original_file
            gen_readme.combined_products_hash = original_hash
            gen_readme.load_last_hash = original_last

    def test_hash_error_returns_none(self, tmp_path):
        def boom():
            raise OSError("no products")

        original_hash = gen_readme.combined_products_hash
        gen_readme.combined_products_hash = boom
        try:
            assert gen_readme.resolve_badge_time() is None
        finally:
            gen_readme.combined_products_hash = original_hash


class TestStaticSections:
    def test_documents_all_four_syntax_forms(self):
        text = gen_readme.make_static_sections()
        for token in ("`+.example.com`", "`.example.com`", "`*.example.com`",
                      "`*.*.example.com`"):
            assert token in text, f"语义表缺少 {token}"
        assert "仅子域" in text or "不含 apex" in text

    def test_documents_artifacts_branch(self):
        text = gen_readme.make_static_sections()
        assert "artifacts" in text
        assert "不进入 git 历史" in text

    def test_keeps_priority_and_release_semantics(self):
        text = gen_readme.make_static_sections()
        assert "block > direct > policy" in text
        assert "conflict_policy" in text


class TestMakePageHeader:
    def test_contains_short_repo_name_and_badge(self):
        html = gen_readme.make_page_header("2026--01--02%2003%3A04")
        short = gen_readme.REPO_NAME.split("/")[-1]
        assert f"<h1>{short}</h1>" in html
        assert f"github.com/{gen_readme.REPO_NAME}" in html
        assert "Updated-2026--01--02%2003%3A04-blue" in html
        assert html.startswith('<div align="center">')

    def test_now_badge_when_no_time(self):
        html = gen_readme.make_page_header(None)
        assert gen_readme.BADGE_TIME_RE.search(html)

    def test_all_links_point_at_artifacts_branch(self):
        """页头里的徽章与链接不得回退到 main/master。"""
        html = gen_readme.make_page_header("2026--01--02%2003%3A04")
        assert gen_readme.ARTIFACTS_BRANCH == "artifacts"
        assert not re.search(r"/(main|master)(/|\?|\"|$)", html)
        assert f"github.com/{gen_readme.REPO_NAME}" in html


class TestMain:
    def run_main(self, tmp_path):
        original = (gen_readme.DIR_RULES, gen_readme.DIR_MRS, gen_readme.README_FILE)
        gen_readme.DIR_RULES = str(tmp_path / "merged-rules")
        gen_readme.DIR_MRS = str(tmp_path / "merged-rules-mrs")
        gen_readme.README_FILE = str(tmp_path / "README.md")
        write(tmp_path / "merged-rules" / "block" / "A" / "a.txt", "a.com\n")
        write(tmp_path / "merged-rules-mrs" / "block" / "A" / "a.mrs", "MRS!")
        try:
            gen_readme.main()
        finally:
            gen_readme.DIR_RULES, gen_readme.DIR_MRS, gen_readme.README_FILE = original
        return (tmp_path / "README.md").read_text(encoding="utf-8")

    def test_writes_readme_with_both_sections(self, tmp_path):
        out = self.run_main(tmp_path)
        assert "## 规则列表" in out
        assert "### 基础规则集合" in out
        assert "### Mihomo 专用集合" in out
        assert "a.txt" in out and "a.mrs" in out

    def test_readme_links_use_artifacts_branch(self, tmp_path):
        out = self.run_main(tmp_path)
        assert "/artifacts/merged-rules/block/A/a.txt" in out
        assert "/artifacts/merged-rules-mrs/block/A/a.mrs" in out

    def test_no_tmp_or_partial_file_left(self, tmp_path):
        self.run_main(tmp_path)
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_step_summary_appended_when_env_set(self, tmp_path):
        summary = tmp_path / "summary.md"
        original_env = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        try:
            self.run_main(tmp_path)
        finally:
            if original_env is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = original_env
        text = summary.read_text(encoding="utf-8")
        assert "README 生成报告" in text
        assert "标准规则" in text and "MRS 规则" in text
        assert "**总计**" in text


class TestOutputSemantics:
    def test_bare_and_prefixed_forms_stay_distinct_in_readme_table(self, tmp_path):
        """README 表格只是链接与大小，不参与语义处理；确认静态说明与产物写法一致。"""
        rules = tmp_path / "merged-rules"
        write(rules / "block" / "A" / "x.txt",
              "+.a.com\n.a.com\na.com\n*.a.com\n")
        files = gen_readme.scan_files(str(rules))
        buf = io.StringIO()
        gen_readme.write_table_rows(buf, files, str(rules))
        assert "x.txt" in buf.getvalue()
        static = gen_readme.make_static_sections()
        assert re.search(r"`\+\.example\.com`", static)
        assert re.search(r"`\.example\.com`", static)
