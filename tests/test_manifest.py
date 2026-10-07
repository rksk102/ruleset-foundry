import manifest
import pytest


class TestSaveLoad:
    def test_roundtrip(self, work_dir):
        path = work_dir / "state" / "products.txt"
        manifest.save_manifest(path, ["b/2.txt", "a/1.txt"])
        assert manifest.load_manifest(path) == ["a/1.txt", "b/2.txt"]

    def test_no_temp_file_left_behind(self, work_dir):
        path = work_dir / "products.txt"
        manifest.save_manifest(path, ["a/1.txt"])
        assert [p.name for p in work_dir.iterdir()] == ["products.txt"]

    def test_missing_returns_none(self, work_dir):
        assert manifest.load_manifest(work_dir / "nope.txt") is None

    def test_comments_and_blanks_ignored(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text("# header\n\na.txt\n\nb.txt\n", encoding="utf-8")
        assert manifest.load_manifest(path) == ["a.txt", "b.txt"]

    def test_backslashes_normalised(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text("block\\domain\\a.txt\n", encoding="utf-8")
        assert manifest.load_manifest(path) == ["block/domain/a.txt"]


class TestCollect:
    def test_collects_txt_recursively(self, work_dir):
        (work_dir / "block" / "domain").mkdir(parents=True)
        (work_dir / "block" / "domain" / "a.txt").write_text("x\n", encoding="utf-8")
        (work_dir / "policy").mkdir()
        (work_dir / "policy" / "b.txt").write_text("x\n", encoding="utf-8")
        (work_dir / "policy" / ".hidden.txt").write_text("x\n", encoding="utf-8")
        (work_dir / "policy" / "c.md").write_text("x\n", encoding="utf-8")
        assert manifest.collect_files(work_dir) == {"block/domain/a.txt", "policy/b.txt"}

    def test_missing_root_returns_empty(self, work_dir):
        assert manifest.collect_files(work_dir / "nope") == set()

    def test_mrs_suffix(self, work_dir):
        (work_dir / "x.mrs").write_text("x", encoding="utf-8")
        (work_dir / "y.txt").write_text("y", encoding="utf-8")
        assert manifest.collect_files(work_dir, ".mrs") == {"x.mrs"}


class TestDiffAndVerify:
    def test_diff_reports_both_directions(self):
        missing, extra = manifest.diff_against(["a.txt", "b.txt"], ["a.txt", "c.txt"])
        assert missing == ["b.txt"]
        assert extra == ["c.txt"]

    def test_verify_passes_on_match(self):
        assert manifest.verify_matches(["a.txt"], ["a.txt"], "sync") is True

    def test_verify_raises_when_missing_baseline(self):
        with pytest.raises(manifest.ManifestError):
            manifest.verify_matches(None, ["a.txt"], "sync")

    def test_verify_raises_on_missing_entry(self):
        with pytest.raises(manifest.ManifestError) as exc:
            manifest.verify_matches(["a.txt", "b.txt"], ["a.txt"], "publish")
        assert "缺失 1" in str(exc.value)

    def test_verify_raises_on_extra_entry(self):
        with pytest.raises(manifest.ManifestError) as exc:
            manifest.verify_matches(["a.txt"], ["a.txt", "z.txt"], "publish")
        assert "多出 1" in str(exc.value)


class TestMergeInputs:
    def test_all_present_returns_empty(self, work_dir):
        (work_dir / "x.txt").write_text("x\n", encoding="utf-8")
        tasks = [{"inputs": ["x.txt"]}]
        assert manifest.merge_inputs(tasks, work_dir) == []

    def test_missing_reported(self, work_dir):
        tasks = [{"inputs": ["x.txt", "y.txt"]}, {"inputs": ["y.txt"]}]
        assert manifest.merge_inputs(tasks, work_dir) == ["x.txt", "y.txt"]

    def test_empty_tasks(self, work_dir):
        assert manifest.merge_inputs([], work_dir) == []
        assert manifest.merge_inputs(None, work_dir) == []
