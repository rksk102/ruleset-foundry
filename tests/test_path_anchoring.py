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
            "+.example.com\n", encoding="utf-8"
        )
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / "rulesets"))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

        with pytest.raises(manifest.ManifestError):
            release_handler.verify_products()

    def test_paired_products_without_baseline_skip_the_absolute_check(self, work_dir, monkeypatch):
        for folder, suffix in ((work_dir / "merged-rules", ".txt"), (work_dir / "merged-rules-mrs", ".mrs")):
            (folder / "block" / "domain" / "Owner").mkdir(parents=True)
            (folder / "block" / "domain" / "Owner" / f"a{suffix}").write_text("x", encoding="utf-8")
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / "rulesets"))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

        assert release_handler.verify_products() is None


class TestBaselineRequirement:
    """ISS-16：显式 require_baseline 时，自定义 rulesets_dir 缺失不得静默降级为告警。"""

    def paired_products(self, work_dir):
        for folder, suffix in ((work_dir / "merged-rules", ".txt"), (work_dir / "merged-rules-mrs", ".mrs")):
            (folder / "block" / "domain" / "Owner").mkdir(parents=True, exist_ok=True)
            (folder / "block" / "domain" / "Owner" / f"a{suffix}").write_text("x", encoding="utf-8")

    def patch_dirs(self, monkeypatch, work_dir, rulesets="custom-rulesets"):
        monkeypatch.setattr(release_handler, "RULESETS_DIR", str(work_dir / rulesets))
        monkeypatch.setattr(release_handler, "MERGED_DIR", str(work_dir / "merged-rules"))
        monkeypatch.setattr(release_handler, "MRS_DIR", str(work_dir / "merged-rules-mrs"))

    def test_missing_custom_rulesets_dir_is_fatal_when_required(self, work_dir, monkeypatch):
        self.paired_products(work_dir)
        self.patch_dirs(monkeypatch, work_dir)

        with pytest.raises(manifest.ManifestError):
            release_handler.verify_products(require_baseline=True)

    def test_missing_custom_rulesets_dir_degrades_by_default(self, work_dir, monkeypatch):
        self.paired_products(work_dir)
        self.patch_dirs(monkeypatch, work_dir)

        assert release_handler.verify_products() is None

    def test_empty_custom_rulesets_dir_is_fatal_when_required(self, work_dir, monkeypatch):
        self.paired_products(work_dir)
        (work_dir / "custom-rulesets").mkdir()
        self.patch_dirs(monkeypatch, work_dir)

        with pytest.raises(manifest.ManifestError, match=release_handler.BASELINE_MISSING):
            release_handler.verify_products(require_baseline=True)

    def test_no_products_fails_on_baseline_when_required(self, work_dir, monkeypatch):
        """没有产物时也必须先撞上基线缺失，而不是先报空跑。"""
        self.patch_dirs(monkeypatch, work_dir)

        with pytest.raises(manifest.ManifestError, match=release_handler.BASELINE_MISSING):
            release_handler.verify_products(require_baseline=True)


class TestRepoRootAnchor:
    def test_anchor_helper_chdirs_to_repo_root(self, work_dir, monkeypatch):
        original = os.getcwd()
        monkeypatch.chdir(work_dir)
        try:
            utils.anchor_cwd_to_repo_root()
            assert Path.cwd() == REPO
        finally:
            os.chdir(original)


class TestRepoSubdirProtection:
    def test_protected_top_level_is_rejected(self):
        assert utils.is_repo_subdir(REPO / "scripts") is False
        assert utils.is_repo_subdir(REPO / "merged-rules") is True

    def test_symlink_to_protected_dir_is_rejected(self, work_dir):
        link = work_dir / "link"
        try:
            link.symlink_to(REPO / "scripts", target_is_directory=True)
        except OSError as e:
            pytest.skip(f"symlink unavailable: {e}")
        assert utils.is_repo_subdir(link) is False
