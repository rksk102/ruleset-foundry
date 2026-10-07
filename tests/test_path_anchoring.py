"""路径基准与空跑门禁：产物校验不得在没有产物时通过，路径不依赖调用者目录。"""
import os
from pathlib import Path

import manifest
import pytest
import release_handler
import utils

REPO = Path(__file__).resolve().parent.parent


class TestVerifyFailsClosed:
    def test_no_products_at_all_is_a_failure(self, work_dir, monkeypatch):
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / "rulesets"))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

        with pytest.raises(manifest.ManifestError):
            release_handler.verify_products()

    def test_unpaired_products_are_a_failure(self, work_dir, monkeypatch):
        (work_dir / "merged-rules" / "block" / "domain" / "Owner").mkdir(parents=True)
        (work_dir / "merged-rules" / "block" / "domain" / "Owner" / "a.txt").write_text(
            "+.example.com\n", encoding="utf-8")
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / "rulesets"))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

        with pytest.raises(manifest.ManifestError):
            release_handler.verify_products()

    def test_paired_products_without_baseline_skip_the_absolute_check(
            self, work_dir, monkeypatch):
        for folder, suffix in ((work_dir / "merged-rules", ".txt"),
                               (work_dir / "merged-rules-mrs", ".mrs")):
            (folder / "block" / "domain" / "Owner").mkdir(parents=True)
            (folder / "block" / "domain" / "Owner" / f"a{suffix}").write_text("x", encoding="utf-8")
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / "rulesets"))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

        assert release_handler.verify_products() is None


class TestRepoRootAnchor:
    def test_repo_root_does_not_follow_cwd(self):
        assert utils.REPO_ROOT == REPO
        assert Path(release_handler.REPO_ROOT) == REPO

    def test_anchor_helper_chdirs_to_repo_root(self, work_dir, monkeypatch):
        original = os.getcwd()
        monkeypatch.chdir(work_dir)
        try:
            utils.anchor_cwd_to_repo_root()
            assert Path.cwd() == REPO
        finally:
            os.chdir(original)
