import os

import main
import manifest
import merger
import pytest
import release_handler


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def mrs_of(entries):
    return [rel[: -len(".txt")] + ".mrs" for rel in entries]


def repo_merge_paths():
    """config.yaml 里的合并产物路径（release_handler 会把它并入清单基线）。"""
    return sorted(release_handler.merge_product_entries(release_handler.get("merges")))


def build_products(root, txt_entries, mrs_entries=None, manifest_entries=None,
                   manifest_file=True, rulesets_dir=True):
    for rel in txt_entries:
        write(root / "merged-rules" / rel, "rule.example\n")
    for rel in (mrs_of(txt_entries) if mrs_entries is None else mrs_entries):
        write(root / "merged-rules-mrs" / rel, "MRS")
    if rulesets_dir:
        os.makedirs(root / "rulesets", exist_ok=True)
    if manifest_file:
        entries = txt_entries if manifest_entries is None else manifest_entries
        write(root / "rulesets" / "products.manifest", "\n".join(entries) + "\n")


class TestFinalizeProducts:

    def _prepare(self, work_dir, monkeypatch, names, stale):
        root = work_dir / "rulesets"
        for rel in names + stale:
            write(root / rel, "a.example\n")
        monkeypatch.setattr(main, "RULESETS_DIR", root)
        monkeypatch.setattr(main, "MANIFEST_FILE", root / "products.manifest")
        return root, [root / rel for rel in names]

    def test_stale_file_is_cleaned_and_never_recorded(self, work_dir, monkeypatch):
        root, expected = self._prepare(
            work_dir, monkeypatch, ["block/domain/Owner/a.txt"], ["sync-summary.txt"])
        produced = main.finalize_products(expected)
        assert produced == {"block/domain/Owner/a.txt"}
        assert not (root / "sync-summary.txt").exists()
        assert (root / "products.manifest").read_text(encoding="utf-8").split() == [
            "block/domain/Owner/a.txt"]

    def test_manifest_mismatch_fails_closed(self, work_dir, monkeypatch, capsys):
        _root, expected = self._prepare(
            work_dir, monkeypatch, ["block/domain/Owner/a.txt"], ["sync-summary.txt"])
        monkeypatch.setattr(main, "clean_orphans", lambda _files: None)
        with pytest.raises(SystemExit) as exc:
            main.finalize_products(expected)
        assert exc.value.code == 1
        assert "产物清单与磁盘不一致" in capsys.readouterr().out


class GhStub:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, fail_fast=False):
        self.calls.append(list(cmd))
        if cmd[:2] == ["release", "list"]:
            return "[]"
        if cmd[:2] == ["release", "view"]:
            return "2" if "assets" in cmd else None
        return "ok"

    def actions(self):
        return [tuple(call[:2]) for call in self.calls]


class ReleaseRun:
    """在 work_dir 内运行 release_handler，gh 与发布状态全部旁路。"""

    def __init__(self, root):
        self.root = root
        self.stub = GhStub()

    def __enter__(self):
        self._cwd = os.getcwd()
        self._saved = (
            release_handler.REPO_ROOT,
            release_handler.TARGET_CONFIG,
            release_handler.CHANGE_DETECTION,
            release_handler.KEEP_DAYS,
            release_handler.run_gh,
            release_handler.load_last_hash,
            release_handler.save_last_hash,
        )
        release_handler.REPO_ROOT = ""
        release_handler.TARGET_CONFIG = {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"}
        release_handler.CHANGE_DETECTION = True
        release_handler.KEEP_DAYS = 3
        release_handler.run_gh = self.stub
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
            release_handler.load_last_hash,
            release_handler.save_last_hash,
        ) = self._saved
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
            assert run.stub.calls == []
        assert release_handler.BASELINE_MISSING in caplog.text

    def test_empty_manifest_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False)
        write(work_dir / "rulesets" / "products.manifest", "# 只有注释，没有条目\n\n")
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
        out = caplog.text
        assert f"缺失: {rulesets[2]}" in out

    def test_extra_product_is_fatal(self, work_dir, caplog):
        rulesets, merges = self.sample()
        extra = "policy/domain/Ghost/extra.txt"
        build_products(work_dir, rulesets + merges + [extra], manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
        assert f"多出: {extra}" in caplog.text

    def test_missing_merge_product_is_fatal(self, work_dir):
        rulesets, merges = self.sample()
        assert merges, "config.yaml 应至少配置一个 merges 任务"
        build_products(work_dir, rulesets, manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.stub.calls == []

    def test_mrs_products_must_match_one_to_one(self, work_dir):
        rulesets, merges = self.sample()
        txt = rulesets[:2] + merges
        mrs = mrs_of([rulesets[0], *merges])
        build_products(work_dir, txt, mrs, manifest_entries=rulesets[:2])
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.stub.calls == []
        with ReleaseRun(work_dir), pytest.raises(manifest.ManifestError) as exc:
            release_handler.verify_products()
        assert "逐一对应" in str(exc.value)

    def test_consistent_products_pass_and_publish(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        with ReleaseRun(work_dir) as run:
            assert run.run() is None
            assert ("release", "create") in run.stub.actions()

    def test_verify_products_counts_baseline(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_entries=rulesets)
        with ReleaseRun(work_dir):
            assert release_handler.verify_products() == len(rulesets) + len(merges)

    def test_baseline_skipped_outside_pipeline_workspace(self, work_dir):
        rulesets, merges = self.sample()
        build_products(work_dir, rulesets + merges, manifest_file=False, rulesets_dir=False)
        with ReleaseRun(work_dir) as run:
            assert run.run() is None
            assert ("release", "create") in run.stub.actions()

    def test_pair_check_still_applies_without_baseline(self, work_dir):
        rulesets, merges = self.sample()
        txt = rulesets[:2] + merges
        build_products(work_dir, txt, mrs_of([rulesets[0], *merges]),
                       manifest_file=False, rulesets_dir=False)
        with ReleaseRun(work_dir) as run:
            assert run.run() == 1
            assert run.stub.calls == []


class TestMergerInputs:
    def test_all_missing_inputs_reported_at_once(self, work_dir, caplog):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "keep.txt", "a.com\n")
        merges = [
            {
                "strategy": "block", "type": "domain", "owner": "rksk", "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/keep.txt", "block/domain/OwnerA/gone1.txt"],
            },
            {
                "strategy": "policy", "type": "domain", "owner": "rksk", "filename": "all-proxy.txt",
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

    def test_present_inputs_merge_and_verify(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write(work_dir / "rulesets" / "products.manifest", "block/domain/OwnerA/ads.txt\n")
        merges = [
            {
                "strategy": "block", "type": "domain", "owner": "rksk", "filename": "all-ads.txt",
                "inputs": ["block/domain/OwnerA/ads.txt"],
            },
        ]
        with MergerRun(work_dir, merges) as run:
            assert run.run() is None
        assert (work_dir / "merged-rules" / "block" / "domain" / "OwnerA" / "ads.txt").is_file()
        assert (work_dir / "merged-rules" / "block" / "domain" / "rksk" / "all-ads.txt").is_file()

    def test_missing_baseline_is_fatal(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

    def test_product_missing_from_baseline_is_fatal(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write(work_dir / "rulesets" / "products.manifest",
              "block/domain/OwnerA/ads.txt\nblock/domain/OwnerA/ghost.txt\n")
        with MergerRun(work_dir, []) as run:
            assert run.run() == 1

    def test_verify_merged_products_uses_baseline(self, work_dir):
        write(work_dir / "rulesets" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        write(work_dir / "rulesets" / "products.manifest", "block/domain/OwnerA/ads.txt\n")
        write(work_dir / "merged-rules" / "block" / "domain" / "OwnerA" / "ads.txt", "a.com\n")
        with MergerRun(work_dir, []):
            assert merger.verify_merged_products([]) is True
