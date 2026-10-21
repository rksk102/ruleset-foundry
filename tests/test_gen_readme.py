import io
import os
import re

import gen_readme
import pytest


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
        assert gen_readme.format_size(1024**2) == "1.00 MB"
        assert gen_readme.format_size(1024**3) == "1.00 GB"

    def test_fractional(self):
        assert gen_readme.format_size(1536) == "1.50 KB"
        assert gen_readme.format_size(4_529_923) == "4.32 MB"

    def test_does_not_exceed_last_unit(self):
        out = gen_readme.format_size(1024**5)
        assert out.endswith(" GB")


class TestTimeBadge:
    def test_explicit_time_used_verbatim(self):
        url = gen_readme.get_time_badge("2026--10--05%2006%3A12")
        assert f"{gen_readme.BADGE_LABEL}-2026--10--05%2006%3A12-blue" in url
        assert "style=flat-square" in url

    def test_none_falls_back_to_now(self):
        url = gen_readme.get_time_badge(None)
        m = gen_readme.BADGE_TIME_RE.search(url)
        assert m, f"徽章 URL 应能被 BADGE_TIME_RE 解析: {url}"

    def test_badge_regex_roundtrip(self):
        encoded = "2026--10--05%2006%3A12"
        assert gen_readme.BADGE_TIME_RE.search(gen_readme.get_time_badge(encoded)).group(1) == encoded

    def test_badge_regex_rejects_wrong_style(self):
        assert gen_readme.BADGE_TIME_RE.search("Updated-2026--10--05%2006%3A12-green") is None


class TestScanFiles:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert gen_readme.scan_files(str(tmp_path / "nope"), ".txt") == []

    def test_recursive_and_sorted(self, tmp_path):
        write(tmp_path / "b" / "y.txt", "y")
        write(tmp_path / "a" / "x.txt", "x")
        found = gen_readme.scan_files(str(tmp_path), ".txt")
        assert [os.path.relpath(f, tmp_path).replace(os.sep, "/") for f in found] == ["a/x.txt", "b/y.txt"]

    def test_sort_key_is_posix_relative_path(self, tmp_path):
        # "a/b.txt" 与 "a0.txt" 在 Windows（\>0）与 Linux（/<0）下默认排序相反；POSIX 键保证一致
        write(tmp_path / "a0.txt", "x")
        write(tmp_path / "a" / "b.txt", "y")
        found = gen_readme.scan_files(str(tmp_path), ".txt")
        assert [os.path.relpath(f, tmp_path).replace(os.sep, "/") for f in found] == ["a/b.txt", "a0.txt"]

    def test_hidden_dirs_skipped(self, tmp_path):
        write(tmp_path / ".hidden" / "keep.txt", "h")
        write(tmp_path / "keep.txt", "k")
        found = gen_readme.scan_files(str(tmp_path), ".txt")
        assert [os.path.basename(f) for f in found] == ["keep.txt"]

    def test_walk_error_propagates(self, tmp_path, monkeypatch):
        def fake_walk(*_a, **kwargs):
            kwargs["onerror"](PermissionError("denied"))
            return
            yield  # pragma: no cover - 使其成为生成器函数

        monkeypatch.setattr(gen_readme.os, "walk", fake_walk)
        with pytest.raises(PermissionError):
            gen_readme.scan_files(str(tmp_path), ".txt")

    def test_hidden_files_skipped(self, tmp_path):
        write(tmp_path / ".hidden.txt", "h")
        write(tmp_path / "keep.txt", "k")
        found = gen_readme.scan_files(str(tmp_path), ".txt")
        assert len(found) == 1 and found[0].endswith("keep.txt")

    def test_empty_dir_not_a_file(self, tmp_path):
        (tmp_path / "emptydir").mkdir()
        assert gen_readme.scan_files(str(tmp_path), ".txt") == []

    def test_other_suffixes_filtered(self, tmp_path):
        write(tmp_path / "keep.txt", "k")
        write(tmp_path / "notes.md", "n")
        write(tmp_path / "data.mrs", "m")
        found = gen_readme.scan_files(str(tmp_path), ".txt")
        assert [os.path.basename(f) for f in found] == ["keep.txt"]


class TestCollectStats:
    def test_counts_and_sums(self, tmp_path):
        write(tmp_path / "a.txt", "abc")
        write(tmp_path / "b.txt", "de")
        count, total = gen_readme.collect_stats([str(tmp_path / "a.txt"), str(tmp_path / "b.txt")])
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

    def test_links_point_at_artifacts_branch_for_raw_and_jsdelivr(self, tmp_path):
        root = tmp_path / "merged-rules"
        target = root / "block" / "domain" / "Owner" / "a.txt"
        write(target, "x" * 10)
        buf = io.StringIO()
        gen_readme.write_table_rows(buf, [str(target)], str(root))
        row = buf.getvalue()
        full = "merged-rules/block/domain/Owner/a.txt"
        assert f"{gen_readme.BASE_RAW}/{full}" in row
        assert f"{gen_readme.BASE_JSDELIVR}/{full}" in row
        assert "ghproxy" not in row.lower()
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

    def test_over_jsdelivr_limit_falls_back_to_source_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_readme, "JSDELIVR_MAX_BYTES", 5)
        row = self.render(tmp_path, "block/domain/Owner/big.txt")
        assert gen_readme.BASE_JSDELIVR not in row
        assert f"{gen_readme.BASE_RAW}/merged-rules/block/domain/Owner/big.txt" in row
        assert "over20MB" in row

    def test_under_jsdelivr_limit_keeps_download_badge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_readme, "JSDELIVR_MAX_BYTES", 100)
        row = self.render(tmp_path, "block/domain/Owner/a.txt")
        assert gen_readme.BASE_JSDELIVR in row

    def test_alt_contains_filename(self, tmp_path):
        row = self.render(tmp_path, "block/domain/Owner/a.txt")
        assert 'alt="Download a.txt"' in row
        assert 'alt="GitHub raw a.txt"' in row

    def test_ampersand_escaped_in_text_and_url(self, tmp_path):
        row = self.render(tmp_path, "block/domain/Owner/a&copy;.txt")
        assert "<b>a&amp;copy;.txt</b>" in row
        assert "a%26copy%3B.txt" in row  # & 与 ; 在 URL path 中均编码
        assert "a&copy;.txt" not in row  # 文本与属性中均不得出现未转义形式

    def test_bang_preserved_in_url(self, tmp_path):
        row = self.render(tmp_path, "policy/domain/MetaCubeX/category-ai-!cn.txt")
        assert "category-ai-!cn.txt" in row
        assert "%21" not in row


class TestMakeSection:
    def test_emits_header_table_and_details(self, tmp_path):
        write(tmp_path / "o" / "a.txt", "abc")
        buf = io.StringIO()
        count, size = gen_readme.make_section(
            buf, "标题", "说明", gen_readme.scan_files(str(tmp_path), ".txt"), str(tmp_path)
        )
        out = buf.getvalue()
        assert count == 1 and size == 3
        assert "### 标题" in out and "*说明*" in out
        assert "1 个文件" in out
        assert "<details>" in out and "</details>" in out
        assert "<details open>" not in out
        assert "| 文件名称 | 大小 | 下载 | GitHub raw |" in out

    def test_empty_dir_still_renders(self, tmp_path):
        buf = io.StringIO()
        count, size = gen_readme.make_section(buf, "T", "D", [], str(tmp_path))
        assert (count, size) == (0, 0)
        assert "0 个文件" in buf.getvalue()


class TestResolveBadgeTime:
    def test_disabled_change_detection_returns_none(self, monkeypatch):
        monkeypatch.setattr(gen_readme, "get", lambda *a, **k: False)
        assert gen_readme.resolve_badge_time() is None

    def test_missing_readme_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_readme, "README_FILE", str(tmp_path / "absent.md"))
        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", lambda: "h")
        assert gen_readme.resolve_badge_time() is None

    def test_unchanged_products_reuse_existing_badge(self, tmp_path, monkeypatch):
        readme = tmp_path / "README.md"
        write(readme, "x\n" + gen_readme.get_time_badge("2026--01--02%2003%3A04") + "\n")
        monkeypatch.setattr(gen_readme, "README_FILE", str(readme))
        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("same", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", lambda: "same")
        assert gen_readme.resolve_badge_time() == "2026--01--02%2003%3A04"

    def test_changed_products_start_fresh(self, tmp_path, monkeypatch):
        readme = tmp_path / "README.md"
        write(readme, gen_readme.get_time_badge("2026--01--02%2003%3A04") + "\n")
        monkeypatch.setattr(gen_readme, "README_FILE", str(readme))
        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("new", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", lambda: "old")
        assert gen_readme.resolve_badge_time() is None

    def test_hash_error_returns_none(self, monkeypatch):
        def boom():
            raise OSError("no products")

        monkeypatch.setattr(gen_readme, "combined_products_hash", boom)
        assert gen_readme.resolve_badge_time() is None

    def test_last_hash_read_error_returns_none(self, monkeypatch):
        def boom():
            raise OSError("state 不可读")

        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", boom)
        assert gen_readme.resolve_badge_time() is None

    def test_last_hash_decode_error_returns_none(self, monkeypatch):
        def boom():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", boom)
        assert gen_readme.resolve_badge_time() is None

    def test_legacy_updated_badge_time_inherited(self, tmp_path, monkeypatch):
        """旧版 Updated-...-blue 徽章格式的时间应被 legacy 正则继承。"""
        readme = tmp_path / "README.md"
        write(readme, "x\nhttps://img.shields.io/badge/Updated-2026--01--02%2003%3A04-blue?style=flat-square&logo=github\n")
        monkeypatch.setattr(gen_readme, "README_FILE", str(readme))
        monkeypatch.setattr(gen_readme, "combined_products_hash", lambda *a, **k: ("same", 1, 1))
        monkeypatch.setattr(gen_readme, "load_last_hash", lambda: "same")
        assert gen_readme.resolve_badge_time() == "2026--01--02%2003%3A04"


class TestStaticSections:
    def test_keeps_semantics_table(self):
        text = gen_readme.make_static_sections()
        for token in ("`+.example.com`", "`.example.com`", "`*.example.com`", "`*.*.example.com`"):
            assert token in text, f"语义表缺少 {token}"
        assert "block > direct > policy" in text

    def test_discloses_force_push_and_release_integrity(self):
        text = gen_readme.make_static_sections()
        assert "强制推送" in text
        assert "Releases" in text and "SHA256SUMS" in text
        assert "SECURITY.md" in text

    def test_never_mentions_ghproxy(self):
        text = gen_readme.make_static_sections()
        assert "ghproxy" not in text.lower()


class TestIntro:
    def test_mrs_recommended_with_complete_yaml(self):
        text = gen_readme.make_intro()
        assert "format: mrs" in text
        assert "rules:" in text
        assert "- RULE-SET,block,REJECT" in text
        assert "merged-rules-mrs/" in text

    def test_jsdelivr_cache_disclosed(self):
        assert "12 小时" in gen_readme.make_intro()

    def test_config_example_collapsed_by_default(self):
        assert "<details open>" not in gen_readme.make_intro()


class TestMakePageHeader:
    def test_contains_short_repo_name_and_badge(self):
        html = gen_readme.make_page_header("2026--01--02%2003%3A04")
        short = gen_readme.REPO_NAME.split("/")[-1]
        assert f"<h1>{short}</h1>" in html
        assert f"github.com/{gen_readme.REPO_NAME}" in html
        assert f"{gen_readme.BADGE_LABEL}-2026--01--02%2003%3A04-blue" in html
        assert html.startswith('<div align="center">')

    def test_build_badge_has_no_hardcoded_status_color(self):
        html = gen_readme.make_page_header(None)
        assert "color=2ea44f" not in html
        assert 'alt="Build status"' in html

    def test_no_repo_size_badge_and_updated_links_to_releases(self):
        html = gen_readme.make_page_header(None)
        assert "repo-size" not in html
        assert 'href="#"' not in html
        assert f"github.com/{gen_readme.REPO_NAME}/releases" in html

    def test_now_badge_when_no_time(self):
        html = gen_readme.make_page_header(None)
        assert gen_readme.BADGE_TIME_RE.search(html)


class TestMain:
    def run_main(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setattr(gen_readme, "DIR_RULES", str(tmp_path / "merged-rules"))
        monkeypatch.setattr(gen_readme, "DIR_MRS", str(tmp_path / "merged-rules-mrs"))
        monkeypatch.setattr(gen_readme, "README_FILE", str(tmp_path / "README.md"))
        write(tmp_path / "merged-rules" / "block" / "A" / "a.txt", "a.com\n")
        write(tmp_path / "merged-rules-mrs" / "block" / "A" / "a.mrs", "MRS!")
        gen_readme.main([])
        return (tmp_path / "README.md").read_text(encoding="utf-8")

    def test_writes_readme_with_both_sections(self, tmp_path, monkeypatch):
        out = self.run_main(tmp_path, monkeypatch)
        assert "## 产物清单" in out
        assert "### 基础规则集合（.txt）" in out
        assert "### Mihomo 专用集合（.mrs）" in out
        assert "a.txt" in out and "a.mrs" in out
        assert "ghproxy" not in out.lower()

    def test_readme_links_use_artifacts_branch(self, tmp_path, monkeypatch):
        out = self.run_main(tmp_path, monkeypatch)
        assert "/artifacts/merged-rules/block/A/a.txt" in out
        assert "/artifacts/merged-rules-mrs/block/A/a.mrs" in out

    def test_no_tmp_or_partial_file_left(self, tmp_path, monkeypatch):
        self.run_main(tmp_path, monkeypatch)
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_step_summary_appended_when_env_set(self, tmp_path, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        self.run_main(tmp_path, monkeypatch)
        text = summary.read_text(encoding="utf-8")
        assert "README 生成报告" in text
        assert "标准规则" in text and "MRS 规则" in text
        assert "**总计**" in text


class TestPreflight:
    def isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_readme, "DIR_RULES", str(tmp_path / "merged-rules"))
        monkeypatch.setattr(gen_readme, "DIR_MRS", str(tmp_path / "merged-rules-mrs"))
        monkeypatch.setattr(gen_readme, "README_FILE", str(tmp_path / "README.md"))

    def test_missing_repo_env_refuses_write(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        self.isolate(tmp_path, monkeypatch)
        write(tmp_path / "merged-rules" / "a.txt", "a.com\n")
        write(tmp_path / "merged-rules-mrs" / "a.mrs", "MRS!")
        with pytest.raises(SystemExit) as exc:
            gen_readme.main([])
        assert exc.value.code != 0
        assert not (tmp_path / "README.md").exists()

    def test_missing_dirs_refuse_overwrite(self, tmp_path, monkeypatch):
        readme = tmp_path / "README.md"
        readme.write_text("旧内容\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        self.isolate(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            gen_readme.main([])
        assert exc.value.code != 0
        assert readme.read_text(encoding="utf-8") == "旧内容\n"

    def test_allow_flags_permit_placeholder_preview(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        self.isolate(tmp_path, monkeypatch)
        gen_readme.main(["--allow-placeholder", "--allow-empty"])
        assert (tmp_path / "README.md").exists()
