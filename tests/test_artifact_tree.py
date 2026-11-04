import os
import subprocess
import sys
from pathlib import Path

import artifact_tree
import manifest
import pytest

REPO = Path(__file__).resolve().parent.parent
PRODUCT_TXT = "merged-rules"
PRODUCT_MRS = "merged-rules-mrs"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit_all(repo):
    git(repo, "add", "-f", ".")
    git(repo, "commit", "-q", "-m", "products")


def run(*extra, txt=PRODUCT_TXT, mrs=PRODUCT_MRS, ref="HEAD"):
    return artifact_tree.main(["--ref", ref, "--txt-dir", txt, "--mrs-dir", mrs, *extra])


@pytest.fixture
def repo(work_dir):
    root = work_dir / "repo"
    (root / PRODUCT_TXT).mkdir(parents=True)
    (root / PRODUCT_MRS).mkdir()
    (root / PRODUCT_TXT / "a.txt").write_text("1.1.1.1\n", encoding="utf-8")
    (root / PRODUCT_MRS / "a.mrs").write_bytes(b"\x01\x02\x03")
    (root / ".gitignore").write_text(f"{PRODUCT_TXT}/\n{PRODUCT_MRS}/\n", encoding="utf-8")
    (root / ".product-sha256").write_text(f"{'a' * 64}|{'b' * 64}|1|1\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    commit_all(root)
    return root


def test_identical_products_match(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert run() == 0


def test_root_control_files_are_allowed(repo, monkeypatch):
    monkeypatch.chdir(repo)
    # 控制文件不在产物目录内，只有被显式放行时才不计为“分支多出”。
    entries = artifact_tree.list_tree("HEAD")
    local = artifact_tree.collect_local_files((PRODUCT_TXT, PRODUCT_MRS), repo)
    controls = artifact_tree._allowed_controls(".product-sha256")
    assert artifact_tree.compare_tree(entries, local, artifact_tree.object_format(), controls) == []
    assert artifact_tree.compare_tree(
        entries, local, artifact_tree.object_format(), controls - {".gitignore"}
    )


def test_manifest_name_matches_canonical_manifest_module():
    assert artifact_tree.MANIFEST_NAME == manifest.MANIFEST_NAME


def _problems(repo):
    return artifact_tree.compare_tree(
        artifact_tree.list_tree("HEAD"),
        artifact_tree.published_local_files(PRODUCT_TXT, PRODUCT_MRS, repo),
        artifact_tree.object_format(),
        artifact_tree._allowed_controls(".product-sha256"),
    )


def test_local_product_manifest_is_excluded_but_preserved(repo, monkeypatch):
    manifest_file = repo / PRODUCT_TXT / artifact_tree.MANIFEST_NAME
    manifest_file.write_text("local manifest\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert _problems(repo) == []
    assert run() == 0
    assert manifest_file.read_text(encoding="utf-8") == "local manifest\n"


def test_legacy_remote_product_manifest_triggers_one_cleanup_repush(repo, monkeypatch):
    manifest_file = repo / PRODUCT_TXT / artifact_tree.MANIFEST_NAME
    manifest_file.write_text("local manifest\n", encoding="utf-8")
    commit_all(repo)
    monkeypatch.chdir(repo)
    assert any(
        artifact_tree.MANIFEST_NAME in problem and "多出" in problem for problem in _problems(repo)
    )
    assert run() == artifact_tree.MISMATCH_EXIT
    assert manifest_file.exists()

    # 新版暂存不再提交清单：清理提交之后应稳定判定为一致
    git(repo, "rm", "-q", "--cached", f"{PRODUCT_TXT}/{artifact_tree.MANIFEST_NAME}")
    git(repo, "commit", "-q", "-m", "cleanup manifest")
    assert manifest_file.exists()
    assert _problems(repo) == []
    assert run() == 0


def test_nested_product_manifest_is_still_compared(repo, monkeypatch):
    nested = repo / PRODUCT_TXT / "sub" / artifact_tree.MANIFEST_NAME
    nested.parent.mkdir()
    nested.write_text("nested-1\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT  # 分支缺少嵌套同名清单
    commit_all(repo)
    assert run() == 0
    nested.write_text("nested-2\n", encoding="utf-8")
    assert run() == artifact_tree.MISMATCH_EXIT  # 嵌套同名文件仍逐字节对账


def test_mrs_product_manifest_is_not_excluded(repo, monkeypatch):
    (repo / PRODUCT_MRS / artifact_tree.MANIFEST_NAME).write_text("mrs\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_nested_control_file_is_not_allowed(repo, monkeypatch):
    (repo / PRODUCT_TXT / ".gitignore").write_text("*\n", encoding="utf-8")
    commit_all(repo)
    (repo / PRODUCT_TXT / ".gitignore").unlink()
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_txt_comment_drift_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_TXT / "a.txt").write_text("1.1.1.1\n# 注释漂移\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_same_size_byte_drift_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_MRS / "a.mrs").write_bytes(b"\x01\x02\x04")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_deleted_local_product_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_MRS / "a.mrs").unlink()
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_missing_branch_product_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_TXT / "b.txt").write_text("2.2.2.2\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_extra_branch_file_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_TXT / "extra.txt").write_text("9.9.9.9\n", encoding="utf-8")
    commit_all(repo)
    (repo / PRODUCT_TXT / "extra.txt").unlink()
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_branch_symlink_is_rejected(repo, monkeypatch):
    (repo / PRODUCT_TXT / "link.txt").symlink_to("a.txt")
    commit_all(repo)
    (repo / PRODUCT_TXT / "link.txt").unlink()
    monkeypatch.chdir(repo)
    problems = artifact_tree.compare_tree(
        artifact_tree.list_tree("HEAD"),
        artifact_tree.collect_local_files((PRODUCT_TXT, PRODUCT_MRS), repo),
        artifact_tree.object_format(),
        artifact_tree._allowed_controls(".product-sha256"),
    )
    assert any("link.txt" in problem and "120000" in problem for problem in problems)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_branch_gitlink_is_rejected(repo, monkeypatch):
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'c' * 40},merged-rules/vendor")
    git(repo, "commit", "-q", "-m", "gitlink")
    monkeypatch.chdir(repo)
    problems = artifact_tree.compare_tree(
        artifact_tree.list_tree("HEAD"),
        artifact_tree.collect_local_files((PRODUCT_TXT, PRODUCT_MRS), repo),
        artifact_tree.object_format(),
        artifact_tree._allowed_controls(".product-sha256"),
    )
    assert any("vendor" in problem and "160000" in problem for problem in problems)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_control_file_type_is_rejected(repo, monkeypatch):
    git(repo, "rm", "-q", "--cached", ".product-sha256")
    (repo / ".product-sha256").unlink()
    (repo / ".product-sha256").symlink_to(".gitignore")
    commit_all(repo)
    monkeypatch.chdir(repo)
    problems = artifact_tree.compare_tree(
        artifact_tree.list_tree("HEAD"),
        artifact_tree.collect_local_files((PRODUCT_TXT, PRODUCT_MRS), repo),
        artifact_tree.object_format(),
        artifact_tree._allowed_controls(".product-sha256"),
    )
    assert any(".product-sha256" in problem and "控制文件" in problem for problem in problems)


def test_custom_marker_is_respected(repo, monkeypatch):
    git(repo, "rm", "-q", ".product-sha256")
    (repo / "alt.sha256").write_text("marker\n", encoding="utf-8")
    commit_all(repo)
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT
    assert run("--marker", "alt.sha256") == 0


def test_empty_local_products_is_mismatch(repo, monkeypatch):
    (repo / PRODUCT_TXT / "a.txt").unlink()
    (repo / PRODUCT_MRS / "a.mrs").unlink()
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.MISMATCH_EXIT


def test_unknown_ref_is_fatal(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert run(ref="does-not-exist") == artifact_tree.ERROR_EXIT


def test_missing_local_dir_is_fatal(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert run(mrs="missing-mrs") == artifact_tree.ERROR_EXIT


def test_local_symlink_is_fatal(repo, monkeypatch):
    (repo / PRODUCT_TXT / "link.txt").symlink_to("a.txt")
    monkeypatch.chdir(repo)
    assert run() == artifact_tree.ERROR_EXIT


def test_invalid_marker_path_is_fatal(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert run("--marker", "../escape") == artifact_tree.ERROR_EXIT


def test_fetch_head_ref_matches_pipeline_usage(repo, monkeypatch):
    (repo / PRODUCT_TXT / "a.txt").write_text("1.1.1.1\n# drift\n", encoding="utf-8")
    git(repo, "fetch", "--no-tags", ".", "HEAD")
    monkeypatch.chdir(repo)
    assert run(ref="FETCH_HEAD") == artifact_tree.MISMATCH_EXIT


def test_cli_exit_codes(repo, monkeypatch):
    monkeypatch.chdir(repo)
    env = {**os.environ, "PYTHONPATH": str(REPO / "scripts")}
    command = [
        sys.executable,
        str(REPO / "scripts" / "artifact_tree.py"),
        "--ref",
        "HEAD",
        "--txt-dir",
        PRODUCT_TXT,
        "--mrs-dir",
        PRODUCT_MRS,
    ]
    matched = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env)
    assert matched.returncode == 0, matched.stderr
    (repo / PRODUCT_TXT / "a.txt").write_text("1.1.1.1\n# drift\n", encoding="utf-8")
    mismatched = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env)
    assert mismatched.returncode == artifact_tree.MISMATCH_EXIT
    failing = subprocess.run(
        [*command[:3], "does-not-exist", *command[4:]],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert failing.returncode == artifact_tree.ERROR_EXIT
    assert "::error::" in failing.stderr


def _fake_git(output):
    return lambda *args: output


@pytest.mark.parametrize(
    "record",
    (
        "100644 blob deadbeef 12",
        "100644 blob deadbeef 12 extra\tmerged-rules/a.txt",
        "100644 blob deadbeef abc\tmerged-rules/a.txt",
    ),
)
def test_malformed_tree_records_are_fatal(record, monkeypatch):
    monkeypatch.setattr(artifact_tree, "_git", _fake_git(record))
    with pytest.raises(artifact_tree.TreeError):
        artifact_tree.list_tree("HEAD")


def test_unsupported_object_format_is_fatal(monkeypatch):
    monkeypatch.setattr(artifact_tree, "_git", _fake_git("sha512"))
    with pytest.raises(artifact_tree.TreeError):
        artifact_tree.object_format()


def test_empty_repository_root_is_fatal(monkeypatch):
    monkeypatch.setattr(artifact_tree, "_git", _fake_git(""))
    with pytest.raises(artifact_tree.TreeError):
        artifact_tree.repository_root()


def test_git_execution_failure_is_fatal(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("git 不可用")

    monkeypatch.setattr(artifact_tree.subprocess, "run", boom)
    with pytest.raises(artifact_tree.TreeError):
        artifact_tree.list_tree("HEAD")


def test_local_directory_symlink_is_fatal(repo, monkeypatch, work_dir):
    (work_dir / "outside").mkdir()
    (repo / "linked").symlink_to(work_dir / "outside")
    monkeypatch.chdir(repo)
    assert run(txt="linked") == artifact_tree.ERROR_EXIT
