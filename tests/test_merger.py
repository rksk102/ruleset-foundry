import tempfile
from pathlib import Path

import manifest
import merger
import pytest
import utils
from support import write_text as write


def fake_manifest(*rels, source=None):
    return manifest.Manifest("test", {
        rel: manifest.file_sha256(str(source / rel)) if source is not None else "0" * 64
        for rel in rels
    })


def real_manifest(source, *rels):
    return fake_manifest(*rels, source=source)


class TestDetectMode:
    def test_ipcidr(self):
        assert merger.detect_mode("ipcidr") == "IP-CIDR"
        assert merger.detect_mode("IpCidr") == "IP-CIDR"

    def test_domain(self):
        assert merger.detect_mode("domain") == "DOMAIN"

    def test_unknown_type_raises(self):
        for raw in ["General", "IP-CIDR", "skip"]:
            with pytest.raises(ValueError):
                merger.detect_mode(raw)


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
        with pytest.raises(ValueError, match="取值非法"):
            merger.resolve_conflict_action("explode", True)


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

    def test_hidden_dir_skipped(self, tmp_path):
        write(tmp_path / ".hidden" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        assert merger.auto_discover_files(str(tmp_path)) == []

    def test_nested_inputs_rejected_instead_of_collapsing_output(self, tmp_path):
        write(tmp_path / "block" / "domain" / "Owner" / "a.txt", "a.com\n")
        write(tmp_path / "block" / "domain" / "Owner" / "sub" / "a.txt", "b.com\n")
        with pytest.raises(ValueError, match="嵌套"):
            merger.auto_discover_files(str(tmp_path))

    def test_missing_dir_returns_empty(self, tmp_path):
        assert merger.auto_discover_files(str(tmp_path / "nope")) == []


class TestProcessTaskLogic:
    def use_dirs(self, monkeypatch, source, output, fn):
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(output))
        return fn()

    def test_merges_and_reports_counts(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\ngoogle.com\n")
        write(source / "block" / "domain" / "B" / "two.txt", "google.com\nsub.google.com\n")

        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "block",
                "domain",
                "Owner",
                "all.txt",
                ["block/domain/A/one.txt", "block/domain/B/two.txt"],
                "合并测试",
                baseline=real_manifest(source, "block/domain/A/one.txt", "block/domain/B/two.txt"),
            ),
        )

        assert result["raw"] == 3
        assert result["opt"] == 3
        assert result["path"] == "block/domain/Owner"
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert {"ads.example.com", "google.com", "sub.google.com"} <= set(content.splitlines())

    def test_suffix_parent_dedups_children(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "one.txt", "+.google.com\n+.ads.google.com\n")

        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "block",
                "domain",
                "Owner",
                "all.txt",
                ["block/domain/A/one.txt"],
                "后缀去重测试",
                baseline=real_manifest(source, "block/domain/A/one.txt"),
            ),
        )

        assert result["raw"] == 2
        assert result["opt"] == 1
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert "+.google.com" in content
        assert "+.ads.google.com" not in content

    def test_subdomain_parent_drops_strict_suffix_child(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "one.txt", ".example.com\n+.www.example.com\n")

        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "block", "domain", "Owner", "all.txt", ["block/domain/A/one.txt"], "根域覆盖测试",
                baseline=real_manifest(source, "block/domain/A/one.txt"),
            ),
        )

        assert result["raw"] == 2
        assert result["opt"] == 1
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert [line for line in content.splitlines() if not line.startswith("#")] == [".example.com"]

    def test_sources_recorded_in_header(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(
            source / "block" / "domain" / "A" / "one.txt",
            "# Source: https://raw.githubusercontent.com/Owner/repo/main/one.txt\nads.example.com\n",
        )
        self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "block",
                "domain",
                "Owner",
                "all.txt",
                ["block/domain/A/one.txt"],
                "来源透传测试",
                baseline=real_manifest(source, "block/domain/A/one.txt"),
            ),
        )
        content = (output / "block" / "domain" / "Owner" / "all.txt").read_text(encoding="utf-8")
        assert "# Sources: https://raw.githubusercontent.com/Owner/repo/main/one.txt" in content

    def test_missing_input_raises(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        source.mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="合并输入缺失"):
            self.use_dirs(
                monkeypatch,
                source,
                tmp_path / "merged",
                lambda: merger.process_task_logic(
                    "block",
                    "domain",
                    "Owner",
                    "all.txt",
                    ["block/domain/A/missing.txt"],
                    "x",
                    baseline=real_manifest(source),
                ),
            )

    def test_missing_input_tolerated_with_allow_partial(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "block" / "domain" / "A" / "keep.txt", "ads.example.com\n")
        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "block",
                "domain",
                "Owner",
                "all.txt",
                ["block/domain/A/keep.txt", "block/domain/A/missing.txt"],
                "x",
                allow_partial=True,
                baseline=real_manifest(source, "block/domain/A/keep.txt"),
            ),
        )
        assert result["src_count"] == 1
        assert (output / "block" / "domain" / "Owner" / "all.txt").is_file()

    def test_all_missing_inputs_skipped_with_allow_partial(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        source.mkdir(parents=True)
        result = self.use_dirs(
            monkeypatch,
            source,
            tmp_path / "merged",
            lambda: merger.process_task_logic(
                "block",
                "domain",
                "Owner",
                "all.txt",
                ["block/domain/A/missing.txt"],
                "x",
                allow_partial=True,
                baseline=real_manifest(source),
            ),
        )
        assert result is None

    def test_input_outside_source_dir_rejected(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        source.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        write(outside, "ads.example.com\n")

        def run():
            merger.process_task_logic(
                "block", "domain", "Owner", "all.txt", [str(outside)], "x",
                baseline=real_manifest(source),
            )

        with pytest.raises(ValueError):
            self.use_dirs(monkeypatch, source, tmp_path / "merged", run)

        def run_relative():
            merger.process_task_logic(
                "block", "domain", "Owner", "all.txt", ["../outside.txt"], "x",
                baseline=real_manifest(source),
            )

        with pytest.raises(ValueError):
            self.use_dirs(monkeypatch, source, tmp_path / "merged", run_relative)

    def test_output_traversal_rejected_without_writing(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\n")
        output = tmp_path / "merged"
        output.mkdir(parents=True)
        escaped = (output / ".." / ".." / ".." / "domain" / "PWNED.txt").resolve()

        def run():
            merger.process_task_logic(
                "../../..", "domain", "Owner", "../PWNED.txt", ["block/domain/A/one.txt"], "x",
                baseline=real_manifest(source, "block/domain/A/one.txt"),
            )

        with pytest.raises(ValueError):
            self.use_dirs(monkeypatch, source, output, run)
        assert not escaped.exists()

    def test_filename_traversal_rejected_without_writing(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        write(source / "block" / "domain" / "A" / "one.txt", "ads.example.com\n")
        output = tmp_path / "merged"
        owner_dir = output / "block" / "domain" / "Owner"
        escaped = (owner_dir / ".." / "escape.txt").resolve()

        def run():
            merger.process_task_logic(
                "block", "domain", "Owner", "../escape.txt", ["block/domain/A/one.txt"], "x",
                baseline=real_manifest(source, "block/domain/A/one.txt"),
            )

        with pytest.raises(ValueError):
            self.use_dirs(monkeypatch, source, output, run)
        assert not escaped.exists()

    def test_ip_task_drops_default_route(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        write(source / "direct" / "ipcidr" / "A" / "cn.txt", "0.0.0.0/0\n1.0.0.0/24\n1.0.1.0/24\n")
        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "direct",
                "ipcidr",
                "Owner",
                "all-cn.txt",
                ["direct/ipcidr/A/cn.txt"],
                "x",
                baseline=real_manifest(source, "direct/ipcidr/A/cn.txt"),
            ),
        )
        assert result["opt"] == 1
        content = (output / "direct" / "ipcidr" / "Owner" / "all-cn.txt").read_text(encoding="utf-8")
        assert "1.0.0.0/23" in content
        assert "0.0.0.0/0" not in content

    @pytest.mark.parametrize(
        "halves", [("0.0.0.0/1", "128.0.0.0/1"), ("::/1", "8000::/1")], ids=["ipv4", "ipv6"]
    )
    def test_ip_merge_rejects_complete_address_family(self, work_dir, monkeypatch, halves):
        source = work_dir / "rulesets"
        output = work_dir / "merged"
        inputs = ["direct/ipcidr/A/left.txt", "direct/ipcidr/B/right.txt"]
        for rel, rule in zip(inputs, halves, strict=True):
            write(source / rel, rule + "\n")

        with pytest.raises(utils.FullAddressFamilyCoverageError, match=r"覆盖整个 IPv[46] 地址族"):
            self.use_dirs(
                monkeypatch,
                source,
                output,
                lambda: merger.process_task_logic(
                    "direct", "ipcidr", "Owner", "all.txt", inputs, "合并测试",
                    baseline=real_manifest(source, *inputs),
                ),
            )

        assert not (output / "direct/ipcidr/Owner/all.txt").exists()

    def test_ip_merge_rejects_mapped_boundary_created_by_collapse(self, work_dir, monkeypatch):
        source = work_dir / "rulesets"
        output = work_dir / "merged"
        inputs = ["direct/ipcidr/A/left.txt", "direct/ipcidr/B/right.txt"]
        for rel, rule in zip(inputs, ("::fffe:0:0/96", "::ffff:0:0/96"), strict=True):
            write(source / rel, rule + "\n")

        with pytest.raises(utils.MappedAddressFamilyBoundaryError, match="映射地址块边界"):
            self.use_dirs(
                monkeypatch,
                source,
                output,
                lambda: merger.process_task_logic(
                    "direct", "ipcidr", "Owner", "all.txt", inputs, "合并测试",
                    baseline=real_manifest(source, *inputs),
                ),
            )

        assert not (output / "direct/ipcidr/Owner/all.txt").exists()

    def test_ip_task_reports_bounded_invalid_cidr_samples(self, tmp_path, monkeypatch):
        source = tmp_path / "rulesets"
        output = tmp_path / "merged"
        lines = [f"bad-cidr-{i}" for i in range(15)] + ["1.0.0.0/24"]
        write(source / "direct" / "ipcidr" / "A" / "cn.txt", "\n".join(lines) + "\n")
        messages = []
        monkeypatch.setattr(merger, "warning", lambda message: messages.append(message))

        result = self.use_dirs(
            monkeypatch,
            source,
            output,
            lambda: merger.process_task_logic(
                "direct",
                "ipcidr",
                "Owner",
                "all-cn.txt",
                ["direct/ipcidr/A/cn.txt"],
                "x",
                baseline=real_manifest(source, "direct/ipcidr/A/cn.txt"),
            ),
        )

        assert result["opt"] == 1
        invalid = [message for message in messages if "无效 CIDR" in message]
        assert len([message for message in invalid if message.strip().startswith("无效 CIDR:")]) == 10
        assert len(invalid) == utils.IP_CIDR_ERROR_SAMPLES + 1
        assert any("及其他 5 条无效 CIDR" in message for message in invalid)


class TestDetectCrossPolicyConflicts:
    def build(self, tmp_path, files):
        for rel, text in files.items():
            write(tmp_path / rel, text)
        return merger.detect_cross_policy_conflicts(str(tmp_path))

    def test_explicit_conflict_same_domain_two_strategies(self, tmp_path):
        explicit, _implicit = self.build(
            tmp_path,
            {
                "block/domain/A/x.txt": "same.example.com\n",
                "policy/domain/A/y.txt": "same.example.com\n",
            },
        )
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_explicit_conflict_across_prefix_forms(self, tmp_path):
        explicit, _implicit = self.build(
            tmp_path,
            {
                "block/domain/A/x.txt": "+.same.example.com\n",
                "policy/domain/A/y.txt": "same.example.com\n",
            },
        )
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_implicit_conflict_suffix_parent_covers_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "+.google.com\n",
                "block/domain/A/ads.txt": "ads.google.com\n",
            },
        )
        assert implicit == {"policy(父) → block(子)": [("ads.google.com", "google.com")]}

    def test_implicit_conflict_suffix_parent_covers_subdomain_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "+.google.com\n",
                "block/domain/A/ads.txt": ".ads.google.com\n",
            },
        )
        assert implicit == {"policy(父) → block(子)": [(".ads.google.com", "google.com")]}

    def test_bare_parent_does_not_cover_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "google.com\n",
                "block/domain/A/ads.txt": "ads.google.com\n",
            },
        )
        assert implicit == {}

    def test_subdomain_parent_covers_subdomain_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": ".google.com\n",
                "block/domain/A/ads.txt": ".ads.google.com\n",
            },
        )
        assert implicit == {"policy(父) → block(子)": [(".ads.google.com", "google.com")]}

    def test_subdomain_and_bare_domain_do_not_conflict(self, tmp_path):
        explicit, implicit = self.build(
            tmp_path,
            {
                "block/domain/A/x.txt": ".example.com\n",
                "policy/domain/A/y.txt": "example.com\n",
            },
        )
        assert explicit == {}
        assert implicit == {}

    def test_single_strategy_reports_nothing(self, tmp_path):
        explicit, implicit = self.build(
            tmp_path,
            {
                "block/domain/A/x.txt": "a.com\nb.com\n",
            },
        )
        assert explicit == {} and implicit == {}

    def test_ipcidr_directories_ignored(self, tmp_path):
        explicit, implicit = self.build(
            tmp_path,
            {
                "block/ipcidr/A/x.txt": "1.0.0.0/24\n",
                "policy/domain/A/y.txt": "1.0.0.0/24\n",
            },
        )
        assert explicit == {} and implicit == {}

    def test_ipcidr_substring_in_parent_dir_does_not_hide_conflicts(self, work_dir):
        base = work_dir / "ipcidr-work"
        write(base / "block/domain/A/x.txt", "same.example.com\n")
        write(base / "policy/domain/A/y.txt", "same.example.com\n")
        explicit, _implicit = merger.detect_cross_policy_conflicts(str(base))
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_ipcidr_substring_in_owner_does_not_hide_conflicts(self, tmp_path):
        explicit, _implicit = self.build(
            tmp_path,
            {
                "block/domain/ipcidr-owner/x.txt": "same.example.com\n",
                "policy/domain/A/y.txt": "same.example.com\n",
            },
        )
        assert explicit == {"block ↔ policy": ["same.example.com"]}

    def test_wildcard_parent_covers_exact_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "*.example.com\n",
                "block/domain/A/ads.txt": "www.example.com\n",
            },
        )
        assert implicit.get("policy(父) → block(子)") == [("www.example.com", "*.example.com")]

    def test_multi_wildcard_does_not_cover_shallower_exact_domain(self, tmp_path, monkeypatch):
        explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "*.*.example.com\n",
                "block/domain/A/ads.txt": "a.example.com\n",
            },
        )
        assert explicit == {}
        assert implicit == {}

    def test_wildcard_parent_covers_suffix_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "*.example.com\n",
                "block/domain/A/ads.txt": "+.example.com\n",
            },
        )
        assert implicit.get("policy(父) → block(子)") == [("+.example.com", "*.example.com")]

    def test_wildcard_parent_covers_subdomain_child(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "*.example.com\n",
                "block/domain/A/ads.txt": ".example.com\n",
            },
        )
        assert implicit.get("policy(父) → block(子)") == [(".example.com", "*.example.com")]

    def test_wildcard_parent_does_not_cover_multi_level_exact(self, tmp_path):
        _explicit, implicit = self.build(
            tmp_path,
            {
                "policy/domain/A/proxy.txt": "*.example.com\n",
                "block/domain/A/ads.txt": "a.b.example.com\n",
            },
        )
        assert implicit == {}

    def test_missing_dir_returns_empty(self, tmp_path):
        assert merger.detect_cross_policy_conflicts(str(tmp_path / "nope")) == ({}, {})


class TestRepoAnchor:
    def test_repo_root_is_rejected(self, monkeypatch):
        monkeypatch.setattr(merger, "error", lambda *a, **k: None)
        monkeypatch.setattr(merger, "gh_error", lambda *a, **k: None)
        with pytest.raises(SystemExit) as exc:
            merger._ensure_repo_anchored("x", str(Path(__file__).resolve().parent.parent))
        assert exc.value.code == 1

    def test_subdir_is_accepted(self):
        repo = Path(__file__).resolve().parent.parent
        merger._ensure_repo_anchored("x", str(repo / "merged-rules"))

    def test_protected_subdir_is_rejected(self, monkeypatch):
        monkeypatch.setattr(merger, "error", lambda *a, **k: None)
        monkeypatch.setattr(merger, "gh_error", lambda *a, **k: None)
        repo = Path(__file__).resolve().parent.parent
        with pytest.raises(SystemExit):
            merger._ensure_repo_anchored("x", str(repo / "scripts"))

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


class TestConflictPolicyIgnore:
    def setup_run(self, monkeypatch, work_dir, policy):
        source = work_dir / "rulesets"
        write(source / "block" / "domain" / "Owner" / "ads.txt", "+.example.com\n")
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": []})
        monkeypatch.setattr(
            merger,
            "load_and_verify_source_manifest",
            lambda *a, **k: fake_manifest("block/domain/Owner/ads.txt", source=source),
        )
        monkeypatch.setattr(merger, "verify_merged_products", lambda tasks, **kwargs: True)
        monkeypatch.setattr(merger, "get", lambda *a, **k: policy)

    def test_ignore_skips_detection_entirely(self, monkeypatch, work_dir):
        self.setup_run(monkeypatch, work_dir, "ignore")
        calls = []

        def boom(_merged_dir):
            calls.append(_merged_dir)
            raise AssertionError("conflict_policy=ignore 时不应执行冲突检测")

        monkeypatch.setattr(merger, "detect_cross_policy_conflicts", boom)

        merger.main()

        assert calls == []

    def test_warn_still_runs_detection(self, monkeypatch, work_dir):
        self.setup_run(monkeypatch, work_dir, "warn")
        calls = []
        monkeypatch.setattr(
            merger, "detect_cross_policy_conflicts", lambda merged_dir: (calls.append(merged_dir), ({}, {}))[1]
        )

        merger.main()

        assert len(calls) == 1


class TestWildcardPatternConflictPolicy:
    @pytest.mark.parametrize(
        ("left", "right", "policy", "overlap"),
        [
            ("*.*.example.com", "*.a.example.com", "fail", True),
            ("*.a.example.com", "*.*.example.com", "fail", True),
            ("*.*.example.com", "*.a.example.com", "warn", True),
            ("*.*.example.com", "*.a.example.com", "ignore", True),
            ("*.*.example.com", "*.example.com", "fail", False),
            ("*.a.example.com", "*.b.example.com", "fail", False),
        ],
    )
    def test_pattern_intersection_reaches_merge_policy(self, work_dir, monkeypatch, left, right, policy, overlap):
        source = work_dir / "rulesets"
        output = work_dir / "merged"
        summary = work_dir / "summary.md"
        entries = {"block/domain/Owner/left.txt": left, "direct/domain/Owner/right.txt": right}
        for rel, pattern in entries.items():
            write(source / rel, pattern + "\n")
        manifest.save_manifest(
            source / manifest.MANIFEST_NAME,
            manifest.hash_entries(source, entries),
            generation=manifest.expected_generation() or "test-patterns",
        )
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(output))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": []})
        original_get = merger.get
        monkeypatch.setattr(
            merger,
            "get",
            lambda *keys, **kwargs: (
                policy if keys == ("behavior", "conflict_policy") else original_get(*keys, **kwargs)
            ),
        )
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        if overlap and policy == "fail":
            with pytest.raises(SystemExit) as exc:
                merger.main()
            assert exc.value.code == 1
        else:
            merger.main()

        text = summary.read_text(encoding="utf-8")
        assert ("### 隐式冲突检测" in text) == (overlap and policy != "ignore")
        if overlap and policy != "ignore":
            assert left in text and right in text
        if overlap and policy == "fail":
            assert manifest.load_manifest(output / manifest.MANIFEST_NAME) is None
            return
        assert manifest.verify_manifest(manifest.load_manifest(output / manifest.MANIFEST_NAME), output, "merge")
        for rel, pattern in entries.items():
            rules = [
                line for line in (output / rel).read_text(encoding="utf-8").splitlines() if not line.startswith("#")
            ]
            assert rules == [pattern]


class TestConflictPolicyReporting:
    def setup_run(self, monkeypatch, work_dir, policy):
        source = work_dir / "rulesets"
        write(source / "block" / "domain" / "Owner" / "ads.txt", "+.example.com\n")
        write(source / "policy" / "domain" / "Owner" / "proxy.txt", "+.example.com\n")
        summary = work_dir / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": []})
        monkeypatch.setattr(
            merger,
            "load_and_verify_source_manifest",
            lambda *a, **k: fake_manifest(
                "block/domain/Owner/ads.txt", "policy/domain/Owner/proxy.txt", source=source
            ),
        )
        monkeypatch.setattr(merger, "verify_merged_products", lambda tasks, **kwargs: True)
        monkeypatch.setattr(merger, "get", lambda *a, **k: policy)
        return summary

    def test_warn_writes_conflict_summary(self, monkeypatch, work_dir):
        summary = self.setup_run(monkeypatch, work_dir, "warn")

        merger.main()

        text = summary.read_text(encoding="utf-8")
        assert "### 显式冲突检测" in text
        assert any(line.strip() == "- `example.com`" for line in text.splitlines())

    def test_fail_exits_after_writing_summary(self, monkeypatch, work_dir):
        summary = self.setup_run(monkeypatch, work_dir, "fail")

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1
        text = summary.read_text(encoding="utf-8")
        assert "### 显式冲突检测" in text


class TestMergerGuards:
    def test_publish_failure_preserves_previous_generation(self, work_dir, monkeypatch):
        source, output = work_dir / "rulesets", work_dir / "merged"
        rel = "block/domain/Owner/rules.txt"
        write(source / rel, "a.example\n")
        manifest.save_manifest(
            source / manifest.MANIFEST_NAME,
            manifest.hash_entries(source, [rel]),
            generation=manifest.expected_generation() or "test-cleanup",
        )
        stale = output / "stale.md"
        write(stale, "stale file\n")
        replace = merger.os.replace

        def fail_publish(src, dst):
            if Path(src) == work_dir / ".merge-merged.staging.tmp" and Path(dst) == output:
                raise OSError("replace failed")
            return replace(src, dst)

        monkeypatch.setattr(merger.os, "replace", fail_publish)
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(output))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": []})

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1

        assert stale.read_text(encoding="utf-8") == "stale file\n"
        assert not (output / rel).exists()
        assert not (output / manifest.MANIFEST_NAME).exists()

    def test_replace_failure_restores_previous_directory(self, work_dir, monkeypatch):
        output = work_dir / "merged"
        staging = work_dir / ".merge-staging-test"
        write(output / "old.txt", "old")
        write(staging / "new.txt", "new")
        replace = merger.os.replace

        def fail_new(src, dst):
            if Path(src) == staging:
                raise OSError("replace failed")
            return replace(src, dst)

        monkeypatch.setattr(merger.os, "replace", fail_new)

        with pytest.raises(OSError, match="replace failed"):
            merger._publish_staged_products(staging, output)

        assert (output / "old.txt").read_text(encoding="utf-8") == "old"
        assert not (work_dir / ".merge-merged.previous.tmp").exists()

    def test_restore_failure_preserves_previous_directory(self, work_dir, monkeypatch):
        output = work_dir / "merged"
        staging = work_dir / ".merge-staging-test"
        write(output / "old.txt", "old")
        write(staging / "new.txt", "new")

        replace = merger.os.replace

        def fail_restores(src, dst):
            if Path(src) in {work_dir / ".merge-merged.previous.tmp", staging}:
                raise OSError("restore failed")
            return replace(src, dst)

        monkeypatch.setattr(merger.os, "replace", fail_restores)

        with pytest.raises(OSError, match="备份保留在"):
            merger._publish_staged_products(staging, output)

        previous = work_dir / ".merge-merged.previous.tmp"
        assert (previous / "old.txt").read_text(encoding="utf-8") == "old"

    def test_interrupted_publish_recovers_only_the_requested_output(self, work_dir):
        output = work_dir / "merged"
        staging = work_dir / ".merge-merged.staging.tmp"
        previous = work_dir / ".merge-merged.previous.tmp"
        other = work_dir / ".merge-other.staging.tmp"
        write(previous / "old.txt", "old")
        write(staging / "discard.txt", "incomplete")
        write(other / "keep.txt", "other")

        with merger._merge_workspace(output):
            assert (output / "old.txt").read_text(encoding="utf-8") == "old"
            assert not (staging / "discard.txt").exists()
            with pytest.raises(OSError, match="无法取得合并输出写锁"), merger._merge_workspace(output):
                pass

        assert not previous.exists()
        assert not staging.exists()
        assert (other / "keep.txt").read_text(encoding="utf-8") == "other"

    def test_merge_task_and_passthrough_may_not_share_a_path(self, work_dir, monkeypatch, capsys):
        source = work_dir / "rulesets"
        cfg = work_dir / "config.yaml"
        cfg.write_text("merges: []\n", encoding="utf-8")
        monkeypatch.setattr(merger, "resolve_config_file", lambda: cfg)
        write(source / "block" / "domain" / "Owner" / "ads.txt", "a.example\n")
        tasks = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "Owner",
                "filename": "ads.txt",
                "inputs": ["block/domain/Owner/ads.txt"],
            }
        ]
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": tasks})
        monkeypatch.setattr(
            merger, "load_and_verify_source_manifest", lambda *a, **k: fake_manifest("block/domain/Owner/ads.txt")
        )

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1
        assert "同一路径" in capsys.readouterr().out

    def test_casefold_cross_path_collision_fails_before_cleanup(self, work_dir, monkeypatch):
        source = work_dir / "rulesets"
        output = work_dir / "merged"
        write(source / "block" / "domain" / "Owner" / "a.txt", "a.example\n")
        write(output / "keep.txt", "preserve\n")
        tasks = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "owner",
                "filename": "a.txt",
                "inputs": ["block/domain/Owner/a.txt"],
            }
        ]
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(output))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": tasks})
        monkeypatch.setattr(
            merger, "load_and_verify_source_manifest", lambda *a, **k: fake_manifest("block/domain/Owner/a.txt")
        )

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1
        assert (output / "keep.txt").read_text(encoding="utf-8") == "preserve\n"

    def test_auto_paths_casefold_collision_fails_before_cleanup(self, work_dir, monkeypatch):
        source = work_dir / "rulesets"
        output = work_dir / "merged"
        write(source / "block" / "domain" / "Owner" / "a.txt", "a.example\n")
        write(source / "block" / "domain" / "owner" / "a.txt", "b.example\n")
        write(output / "keep.txt", "preserve\n")
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(output))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": []})
        monkeypatch.setattr(merger, "load_and_verify_source_manifest", lambda *a, **k: fake_manifest())

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1
        assert (output / "keep.txt").read_text(encoding="utf-8") == "preserve\n"

    def test_zero_rule_merge_result_is_refused(self, work_dir, monkeypatch):
        source = work_dir / "rulesets"
        write(source / "block" / "domain" / "Owner" / "ads.txt", "# 只有注释\n")
        monkeypatch.setattr(merger, "SOURCE_DIR", str(source))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))

        with pytest.raises(ValueError, match="合并结果为空"):
            merger.process_task_logic(
                "block", "domain", "Owner", "out.txt", ["block/domain/Owner/ads.txt"], "测试",
                baseline=real_manifest(source, "block/domain/Owner/ads.txt"),
            )
