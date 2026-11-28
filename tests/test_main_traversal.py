import hashlib
from pathlib import Path
from unittest.mock import patch

import main
import pytest
from support import write_text as write

SOURCE_URL = "https://github.com/o/r/raw/m/x.txt"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def load_sources(monkeypatch, work_dir, content):
    target = work_dir / "sources.urls"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(main, "SOURCES_FILE", str(target))
    monkeypatch.setattr(main, "RULESETS_DIR", work_dir / "rulesets")
    return main.parse_sources()


def single_task(monkeypatch, work_dir, content):
    return load_sources(monkeypatch, work_dir, content)[0]


class TestStagedCommit:
    def prepare(self, work_dir, monkeypatch):
        root = work_dir / "rulesets"
        staging = root / ".staging-test"
        for name in ("a.txt", "b.txt"):
            write(root / name, f"old-{name}")
            write(staging / name, f"new-{name}")
        monkeypatch.setattr(main, "RULESETS_DIR", root)
        return root, staging, [root / "a.txt", root / "b.txt"]

    def test_first_manifest_write_failure_removes_empty_transaction(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        with patch.object(main, "_write_rollback_manifest", side_effect=OSError("disk full")), pytest.raises(OSError):
            main.commit_staged_products(staging, files)
        assert not list(root.glob(".rollback-*"))
        assert [path.read_text(encoding="utf-8") for path in files] == ["old-a.txt", "old-b.txt"]

    def test_empty_pre_manifest_transaction_is_safe_to_clean(self, work_dir, monkeypatch):
        root = work_dir / "rulesets"
        empty = root / ".rollback-empty"
        empty.mkdir(parents=True)
        main._recover_stale_transactions(root)
        assert not empty.exists()

    def test_partial_manifest_directory_is_preserved(self, work_dir):
        root = work_dir / "rulesets"
        partial = root / ".rollback-partial"
        partial.mkdir(parents=True)
        (partial / "transaction.json").write_text("{")
        with pytest.raises(ValueError, match="无法读取恢复事务"):
            main._recover_stale_transactions(root)
        assert (partial / "transaction.json").is_file()

    def test_unmarked_partial_directory_without_manifest_is_preserved(self, work_dir):
        root = work_dir / "rulesets"
        partial = root / ".rollback-partial"
        partial.mkdir(parents=True)
        write(partial / "0", "old-a.txt")
        with pytest.raises(ValueError, match="无法读取恢复事务"):
            main._recover_stale_transactions(root)
        assert (partial / "0").is_file()

    @pytest.mark.parametrize(
        ("exception_type", "message"),
        [(OSError, "injected replace failure"), (KeyboardInterrupt, None)],
        ids=["os-error", "keyboard-interrupt"],
    )
    def test_interrupted_second_replace_restores_previous_generation(
        self, work_dir, monkeypatch, exception_type, message
    ):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        real_replace = main.os.replace
        target_calls = 0

        def fail_second(src, dst):
            nonlocal target_calls
            if Path(src).parent == staging:
                target_calls += 1
                if target_calls == 2:
                    raise exception_type() if message is None else exception_type(message)
            return real_replace(src, dst)

        with patch.object(main.os, "replace", side_effect=fail_second), pytest.raises(exception_type) as exc:
            main.commit_staged_products(staging, files)

        if message is not None:
            assert str(exc.value) == message
        assert [path.read_text(encoding="utf-8") for path in files] == ["old-a.txt", "old-b.txt"]
        assert not list(root.glob(".rollback-*"))

    def test_new_file_is_removed_when_later_replace_fails(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        new_file = root / "new.txt"
        write(staging / "new.txt", "new")
        files.insert(1, new_file)
        real_replace = main.os.replace
        target_calls = 0

        def fail_last(src, dst):
            nonlocal target_calls
            if Path(src).parent == staging:
                target_calls += 1
                if target_calls == 3:
                    raise OSError("injected replace failure")
            return real_replace(src, dst)

        with patch.object(main.os, "replace", side_effect=fail_last), pytest.raises(OSError):
            main.commit_staged_products(staging, files)

        assert not new_file.exists()
        assert [path.read_text(encoding="utf-8") for path in (root / "a.txt", root / "b.txt")] == [
            "old-a.txt", "old-b.txt"
        ]

    def test_failed_restore_preserves_backups_and_manifest(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        real_replace = main.os.replace
        target_calls = 0

        def fail_commit_and_restore(src, dst):
            nonlocal target_calls
            if Path(src).parent == staging:
                target_calls += 1
                if target_calls == 2:
                    raise OSError("injected replace failure")
            if Path(src).parent.name.startswith(".rollback-") and Path(src).name.isdigit():
                raise OSError("injected restore failure")
            return real_replace(src, dst)

        with patch.object(main.os, "replace", side_effect=fail_commit_and_restore), pytest.raises(OSError):
            main.commit_staged_products(staging, files)

        backups = list(root.glob(".rollback-*"))
        assert len(backups) == 1
        assert (backups[0] / "transaction.json").is_file()
        assert (backups[0] / "0").read_text(encoding="utf-8") == "old-a.txt"

    def test_legacy_transaction_restores_only_a_trusted_backup(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-legacy"
        rollback.mkdir()
        write(root / "a.txt", "partial-new-a")
        write(rollback / "0", "old-a.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True},
        ])

        main._recover_stale_transactions(root)

        assert files[0].read_text(encoding="utf-8") == "old-a.txt"
        assert not rollback.exists()

    def test_legacy_restore_keeps_backup_for_retry_after_a_later_failure(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-legacy"
        rollback.mkdir()
        write(root / "a.txt", "partial-new-a")
        write(rollback / "0", "old-a.txt")
        write(rollback / "1", "old-b.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True},
            {"path": "b.txt", "backup": "1", "existed": True},
        ])
        replace = main.os.replace
        fail = True

        def fail_second(src, dst):
            nonlocal fail
            if fail and Path(src).name.startswith(".restore-") and Path(dst).name == "a.txt":
                raise OSError("restore failed")
            return replace(src, dst)

        monkeypatch.setattr(main.os, "replace", fail_second)
        with pytest.raises(RuntimeError, match="恢复未完成"):
            main._recover_stale_transactions(root)
        assert (rollback / "1").read_text(encoding="utf-8") == "old-b.txt"

        fail = False
        main._recover_stale_transactions(root)
        assert [path.read_text(encoding="utf-8") for path in files] == ["old-a.txt", "old-b.txt"]
        assert not rollback.exists()

    def test_legacy_transaction_missing_backup_is_preserved(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-legacy"
        rollback.mkdir()
        write(root / "a.txt", "partial-new-a")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True},
        ])

        with pytest.raises(RuntimeError, match="恢复未完成"):
            main._recover_stale_transactions(root)

        assert files[0].read_text(encoding="utf-8") == "partial-new-a"
        assert (rollback / "transaction.json").is_file()
        assert not (rollback / "0").exists()

    def test_legacy_transaction_does_not_follow_a_symlink_backup(self, work_dir, monkeypatch):
        root, staging, _files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-legacy"
        rollback.mkdir()
        external = work_dir / "external"
        write(external, "outside")
        (rollback / "0").symlink_to(external)
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True},
        ])

        with pytest.raises(RuntimeError, match="恢复未完成"):
            main._recover_stale_transactions(root)

        assert (root / "a.txt").read_text(encoding="utf-8") == "old-a.txt"
        assert (rollback / "transaction.json").is_file()

    @pytest.mark.parametrize("kind", ["directory", "dangling-symlink"])
    def test_non_regular_target_fails_before_creating_a_transaction(
        self, work_dir, monkeypatch, kind
    ):
        root, staging, _files = self.prepare(work_dir, monkeypatch)
        new_file = root / "new.txt"
        write(staging / "new.txt", "new")
        if kind == "directory":
            new_file.mkdir()
        else:
            new_file.symlink_to(root / "absent")

        with pytest.raises(OSError, match="旧产物不是普通文件"):
            main.commit_staged_products(staging, [new_file])

        assert not list(root.glob(".rollback-*"))
        assert not list(root.glob(".committed-*"))
        if kind == "directory":
            assert new_file.is_dir()
        else:
            assert new_file.is_symlink()



    def test_startup_recovers_interrupted_generation(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-interrupted"
        rollback.mkdir()
        write(root / "a.txt", "partial-new-a")
        write(rollback / "0", "old-a.txt")
        write(rollback / "1", "old-b.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
            {"path": "b.txt", "backup": "1", "existed": True, "sha256": sha("old-b.txt")},
        ])

        main._recover_stale_transactions(root)

        assert [path.read_text(encoding="utf-8") for path in files] == ["old-a.txt", "old-b.txt"]
        assert not rollback.exists()

    def test_startup_restore_failure_keeps_transaction(self, work_dir, monkeypatch):
        root, staging, _files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-failed"
        rollback.mkdir()
        write(rollback / "0", "old-a.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
        ])
        with patch.object(main.os, "replace", side_effect=OSError("restore failed")), pytest.raises(
            RuntimeError, match="恢复未完成"
        ):
            main._recover_stale_transactions(root)
        assert (rollback / "0").is_file()
        assert (rollback / "transaction.json").is_file()
        assert staging.exists()

    @pytest.mark.parametrize("backup_content", [None, "wrong-old-a"], ids=["missing", "corrupt"])
    def test_unproven_restore_preserves_transaction(self, work_dir, monkeypatch, backup_content):
        root, staging, _files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-broken"
        rollback.mkdir()
        write(root / "a.txt", "partial-new-a")
        if backup_content is not None:
            write(rollback / "0", backup_content)
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
        ])

        with pytest.raises(RuntimeError, match="恢复未完成"):
            main._recover_stale_transactions(root)

        assert (root / "a.txt").read_text(encoding="utf-8") == "partial-new-a"
        assert (rollback / "transaction.json").is_file()
        assert staging.exists()

    @pytest.mark.parametrize("backup_content", [None, "wrong-old-a"], ids=["missing", "corrupt"])
    def test_already_restored_target_allows_idempotent_cleanup(
        self, work_dir, monkeypatch, backup_content
    ):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-restored"
        rollback.mkdir()
        if backup_content is not None:
            write(rollback / "0", backup_content)
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
        ])

        main._recover_stale_transactions(root)

        assert files[0].read_text(encoding="utf-8") == "old-a.txt"
        assert not rollback.exists()
        assert not staging.exists()

    def test_sync_lock_rejects_second_writer(self, work_dir, monkeypatch):
        root = work_dir / "rulesets"
        root.mkdir()
        with main._sync_lock(root), pytest.raises(RuntimeError, match="另一个同步进程"), main._sync_lock(root):
            pass
        with main._sync_lock(root):
            assert (root / ".sync.lock").is_file()

    def test_startup_keeps_fully_committed_generation(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-complete"
        rollback.mkdir()
        write(files[0], "new-a.txt")
        write(rollback / "0", "old-a.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
        ], committed=True)

        main._recover_stale_transactions(root)

        assert not rollback.exists()
        assert files[0].read_text(encoding="utf-8") == "new-a.txt"

    def test_startup_committed_transaction_is_marked_before_cleanup(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        rollback = root / ".rollback-complete"
        rollback.mkdir()
        write(rollback / "0", "old-a.txt")
        write(files[0], "new-a.txt")
        main._write_rollback_manifest(rollback, staging, [
            {"path": "a.txt", "backup": "0", "existed": True, "sha256": sha("old-a.txt")},
        ], committed=True)

        remove = main.shutil.rmtree

        def interrupt_cleanup(path, *args, **kwargs):
            if Path(path).name in {".rollback-complete", ".committed-complete"}:
                (Path(path) / "transaction.json").unlink()
                raise OSError("interrupted cleanup")
            return remove(path, *args, **kwargs)

        with patch.object(main.shutil, "rmtree", side_effect=interrupt_cleanup), pytest.raises(
            OSError, match="interrupted cleanup"
        ):
            main._recover_stale_transactions(root)

        main._cleanup_stale_staging(root)
        assert not rollback.exists()
        assert not (root / ".committed-complete").exists()
        assert not staging.exists()
        assert files[0].read_text(encoding="utf-8") == "new-a.txt"


    def test_success_commits_all_files_without_rollback_artifacts(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        main.commit_staged_products(staging, files)
        assert [path.read_text(encoding="utf-8") for path in files] == ["new-a.txt", "new-b.txt"]
        assert not list(root.glob(".rollback-*"))
        assert not list(root.glob(".committed-*"))

    def test_committed_directory_without_manifest_can_finish_cleanup(self, work_dir, monkeypatch):
        root, _staging, files = self.prepare(work_dir, monkeypatch)
        committed = root / ".committed-partial"
        committed.mkdir()
        write(committed / "0", "old-a.txt")

        main._recover_stale_transactions(root)

        assert not committed.exists()
        assert files[0].read_text(encoding="utf-8") == "old-a.txt"

    def test_commit_cleanup_failure_keeps_evidence_for_retry(self, work_dir, monkeypatch):
        root, staging, files = self.prepare(work_dir, monkeypatch)
        real_rmtree = main.shutil.rmtree

        def fail_after_manifest_removal(path, *args, **kwargs):
            target = Path(path)
            if target.name.startswith(".committed-"):
                (target / "transaction.json").unlink()
                raise OSError("injected cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        with patch.object(main.shutil, "rmtree", side_effect=fail_after_manifest_removal), pytest.raises(
            OSError, match="injected cleanup failure"
        ):
            main.commit_staged_products(staging, files)

        assert [path.read_text(encoding="utf-8") for path in files] == ["new-a.txt", "new-b.txt"]
        assert len(list(root.glob(".committed-*"))) == 1
        main._cleanup_stale_staging(root)
        assert not list(root.glob(".committed-*"))
        assert not staging.exists()
        assert [path.read_text(encoding="utf-8") for path in files] == ["new-a.txt", "new-b.txt"]


class TestMarkerWhitelistRejected:
    def test_policy_parent_traversal_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir, f"[policy:../../../etc]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_policy_absolute_path_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir, f"[policy:/tmp/evil]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_policy_drive_path_rejected(self, monkeypatch, work_dir):
        for marker in ("[policy:C:/Windows/Temp]", "[policy:C:Windows]", "[policy:u:x]"):
            with pytest.raises(SystemExit) as exc:
                load_sources(monkeypatch, work_dir, f"{marker}\n{SOURCE_URL}\n")
            assert exc.value.code == 1, marker

    def test_policy_leading_dot_and_bare_dotdot_rejected(self, monkeypatch, work_dir):
        for marker in ("[policy:.hidden]", "[policy:..]", "[policy:block/../direct]"):
            with pytest.raises(SystemExit):
                load_sources(monkeypatch, work_dir, f"{marker}\n{SOURCE_URL}\n")

    def test_type_parent_traversal_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir, f"[type:../../..]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_type_drive_path_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit):
            load_sources(monkeypatch, work_dir, f"[type:C:/win]\n{SOURCE_URL}\n")

    def test_valid_markers_still_accepted(self, monkeypatch, work_dir):
        tasks = load_sources(
            monkeypatch,
            work_dir,
            "\n".join(
                [
                    "[policy:reject]",
                    "[type:ipcidr]",
                    SOURCE_URL,
                    "[policy:direct]",
                    "[type:domain]",
                    SOURCE_URL,
                    "[policy:custom-list]",
                    SOURCE_URL,
                ]
            )
            + "\n",
        )
        assert [t["policy"] for t in tasks] == ["block", "direct", "custom-list"]
        assert [t["type"] for t in tasks] == ["ipcidr", "domain", "domain"]


class TestUrlDerivedComponents:
    def test_userinfo_host_cannot_become_drive(self, monkeypatch, work_dir):
        # 含 userinfo/非 443 的来源已在解析阶段被拒绝（见 test_main）；路径派生仍只按 hostname
        clean_task = single_task(monkeypatch, work_dir, "https://evil.com/x.txt\n")
        task = {**clean_task, "url": "https://u:p@evil.com:8443/x.txt"}
        owner, name, rel, abs_path = main.build_filepath(task)
        assert (owner, name) == ("evil.com", "x.txt")
        assert rel.as_posix() == "policy/domain/evil.com/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())
        assert main.ensure_within_rulesets(abs_path) == Path(abs_path).resolve()

    def test_dotdot_path_segments_stay_inside(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://evil.com/a/../../x.txt\n")
        _owner, _name, rel, abs_path = main.build_filepath(task)
        assert rel.as_posix() == "policy/domain/evil.com/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())

    def test_dotdot_filename_falls_back(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://evil.com/dir/..\n")
        _owner, name, rel, abs_path = main.build_filepath(task)
        assert name == "rules.txt"
        assert rel.as_posix() == "policy/domain/evil.com/rules.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())

    def test_collision_slug_is_sanitised(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://github.com/u:x/repo/raw/m/x.txt\n")
        plan = {str(main.Path("policy/domain/u_x/x.txt")): ["u:x__repo"]}
        owner, _name, rel, abs_path = main.build_filepath(task, plan, {})
        assert owner == "u_x__repo"
        assert rel.as_posix() == "policy/domain/u_x__repo/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())


class TestNormalPaths:
    def test_normal_source_lands_where_expected(self, monkeypatch, work_dir):
        task = single_task(
            monkeypatch,
            work_dir,
            "\n".join(
                [
                    "[policy:block]",
                    "[type:domain]",
                    "https://github.com/Loyalsoldier/clash-rules/raw/r/reject.txt",
                ]
            )
            + "\n",
        )
        owner, name, rel, abs_path = main.build_filepath(task)
        assert (owner, name) == ("Loyalsoldier", "reject.txt")
        assert rel.as_posix() == "block/domain/Loyalsoldier/reject.txt"
        assert abs_path == main.RULESETS_DIR / rel

    def test_real_sources_file_unchanged(self, monkeypatch, work_dir):
        monkeypatch.setattr(main, "SOURCES_FILE", "sources.urls")
        tasks = main.parse_sources()
        assert len(tasks) > 5
        for task in tasks:
            assert task["policy"] in ("block", "direct", "policy")
            assert task["type"] in ("domain", "ipcidr")
            rel = main._base_rel_path(task)
            assert ".." not in rel.parts


class TestLandingAssertion:
    def test_out_of_tree_path_rejected(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        with pytest.raises(SystemExit) as exc:
            main.ensure_within_rulesets(work_dir / "escaped" / "x.txt")
        assert exc.value.code == 1

    def test_absolute_outside_paths_rejected(self, monkeypatch, work_dir):
        monkeypatch.setattr(main, "RULESETS_DIR", work_dir / "rulesets")
        for target in ("C:/Windows/Temp/x.txt", "U:/p@evil.com:8443/x.txt", "\\tmp\\evil\\x.txt", "../outside/x.txt"):
            with pytest.raises(SystemExit):
                main.ensure_within_rulesets(target)

    def test_inside_rulesets_allowed(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        target = rulesets / "policy" / "domain" / "x.txt"
        assert main.ensure_within_rulesets(target) == target.resolve()
        assert main.ensure_within_rulesets(rulesets) == rulesets.resolve()

    def test_process_group_refuses_to_write_outside(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        escaped = work_dir / "escaped" / "x.txt"
        group = {
            "path": str(escaped),
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(0, {"url": SOURCE_URL})],
            "sources": [SOURCE_URL],
        }
        with pytest.raises(SystemExit) as exc:
            main.process_group(group, {0: b"example.com\n"})
        assert exc.value.code == 1
        assert not escaped.exists()

    def test_process_group_still_writes_inside(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        target = rulesets / "block" / "domain" / "o" / "x.txt"
        group = {
            "path": str(target),
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(0, {"url": SOURCE_URL})],
            "sources": [SOURCE_URL],
        }
        count, errors = main.process_group(group, {0: b"example.com\nsub.example.org\n"})
        assert count == 2
        assert errors == {"download": [], "parse": []}
        assert target.read_text(encoding="utf-8").splitlines() == [
            f"# Source: {SOURCE_URL}",
            "example.com",
            "sub.example.org",
        ]
