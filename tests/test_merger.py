import tempfile

import merger
import pytest


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestDetectMode:
    def test_ipcidr(self):
        assert merger.detect_mode("ipcidr") == "IP-CIDR"
        assert merger.detect_mode("IpCidr") == "IP-CIDR"

    def test_domain(self):
        assert merger.detect_mode("domain") == "DOMAIN"
        assert merger.detect_mode("General") == "DOMAIN"

    def test_hyphenated_form_falls_back_to_domain(self):
        assert merger.detect_mode("IP-CIDR") == "DOMAIN"


class TestResolveConflictAction:
    def test_no_conflicts_returns_none(self):
        assert merger.resolve_conflict_action("warn", False) == "none"
        assert merger.resolve_conflict_action("fail", False) == "none"

    def test_policies_passthrough(self):
        assert merger.resolve_conflict_action("ignore", True) == "ignore"
        assert merger.resolve_conflict_action("warn", True) == "warn"
        assert merger.resolve_conflict_action("fail", True) == "fail"

    def test_default_is_warn(self):
        assert merger.resolve_conflict_action(None, True) == "warn"

    def test_invalid_policy_raises(self):
        try:
            merger.resolve_conflict_action("explode", True)
        except ValueError:
            return
        raise AssertionError("非法取值应抛出 ValueError")


class TestAutoDiscoverFiles:
    def test_three_level_structure_discovered(self, tmp_path):
        write(tmp_path / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        tasks = merger.auto_discover_files(str(tmp_path))
        assert len(tasks) == 1
        assert tasks[0]["strategy"] == "block"
        assert tasks[0]["type"] == "domain"
        assert tasks[0]["owner"] == "OwnerA"
        assert tasks[0]["filename"] == "ads.txt"
        assert tasks[0]["inputs"] == ["block/domain/OwnerA/ads.txt"]

    def test_shallow_and_non_txt_skipped(self, tmp_path):
        write(tmp_path / "sync-summary.txt", "x\n")
        write(tmp_path / "block" / "domain" / "note.md", "x\n")
        assert merger.auto_discover_files(str(tmp_path)) == []

    def test_hidden_file_skipped(self, tmp_path):
        write(tmp_path / "block" / "domain" / "OwnerA" / ".hidden.txt", "x\n")
        assert merger.auto_discover_files(str(tmp_path)) == []

    def test_missing_dir_returns_empty(self, tmp_path):
        assert merger.auto_discover_files(str(tmp_path / "nope")) == []


class TestProcessTaskLogic:
    def use_dirs(self, source, output, fn):
        original = (merger.SOURCE_DIR, merger.OUTPUT_DIR)
        merger.SOURCE_DIR, merger.OUTPUT_DIR = str(source), str(output)
        try:
            return fn()
        finally:
            merger.SOURCE_DIR, merger.OUTPUT_DIR = original

    def test_merges_and_reports_counts(self, tmp_path):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\ngoogle.com\n")
        write(source / "block" / "domain" / "B" / "two.txt", "google.com\nsub.google.com\n")

        result = self.use_dirs(source, output, lambda: merger.process_task_logic(
            "block", "domain", "Owner", "all.txt",
            ["block/domain/A/one.txt", "block/domain/B/two.txt"], "合并测试",
        ))

        assert result["raw"] == 3
        assert result["opt"] == 3
        assert result["path"] == "block/domain/Owner"
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert "ads.example.com" in content
        assert "google.com" in content
        assert "sub.google.com" in content

    def test_suffix_parent_dedups_children(self, tmp_path):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "one.txt", "+.google.com\n+.ads.google.com\n")

        result = self.use_dirs(source, output, lambda: merger.process_task_logic(
            "block", "domain", "Owner", "all.txt",
            ["block/domain/A/one.txt"], "后缀去重测试",
        ))

        assert result["raw"] == 2
        assert result["opt"] == 1
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert "+.google.com" in content
        assert "+.ads.google.com" not in content

    def test_sources_recorded_in_header(self, tmp_path):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(
            source / "block" / "domain" / "A" / "one.txt",
            "# Source: https://raw.githubusercontent.com/Owner/repo/main/one.txt\nads.example.com\n",
        )
        self.use_dirs(source, output, lambda: merger.process_task_logic(
            "block", "domain", "Owner", "all.txt",
            ["block/domain/A/one.txt"], "来源透传测试",
        ))
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert "# Sources: https://raw.githubusercontent.com/Owner/repo/main/one.txt" in content

    def test_missing_input_raises(self, tmp_path):
        source = tmp_path / "rulesets"
        source.mkdir(parents=True)
        try:
            self.use_dirs(source, tmp_path / "merged", lambda: merger.process_task_logic(
                "block", "domain", "Owner", "all.txt", ["block/domain/A/missing.txt"], "x",
            ))
        except FileNotFoundError:
            return
        raise AssertionError("缺失输入应抛出 FileNotFoundError")

    def test_input_outside_source_dir_rejected(self, tmp_path):
        source = tmp_path / "rulesets"
        source.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        write(outside, "ads.example.com\n")

        def run():
            merger.process_task_logic("block", "domain", "Owner", "all.txt",
                                      [str(outside)], "x")

        with pytest.raises(ValueError):
            self.use_dirs(source, tmp_path / "merged", run)

        def run_relative():
            merger.process_task_logic("block", "domain", "Owner", "all.txt",
                                      ["../outside.txt"], "x")

        with pytest.raises(ValueError):
            self.use_dirs(source, tmp_path / "merged", run_relative)

    def test_output_traversal_rejected_without_writing(self, tmp_path):
        source = tmp_path / "rulesets"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\n")
        output = tmp_path / "merged"
        output.mkdir(parents=True)
        escaped = (output / ".." / ".." / ".." / "domain" / "PWNED.txt").resolve()

        def run():
            merger.process_task_logic("../../..", "domain", "Owner", "../PWNED.txt",
                                      ["block/domain/A/one.txt"], "x")

        with pytest.raises(ValueError):
            self.use_dirs(source, output, run)
        assert not escaped.exists()

    def test_filename_traversal_rejected_without_writing(self, tmp_path):
        source = tmp_path / "rulesets"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\n")
        output = tmp_path / "merged"
        owner_dir = output / "block" / "domain" / "Owner"
        escaped = (owner_dir / ".." / "escape.txt").resolve()

        def run():
            merger.process_task_logic("block", "domain", "Owner", "../escape.txt",
                                      ["block/domain/A/one.txt"], "x")

        with pytest.raises(ValueError):
            self.use_dirs(source, output, run)
        assert not escaped.exists()

    def test_ip_task_drops_default_route(self, tmp_path):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "direct" / "ipcidr" / "A" / "cn.txt", "0.0.0.0/0\n1.0.0.0/24\n1.0.1.0/24\n")
        result = self.use_dirs(source, output, lambda: merger.process_task_logic(
            "direct", "ipcidr", "Owner", "all-cn.txt", ["direct/ipcidr/A/cn.txt"], "x",
        ))
        assert result["opt"] == 1
        content = (output / "direct" / "ipcidr" / "Owner" / "all-cn.txt").read_text(encoding="utf-8")
        assert "1.0.0.0/23" in content
        assert "0.0.0.0/0" not in content


class TestDetectCrossPolicyConflicts:
    def build(self, tmp_path, files):
        for rel, text in files.items():
            write(tmp_path / rel, text)
        return merger.detect_cross_policy_conflicts(str(tmp_path))

    def test_explicit_conflict_same_domain_two_strategies(self, tmp_path):
        explicit, _implicit = self.build(tmp_path, {
            "block/domain/A/x.txt": "same.example.com\n",
            "policy/domain/A/y.txt": "same.example.com\n",
        })
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_explicit_conflict_across_prefix_forms(self, tmp_path):
        explicit, _implicit = self.build(tmp_path, {
            "block/domain/A/x.txt": "+.same.example.com\n",
            "policy/domain/A/y.txt": "same.example.com\n",
        })
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_implicit_conflict_suffix_parent_covers_child(self, tmp_path):
        _explicit, implicit = self.build(tmp_path, {
            "policy/domain/A/proxy.txt": "+.google.com\n",
            "block/domain/A/ads.txt": "ads.google.com\n",
        })
        assert implicit == {"policy(父) → block(子)": [("ads.google.com", "google.com")]}

    def test_implicit_conflict_suffix_parent_covers_subdomain_child(self, tmp_path):
        _explicit, implicit = self.build(tmp_path, {
            "policy/domain/A/proxy.txt": "+.google.com\n",
            "block/domain/A/ads.txt": ".ads.google.com\n",
        })
        assert implicit == {"policy(父) → block(子)": [(".ads.google.com", "google.com")]}

    def test_bare_parent_does_not_cover_child(self, tmp_path):
        _explicit, implicit = self.build(tmp_path, {
            "policy/domain/A/proxy.txt": "google.com\n",
            "block/domain/A/ads.txt": "ads.google.com\n",
        })
        assert implicit == {}

    def test_subdomain_parent_does_not_cover_child(self, tmp_path):
        _explicit, implicit = self.build(tmp_path, {
            "policy/domain/A/proxy.txt": ".google.com\n",
            "block/domain/A/ads.txt": ".ads.google.com\n",
        })
        assert implicit == {}

    def test_single_strategy_reports_nothing(self, tmp_path):
        explicit, implicit = self.build(tmp_path, {
            "block/domain/A/x.txt": "a.com\nb.com\n",
        })
        assert explicit == {} and implicit == {}

    def test_ipcidr_directories_ignored(self, tmp_path):
        explicit, implicit = self.build(tmp_path, {
            "block/ipcidr/A/x.txt": "1.0.0.0/24\n",
            "policy/domain/A/y.txt": "1.0.0.0/24\n",
        })
        assert explicit == {} and implicit == {}

    def test_missing_dir_returns_empty(self, tmp_path):
        assert merger.detect_cross_policy_conflicts(str(tmp_path / "nope")) == ({}, {})


class TestRepoAnchor:

    def test_source_dir_outside_repo_is_rejected(self, monkeypatch):
        monkeypatch.setattr(merger, "SOURCE_DIR", tempfile.gettempdir())

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1

    def test_output_dir_outside_repo_is_rejected(self, monkeypatch, work_dir):
        source = work_dir / "rulesets"
        source.mkdir(parents=True)
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", tempfile.gettempdir())

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1
