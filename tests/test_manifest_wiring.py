import os
from pathlib import Path

import main
import manifest
import merger
import pytest
import release_handler
from support import domain_mrs_bytes, ipcidr_mrs_bytes
from support import write_text as write
from test_release_handler import ReleaseState, current_expected_tag


def mrs_of(entries):
    return [rel[: -len(".txt")] + ".mrs" for rel in entries]


GEN = "test-generation"


def write_v2_manifest(root, entries=None, generation=GEN):
    root.mkdir(parents=True, exist_ok=True)
    rels = sorted(manifest.collect_files(root) if entries is None else set(entries))
    hashes = {}
    for rel in rels:
        target = root / rel
        hashes[rel] = manifest.file_sha256(str(target)) if target.exists() else "0" * 64
    manifest.save_manifest(root / manifest.MANIFEST_NAME, hashes, generation=generation)


def repo_merge_paths():
    return sorted(release_handler.merge_product_entries(release_handler.get("merges")))


def build_products(
    root,
    txt_entries,
    mrs_entries=None,
    manifest_entries=None,
    manifest_file=True,
    rulesets_dir=True,
):
    for rel in txt_entries:
        is_ip = Path(rel).parts[1] == "ipcidr"
        write(root / "merged-rules" / rel, "192.0.2.0/24\n" if is_ip else "rule.example\n")
    for rel in mrs_of(txt_entries) if mrs_entries is None else mrs_entries:
        target = root / "merged-rules-mrs" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(rel).parts[1] == "ipcidr":
            target.write_bytes(ipcidr_mrs_bytes(["192.0.2.0/24"]))
        else:
            target.write_bytes(domain_mrs_bytes(["rule.example"]))
    write_v2_manifest(root / "merged-rules")
    if not rulesets_dir:
        return
    os.makedirs(root / "rulesets", exist_ok=True)
    if manifest_file:
        write_v2_manifest(root / "rulesets", entries=txt_entries if manifest_entries is None else manifest_entries)


class TestFinalizeProducts:
    def _prepare(self, work_dir, monkeypatch, names, stale):
        root = work_dir / "rulesets"
        for rel in names + stale:
            write(root / rel, "a.example\n")
        monkeypatch.setattr(main, "RULESETS_DIR", root)
        monkeypatch.setattr(main, "MANIFEST_FILE", root / "products.manifest")
        return root, [root / rel for rel in names]

    def test_stale_file_is_cleaned_and_never_recorded(self, work_dir, monkeypatch):
        root, expected = self._prepare(work_dir, monkeypatch, ["block/domain/Owner/a.txt"], ["sync-summary.txt"])
        produced = main.finalize_products(expected)
        assert produced == {"block/domain/Owner/a.txt"}
        assert not (root / "sync-summary.txt").exists()
        loaded = manifest.load_manifest(root / "products.manifest")
        assert list(loaded.entries) == ["block/domain/Owner/a.txt"]
        assert loaded.generation
        assert manifest.verify_manifest(loaded, root, "finalize") is True

    def test_manifest_mismatch_fails_closed(self, work_dir, monkeypatch, capsys):
        _root, expected = self._prepare(work_dir, monkeypatch, ["block/domain/Owner/a.txt"], ["sync-summary.txt"])
        monkeypatch.setattr(main, "clean_orphans", lambda _files: None)
        with pytest.raises(SystemExit) as exc:
            main.finalize_products(expected)
        assert exc.value.code == 1
        assert "产物清单与磁盘不一致" in capsys.readouterr().out


class TestCleanOrphans:
    def test_directory_named_txt_does_not_crash_cleanup(self, work_dir, monkeypatch):
        root = work_dir / "rulesets"
        product = root / "block" / "domain" / "Owner" / "a.txt"
        product.parent.mkdir(parents=True)
        product.write_text("a.example\n", encoding="utf-8")
        (root / "foo.txt").mkdir()
        (root / "foo.txt" / "inner.txt").write_text("x.example\n", encoding="utf-8")
        monkeypatch.setattr(main, "RULESETS_DIR", root)

        main.clean_orphans([product])

        assert product.exists()
        assert not (root / "foo.txt").exists()


class ReleaseRun:
    """在 work_dir 内运行 release_handler，gh 与发布状态全部旁路。"""

    def __init__(self, root, *, ci_build=None):
        self.root = root
        self.ci_build = ci_build

    def __enter__(self):
        self._cwd = os.getcwd()
        self._patch = pytest.MonkeyPatch()
        self._saved = (
            release_handler.REPO_ROOT,
            release_handler.TARGET_CONFIG,
            release_handler.CHANGE_DETECTION,
            release_handler.KEEP_DAYS,
            release_handler.run_gh,
            release_handler.run_gh_bytes,
            release_handler.load_last_hash,
            release_handler.save_last_hash,
        )
        release_handler.REPO_ROOT = ""
        release_handler.TARGET_CONFIG = {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"}
        release_handler.CHANGE_DETECTION = True
        release_handler.KEEP_DAYS = 3
        os.chdir(self.root)
        state = ReleaseState(self.root, current_expected_tag())
        self.state = state
        self._patch.setenv("GITHUB_REPOSITORY", "rksk102/ruleset-foundry")
        self._patch.setenv("GITHUB_REF", "refs/heads/main")
        self._patch.delenv("PUBLISH_GUARD", raising=False)
        if self.ci_build is None:
            self._patch.delenv("GITHUB_ACTIONS", raising=False)
        else:
            run_id, attempt = self.ci_build
            self._patch.setenv("GITHUB_ACTIONS", "true")
            state.add_build(run_id, attempt, os.environ["GITHUB_SHA"])
        release_handler.run_gh = state
        release_handler.run_gh_bytes = state.bytes
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda _hash: None
        os.chdir(self.root)
        return self

    def __exit__(self, *exc):
        (
            release_handler.REPO_ROOT,
            release_handler.TARGET_CONFIG,
            release_handler.CHANGE_DETECTION,
            release_handler.KEEP_DAYS,
            release_handler.run_gh,
            release_handler.run_gh_bytes,
            release_handler.load_last_hash,
            release_handler.save_last_hash,
        ) = self._saved
        self._patch.undo()
        os.chdir(self._cwd)
        return False

    def run(self):
        try:
            release_handler.main()
        except SystemExit as exc:
            return exc.code
        return None


class MergerRun:
    """把 merger 的源/产物目录指向 work_dir，配置合并任务由测试注入。"""

    def __init__(self, root, merges):
        self.root = root
        self.merges = merges

    def __enter__(self):
        self._saved = (merger.SOURCE_DIR, merger.OUTPUT_DIR, merger.load_config)
        merger.SOURCE_DIR = str(self.root / "rulesets")
        merger.OUTPUT_DIR = str(self.root / "merged-rules")
        merger.load_config = lambda: {"merges": self.merges}
        return self

    def __exit__(self, *exc):
        merger.SOURCE_DIR, merger.OUTPUT_DIR, merger.load_config = self._saved
        return False

    def run(self):
        try:
            merger.main()
        except SystemExit as exc:
            return exc.code
        return None


class TestReleaseBaseline:
    def sample(self):
        merges = repo_merge_paths()
        rulesets = [f"block/domain/Owner{i}/rules{i}.txt" for i in range(3)]
        return rulesets, merges

    def test_missing_manifest_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.state.calls == []
        assert release_handler.BASELINE_MISSING in caplog.text

    def test_empty_manifest_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False)
        write(work_dir / "rulesets" / "products.manifest", "# 只有注释，没有条目\n\n")
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
        assert "不支持的产物清单格式" in caplog.text

    def test_empty_v2_manifest_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False)
        write_v2_manifest(work_dir / "rulesets", entries=[])
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
        assert release_handler.BASELINE_MISSING in caplog.text

    def test_verify_products_raises_without_baseline(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False)
        with ReleaseRun(work_dir), pytest.raises(manifest.ManifestError) as exc:
            release_handler.verify_products()
        assert release_handler.BASELINE_MISSING in str(exc.value)

    def test_missing_product_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets[:2] + merges, manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
        assert rulesets[2] in caplog.text

    def test_extra_product_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        extra = "policy/domain/Ghost/extra.txt"
        build_products(work_dir, rulesets + merges + [extra], manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
        assert extra in caplog.text

    def test_missing_merge_product_is_fatal(self, work_dir):
        rulesets, merges = self.sample()
        assert merges, "config.yaml 应至少配置一个 merges 任务"
        build_products(work_dir, rulesets, manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.state.calls == []

    def test_mrs_products_must_match_one_to_one(self, work_dir):
        rulesets, merges = self.sample()
        txt = rulesets[:2] + merges
        mrs = mrs_of([rulesets[0], *merges])
        build_products(work_dir, txt, mrs, manifest_entries=rulesets[:2])
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.state.calls == []
        with ReleaseRun(work_dir), pytest.raises(manifest.ManifestError) as exc:
            release_handler.verify_products()
        assert "逐一对应" in str(exc.value)

    def test_consistent_products_pass_and_publish(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() is None, run.state.release
            assert run.state.release is not None and not run.state.release["draft"]

    def test_verify_products_counts_baseline(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        with ReleaseRun(work_dir):
            assert release_handler.verify_products() == len(rulesets) + len(merges)

    def test_selected_build_generation_survives_a_publish_only_rerun(self, work_dir, monkeypatch):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        generation = "github-123-attempt-1"
        write_v2_manifest(work_dir / "rulesets", entries=rulesets, generation=generation)
        write_v2_manifest(work_dir / "merged-rules", generation=generation)
        monkeypatch.setenv("GITHUB_RUN_ID", "123")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", generation)
        with ReleaseRun(work_dir, ci_build=(123, 1)) as run:
            assert release_handler.verify_products(require_baseline=True) == len(rulesets) + len(merges)
            assert run.run() is None
            assert run.state.latest == run.state.tag
            tag = run.state.tags[run.state.tag]
            assert f'"generation":"{generation}"' in tag["message"]
            write_v2_manifest(work_dir / "merged-rules", generation="github-123-attempt-2")
            with pytest.raises(manifest.ManifestError, match="跨轮混合产物"):
                release_handler.verify_products(require_baseline=True)

    @pytest.mark.parametrize("directory", ["merged-rules", "merged-rules-mrs"])
    def test_complete_valid_pairs_with_unlisted_files_are_rejected(self, work_dir, directory):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        write(work_dir / directory / "extra.json", '{"case": "extra file"}\n')

        with ReleaseRun(work_dir), pytest.raises(manifest.ManifestError, match=r"多出.*extra\.json"):
            release_handler.verify_products(require_baseline=True)

    def test_allow_partial_filters_merge_products_without_baseline_inputs(self, work_dir, monkeypatch):
        write(work_dir / "rulesets" / "products.manifest", "")
        manifest.save_manifest(
            work_dir / "rulesets" / "products.manifest",
            {"block/domain/OwnerA/ads.txt": "0" * 64},
            generation=GEN,
        )
        merges = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/ads.txt"],
            },
            {
                "strategy": "policy",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-proxy.txt",
                "inputs": ["policy/domain/GoneB/gone.txt"],
            },
        ]
        monkeypatch.setattr(release_handler, "ALLOW_PARTIAL", True)
        monkeypatch.setattr(release_handler, "get", lambda *a, **k: merges)
        monkeypatch.chdir(work_dir)
        _expected_baseline, expected = release_handler.load_baseline()
        assert "block/domain/rksk/all-ads.txt" in expected
        assert "policy/domain/rksk/all-proxy.txt" not in expected

    def test_baseline_skipped_outside_pipeline_workspace(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False, rulesets_dir=False)
        with ReleaseRun(work_dir) as run:
            assert run.run() is None, run.state.release
            assert run.state.release is not None and not run.state.release["draft"]

    def test_pair_check_still_applies_without_baseline(self, work_dir):
        rulesets, merges = self.sample()
        txt = rulesets[:2] + merges
        build_products(work_dir, txt, mrs_of([rulesets[0], *merges]), manifest_file=False, rulesets_dir=False)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.state.calls == []


class TestMergerInputs:
    def test_all_missing_inputs_reported_at_once(self, work_dir, caplog):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "keep.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets")
        merges = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/keep.txt", "block/domain/OwnerA/gone1.txt"],
            },
            {
                "strategy": "policy",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-proxy.txt",
                "inputs": ["policy/domain/GoneB/gone2.txt", "policy/domain/GoneB/gone3.txt"],
            },
        ]
        with MergerRun(work_dir, merges) as run:
            assert run.run() == 1
        out = caplog.text
        for rel in ("gone1.txt", "gone2.txt", "gone3.txt"):
            assert rel in out
        assert "keep.txt" not in out
        assert not (work_dir / "merged-rules").exists()

    def test_missing_inputs_function_aggregates(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "keep.txt", "a.com\n")
        merges = [
            {"inputs": ["gone1.txt", "block/domain/OwnerA/keep.txt"]},
            {"inputs": ["gone2.txt", "gone1.txt"]},
        ]
        with MergerRun(work_dir, merges):
            assert merger.missing_merge_inputs(merges) == ["gone1.txt", "gone2.txt"]


    def test_merger_rejects_tampered_source_file(self, work_dir):
        source = work_dir / "rulesets"
        write(source / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(source)
        write(source / "block" / "domain" / "OwnerA" / "ads.txt", "tampered.example\n")
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

    def test_merger_rejects_generation_mismatch(self, work_dir, monkeypatch):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets", generation="github-match")

        monkeypatch.setenv("GITHUB_RUN_ID", "other")
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

        monkeypatch.setenv("GITHUB_RUN_ID", "match")
        with MergerRun(work_dir, []) as run:
            assert run.run() is None

    def test_merge_headers_are_byte_stable(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets")
        merges = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/ads.txt"],
            },
        ]
        with MergerRun(work_dir, merges) as run:
            assert run.run() is None
        assert (work_dir / "merged-rules" / "block" / "domain" / "OwnerA" / "ads.txt").is_file()
        merged = manifest.load_manifest(work_dir / "merged-rules" / "products.manifest")
        assert merged is not None
        assert "block/domain/rksk/all-ads.txt" in merged.entries
        assert manifest.verify_manifest(merged, work_dir / "merged-rules", "merged") is True
        target = work_dir / "merged-rules" / "block" / "domain" / "rksk" / "all-ads.txt"
        before = target.read_bytes()

        with MergerRun(work_dir, merges) as run:
            assert run.run() is None

        assert target.read_bytes() == before
        assert b"# Date:" not in before

    def test_allow_partial_merges_remaining_inputs(self, work_dir, monkeypatch):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets")
        merges = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-ads.txt",
                "inputs": [
                    "block/domain/OwnerA/ads.txt",
                    "block/domain/OwnerA/gone.txt",
                ],
            },
        ]
        monkeypatch.setattr(merger, "ALLOW_PARTIAL", True)
        with MergerRun(work_dir, merges) as run:
            assert run.run() is None
        assert (work_dir / "merged-rules" / "block" / "domain" / "rksk" / "all-ads.txt").is_file()

    def test_allow_partial_skips_task_with_all_inputs_missing(self, work_dir, monkeypatch):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets")
        merges = [
            {
                "strategy": "block",
                "type": "domain",
                "owner": "rksk",
                "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/gone.txt"],
            },
        ]
        monkeypatch.setattr(merger, "ALLOW_PARTIAL", True)
        with MergerRun(work_dir, merges) as run:
            assert run.run() is None
        assert not (work_dir / "merged-rules" / "block" / "domain" / "rksk" / "all-ads.txt").exists()
        assert (work_dir / "merged-rules" / "block" / "domain" / "OwnerA" / "ads.txt").is_file()

    def test_missing_baseline_is_fatal(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

    def test_product_missing_from_baseline_is_fatal(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(
            work_dir / "rulesets", entries=["block/domain/OwnerA/ads.txt", "block/domain/OwnerA/ghost.txt"]
        )
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

    def test_verify_merged_products_uses_baseline(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write_v2_manifest(work_dir / "rulesets")
        write(work_dir / "merged-rules" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        with MergerRun(work_dir, []):
            baseline = merger.load_and_verify_source_manifest()
            assert merger.verify_merged_products([], baseline=baseline) is True
            assert (work_dir / "merged-rules" / "products.manifest").is_file()

    @pytest.mark.parametrize(
        "change_content", [False, True], ids=["manifest-only", "manifest-and-bytes"]
    )
    def test_source_generation_change_during_merge_is_rejected(self, work_dir, monkeypatch, change_content):
        source = work_dir / "rulesets"
        rel = "block/domain/OwnerA/ads.txt"
        write(source / rel, "a.example\n")
        write_v2_manifest(source, generation="g1")
        merges = [{
            "strategy": "block",
            "type": "domain",
            "owner": "rksk",
            "filename": "all-ads.txt",
            "inputs": [rel],
        }]
        original = merger.process_task_logic

        def advance_generation(*args, **kwargs):
            result = original(*args, **kwargs)
            if change_content:
                write(source / rel, "b.example\n")
            write_v2_manifest(source, generation="g2")
            return result

        monkeypatch.setattr(merger, "process_task_logic", advance_generation)
        with MergerRun(work_dir, merges) as run:
            assert run.run() == 1
        assert manifest.load_manifest(work_dir / "merged-rules" / manifest.MANIFEST_NAME) is None

    @pytest.mark.parametrize("allow_partial", [False, True], ids=["strict", "partial"])
    def test_present_unlisted_input_is_rejected(self, work_dir, monkeypatch, allow_partial):
        source = work_dir / "rulesets"
        current = "block/domain/OwnerA/current.txt"
        hidden = "block/domain/OwnerA/.history/stale.txt"
        write(source / current, "current.example\n")
        write(source / hidden, "stale.example\n")
        write_v2_manifest(source)
        merges = [{
            "strategy": "block",
            "type": "domain",
            "owner": "rksk",
            "filename": "all-ads.txt",
            "inputs": [current, hidden],
        }]
        monkeypatch.setattr(merger, "ALLOW_PARTIAL", allow_partial)

        with MergerRun(work_dir, merges) as run:
            assert run.run() == 1

        assert not (work_dir / "merged-rules" / "block/domain/rksk/all-ads.txt").exists()
        assert manifest.load_manifest(work_dir / "merged-rules" / manifest.MANIFEST_NAME) is None

    @pytest.mark.parametrize("failure", ["generation", "conflict", "write"])
    def test_failed_merge_preserves_previous_verified_generation(
        self, work_dir, monkeypatch, failure
    ):
        source = work_dir / "rulesets"
        output = work_dir / "merged-rules"
        write(source / "block/domain/Owner/ads.txt", "same.example\n")
        write(source / "direct/domain/Owner/direct.txt", "same.example\n")
        write_v2_manifest(source)
        previous = {"block/domain/Previous/keep.txt": "previous.example\n"}
        for rel, content in previous.items():
            write(output / rel, content)
        write_v2_manifest(output, generation="old-generation")

        if failure == "generation":
            original = merger.process_task_logic

            def advance_generation(*args, **kwargs):
                result = original(*args, **kwargs)
                write_v2_manifest(source, generation="g2")
                return result

            monkeypatch.setattr(merger, "process_task_logic", advance_generation)
        elif failure == "conflict":
            original_get = merger.get
            monkeypatch.setattr(merger, "get", lambda *keys, **kwargs: (
                "fail" if keys == ("behavior", "conflict_policy") else original_get(*keys, **kwargs)
            ))
        else:
            def fail_manifest(*args, **kwargs):
                raise OSError("disk full")

            monkeypatch.setattr(manifest, "save_manifest", fail_manifest)

        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

        for rel, content in previous.items():
            assert (output / rel).read_text(encoding="utf-8") == content
        assert manifest.load_manifest(output / manifest.MANIFEST_NAME).generation == "old-generation"
        assert not list(output.glob(".*"))
