import hashlib

import manifest
import pytest

H1 = hashlib.sha256(b"1").hexdigest()
H2 = hashlib.sha256(b"2").hexdigest()


class TestGeneration:
    def test_ci_generation_includes_run_attempt(self, monkeypatch):
        monkeypatch.setenv("GITHUB_RUN_ID", "123")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        first = manifest.current_generation()
        assert first == manifest.expected_generation()

        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
        assert manifest.current_generation() == manifest.expected_generation()
        assert manifest.current_generation() != first

    def test_local_generation_behavior_is_unchanged(self, monkeypatch):
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        monkeypatch.setattr(manifest, "beijing_timestamp", lambda: "2026-10-31 12:00:00")
        assert manifest.current_generation().startswith("local-2026-10-31 12:00:00-")
        assert manifest.expected_generation() is None


class TestSaveLoad:
    def test_roundtrip(self, work_dir):
        path = work_dir / "state" / "products.txt"
        manifest.save_manifest(path, {"b/2.txt": H2, "a/1.txt": H1}, generation="g-1")
        loaded = manifest.load_manifest(path)
        assert loaded.generation == "g-1"
        assert loaded.entries == {"a/1.txt": H1, "b/2.txt": H2}
        assert loaded.paths == ("a/1.txt", "b/2.txt")

    def test_no_temp_file_left_behind(self, work_dir):
        path = work_dir / "products.txt"
        manifest.save_manifest(path, {"a/1.txt": H1})
        assert [p.name for p in work_dir.iterdir()] == ["products.txt"]

    def test_missing_returns_none(self, work_dir):
        assert manifest.load_manifest(work_dir / "nope.txt") is None

    def test_comments_and_blanks_ignored(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text(
            f"{manifest.MANIFEST_MAGIC}\n# generation: g\n\n{H1}  a.txt\n\n{H2}  b.txt\n",
            encoding="utf-8",
        )
        assert manifest.load_manifest(path).entries == {"a.txt": H1, "b.txt": H2}

    def test_backslashes_normalised(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text(f"{manifest.MANIFEST_MAGIC}\n# generation: g\n{H1}  block\\domain\\a.txt\n", encoding="utf-8")
        assert list(manifest.load_manifest(path).entries) == ["block/domain/a.txt"]

    def test_old_format_is_rejected(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text("block/domain/a.txt\n", encoding="utf-8")
        with pytest.raises(manifest.ManifestError, match="不支持"):
            manifest.load_manifest(path)

    def test_missing_generation_is_rejected(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text(f"{manifest.MANIFEST_MAGIC}\n{H1}  a.txt\n", encoding="utf-8")
        with pytest.raises(manifest.ManifestError, match="generation"):
            manifest.load_manifest(path)

    def test_duplicate_entry_is_rejected(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text(
            f"{manifest.MANIFEST_MAGIC}\n# generation: g\n{H1}  a.txt\n{H2}  a.txt\n",
            encoding="utf-8",
        )
        with pytest.raises(manifest.ManifestError, match="重复"):
            manifest.load_manifest(path)

    def test_traversal_entry_is_rejected(self, work_dir):
        path = work_dir / "m.txt"
        path.write_text(f"{manifest.MANIFEST_MAGIC}\n# generation: g\n{H1}  ../a.txt\n", encoding="utf-8")
        with pytest.raises(manifest.ManifestError, match="越界"):
            manifest.load_manifest(path)

    @pytest.mark.parametrize("rel", ["../a.txt", "C:/a.txt", r"C:\a.txt", "/a.txt", "a//b.txt", "a/b/"])
    def test_save_rejects_unsafe_relative_paths(self, work_dir, rel):
        path = work_dir / "m.txt"
        with pytest.raises(manifest.ManifestError, match="路径"):
            manifest.save_manifest(path, {rel: H1}, generation="g")
        assert not path.exists()

    @pytest.mark.parametrize("rel", ["C:/a.txt", r"C:\a.txt", "/a.txt", "a//b.txt", r"a\\b.txt"])
    def test_load_rejects_unsafe_relative_paths(self, work_dir, rel):
        path = work_dir / "m.txt"
        path.write_text(f"{manifest.MANIFEST_MAGIC}\n# generation: g\n{H1}  {rel}\n", encoding="utf-8")
        with pytest.raises(manifest.ManifestError, match="路径"):
            manifest.load_manifest(path)


class TestCollect:
    def test_collects_txt_recursively(self, work_dir):
        (work_dir / "block" / "domain").mkdir(parents=True)
        (work_dir / "block" / "domain" / "a.txt").write_text("x\n", encoding="utf-8")
        (work_dir / "policy").mkdir()
        (work_dir / "policy" / "b.txt").write_text("x\n", encoding="utf-8")
        (work_dir / "policy" / ".hidden.txt").write_text("x\n", encoding="utf-8")
        (work_dir / ".hidden").mkdir()
        (work_dir / ".hidden" / "a.txt").write_text("x\n", encoding="utf-8")
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

    def test_verify_manifest_checks_hashes(self, work_dir):
        (work_dir / "a.txt").write_text("a\n", encoding="utf-8")
        m = manifest.Manifest("g", {"a.txt": H1})
        with pytest.raises(manifest.ManifestError, match="哈希"):
            manifest.verify_manifest(m, work_dir, "ctx")

    def test_verify_manifest_accepts_matching_hash(self, work_dir):
        (work_dir / "a.txt").write_text("a\n", encoding="utf-8")
        digest = manifest.file_sha256(str(work_dir / "a.txt"))
        m = manifest.Manifest("g", {"a.txt": digest})
        assert manifest.verify_manifest(m, work_dir, "ctx") is True


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
