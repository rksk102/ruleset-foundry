import datetime
import json
import os
import sys
import zipfile

import pytest
import release_handler


def release_listing(*releases):
    return json.dumps(list(releases))


EMPTY_LISTING = release_listing()

VIEW_FOUND = json.dumps({"tagName": "rules-x"})


class GhStub:
    """按子命令返回合理默认值：release list 必须是可解析的 JSON。"""

    def __init__(self, fail_when=None, responses=None, asset_count="3"):
        self.calls = []
        self.fail_when = fail_when or (lambda cmd: False)
        self.responses = responses or {}
        self.asset_count = asset_count

    def __call__(self, cmd, fail_fast=False):
        self.calls.append(list(cmd))
        if self.fail_when(cmd):
            return None
        key = (cmd[0], cmd[1]) if len(cmd) > 1 else (cmd[0],)
        if key in self.responses:
            return self.responses[key]
        if key == ("release", "list"):
            return EMPTY_LISTING
        if key == ("release", "view"):
            if "assets" in cmd:
                return self.asset_count
            return VIEW_FOUND
        return "ok"

    def commands(self):
        return [" ".join(c) for c in self.calls]

    def release_actions(self):
        return [c[1] for c in self.calls if c[0] == "release" and len(c) > 1]


def run_with_stub(stub, fn):
    original = release_handler.run_gh
    release_handler.run_gh = stub
    try:
        return fn()
    finally:
        release_handler.run_gh = original


ZIP = "merged-rules-2026-10-05.zip"
NOTES = "## notes"


class TestShouldPublish:
    def test_disabled_always_publishes(self):
        publish, why = release_handler.should_publish("h", "h", False)
        assert publish is True
        assert "关闭" in why

    def test_first_publish_when_no_last_hash(self):
        publish, why = release_handler.should_publish("h", None, True)
        assert publish is True
        assert "首次" in why

    def test_empty_string_last_hash_treated_as_first(self):
        publish, _why = release_handler.should_publish("h", "", True)
        assert publish is True

    def test_changed_hash_publishes(self):
        publish, why = release_handler.should_publish("new", "old", True)
        assert publish is True
        assert "变化" in why

    def test_same_hash_skips(self):
        publish, why = release_handler.should_publish("same", "same", True)
        assert publish is False
        assert "无变化" in why


class TestGenerateReleaseNotes:
    def notes(self, txt=1, mrs=1):
        manifest = {
            "merged-rules": [f"merged-rules/a{i}.txt" for i in range(txt)],
            "merged-rules-mrs": [f"merged-rules-mrs/b{i}.mrs" for i in range(mrs)],
        }
        return release_handler.generate_release_notes("2026-10-05", "06:12:00", manifest)

    def test_counts_render(self):
        out = self.notes(2, 3)
        assert "**2**" in out and "**3**" in out and "**5**" in out

    def test_tag_date_and_time_included(self):
        out = self.notes()
        assert "2026-10-05" in out and "06:12:00" in out

    def test_missing_manifest_sections_count_zero(self):
        out = release_handler.generate_release_notes("2026-10-05", "00:00:00", {})
        assert "**0**" in out

    def test_file_details_listed(self):
        out = self.notes(2, 0)
        assert "merged-rules/a0.txt" in out
        assert "merged-rules/a1.txt" in out

    def test_counts_follow_configured_dirs(self, monkeypatch):
        monkeypatch.setattr(release_handler, "MERGED_DIR", "txt-out")
        monkeypatch.setattr(release_handler, "MRS_DIR", "mrs-out")
        out = release_handler.generate_release_notes(
            "2026-10-05", "06:12:00",
            {"txt-out": ["txt-out/a.txt"], "mrs-out": ["mrs-out/b.mrs"]})
        assert "| 文本规则 | `txt-out` | **1** |" in out
        assert "| MRS 规则 | `mrs-out` | **1** |" in out
        assert "**2**" in out

    def test_empty_manifest_does_not_crash(self):
        out = release_handler.generate_release_notes("2026-10-05", "00:00:00",
                                                     {"merged-rules": [], "merged-rules-mrs": []})
        assert isinstance(out, str) and out


def posix_paths(paths):
    return [p.replace("\\", "/") for p in paths]


class TestZipTargetFiles:
    def isolate(self, work_dir):
        """REPO_ROOT 必须是相对路径：zip 的相对名由它算出来。"""
        original = (release_handler.REPO_ROOT, release_handler.TARGET_CONFIG)
        release_handler.REPO_ROOT = ""
        release_handler.TARGET_CONFIG = {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"}
        return original

    def restore(self, original, cwd):
        release_handler.REPO_ROOT, release_handler.TARGET_CONFIG = original
        os.chdir(cwd)

    def test_packs_matching_extensions_only(self, work_dir):
        cwd = os.getcwd()
        original = self.isolate(work_dir)
        (work_dir / "merged-rules" / "A").mkdir(parents=True)
        (work_dir / "merged-rules" / "A" / "x.txt").write_text("x", encoding="utf-8")
        (work_dir / "merged-rules" / "A" / "skip.md").write_text("y", encoding="utf-8")
        (work_dir / "merged-rules-mrs" / "A").mkdir(parents=True)
        (work_dir / "merged-rules-mrs" / "A" / "x.mrs").write_text("z", encoding="utf-8")
        os.chdir(work_dir)
        try:
            zip_name, manifest = release_handler.zip_target_files("2026-10-05")
            assert zip_name == "merged-rules-2026-10-05.zip"
            assert posix_paths(manifest["merged-rules"]) == ["merged-rules/A/x.txt"]
            assert posix_paths(manifest["merged-rules-mrs"]) == ["merged-rules-mrs/A/x.mrs"]
            with zipfile.ZipFile(zip_name) as z:
                names = sorted(n.replace("\\", "/") for n in z.namelist())
            assert names == ["merged-rules-mrs/A/x.mrs", "merged-rules/A/x.txt"]
        finally:
            self.restore(original, cwd)

    def test_missing_dir_skipped_not_fatal(self, work_dir):
        cwd = os.getcwd()
        original = self.isolate(work_dir)
        (work_dir / "merged-rules").mkdir()
        (work_dir / "merged-rules" / "a.txt").write_text("x", encoding="utf-8")
        os.chdir(work_dir)
        try:
            _zip_name, manifest = release_handler.zip_target_files("2026-10-05")
            assert posix_paths(manifest["merged-rules"]) == ["merged-rules/a.txt"]
            assert "merged-rules-mrs" not in manifest
        finally:
            self.restore(original, cwd)

    def test_no_files_exits_nonzero(self, work_dir):
        cwd = os.getcwd()
        original = self.isolate(work_dir)
        (work_dir / "merged-rules").mkdir()
        os.chdir(work_dir)
        try:
            try:
                release_handler.zip_target_files("2026-10-05")
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("无匹配文件时应 SystemExit(1)")
        finally:
            self.restore(original, cwd)


class TestPublishRelease:
    def test_existing_release_updates_in_place(self):
        stub = GhStub()
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result == "ok"
        assert stub.calls[0] == ["release", "upload", "rules-2026-10-05", ZIP, "--clobber"]
        assert stub.calls[1] == [
            "release", "edit", "rules-2026-10-05",
            "--title", "title",
            "--notes", NOTES,
            "--latest",
        ]
        assert len(stub.calls) == 2

    def test_existing_release_never_deletes(self):
        stub = GhStub()
        run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        flat = " ".join(stub.commands())
        assert "delete" not in flat
        assert "git/refs/tags" not in flat

    def test_missing_release_created(self):
        stub = GhStub()
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, False),
        )
        assert result == "ok"
        assert len(stub.calls) == 1
        assert stub.calls[0][:4] == ["release", "create", "rules-2026-10-05", ZIP]
        assert stub.calls[0][-1] == "--latest"

    def test_upload_failure_skips_edit(self):
        stub = GhStub(fail_when=lambda cmd: cmd[1] == "upload")
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result is None
        assert [c[1] for c in stub.calls] == ["upload"]

    def test_edit_failure_propagates(self):
        stub = GhStub(fail_when=lambda cmd: cmd[1] == "edit")
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result is None
        assert [c[1] for c in stub.calls] == ["upload", "edit"]

    def test_create_failure_propagates(self):
        stub = GhStub(fail_when=lambda cmd: cmd[1] == "create")
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, False),
        )
        assert result is None

    def test_always_passes_latest_flag(self):
        for exists in (True, False):
            stub = GhStub()
            run_with_stub(
                stub,
                lambda e=exists: release_handler.publish_release(
                    "rules-2026-10-05", ZIP, "t", NOTES, e),
            )
            assert stub.calls[-1][-1] == "--latest"


class TestMainOrchestration:

    def setup_env(self, work_dir):
        cwd = os.getcwd()
        original = (release_handler.REPO_ROOT, release_handler.TARGET_CONFIG,
                    release_handler.CHANGE_DETECTION, release_handler.KEEP_DAYS)
        release_handler.REPO_ROOT = ""
        release_handler.TARGET_CONFIG = {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"}
        release_handler.CHANGE_DETECTION = True
        release_handler.KEEP_DAYS = 3
        (work_dir / "merged-rules" / "A").mkdir(parents=True)
        (work_dir / "merged-rules" / "A" / "x.txt").write_text("x", encoding="utf-8")
        (work_dir / "merged-rules-mrs" / "A").mkdir(parents=True)
        (work_dir / "merged-rules-mrs" / "A" / "x.mrs").write_text("y", encoding="utf-8")
        os.chdir(work_dir)
        return cwd, original

    def teardown_env(self, cwd, original):
        release_handler.REPO_ROOT, release_handler.TARGET_CONFIG, \
            release_handler.CHANGE_DETECTION, release_handler.KEEP_DAYS = original
        os.chdir(cwd)

    def test_publishes_when_hash_changed(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        stub = GhStub(responses={"release": "ok"})
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        saved = []
        original_save = release_handler.save_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: "old"
        release_handler.save_last_hash = lambda h: saved.append(h)
        try:
            run_with_stub(stub, release_handler.main)
            assert saved == ["new"], "发布成功后应保存新哈希"
            assert not [f for f in os.listdir(".") if f.startswith("merged-rules-")
                        and f.endswith(".zip")], "发布后应删除临时 zip"
        except SystemExit as e:
            raise AssertionError(f"main() 不应退出: {e}") from e
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            self.teardown_env(cwd, original)

    def test_skips_when_hash_unchanged(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        stub = GhStub()
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("same", 1, 1)
        release_handler.load_last_hash = lambda: "same"
        try:
            run_with_stub(stub, release_handler.main)
            assert stub.calls == [], "内容无变化时不应调用任何 gh 命令"
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            self.teardown_env(cwd, original)

    def test_aborts_when_product_counts_differ(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        stub = GhStub()
        original_hash = release_handler.combined_products_hash
        release_handler.combined_products_hash = lambda *a, **k: ("h", 3, 2)
        try:
            try:
                run_with_stub(stub, release_handler.main)
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("数量不一致时应 SystemExit(1)")
            assert stub.calls == []
        finally:
            release_handler.combined_products_hash = original_hash
            self.teardown_env(cwd, original)

    def test_failed_publish_exits_and_removes_zip(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        stub = GhStub(fail_when=lambda cmd: len(cmd) > 1 and cmd[1] in ("create", "upload", "edit"),
                      responses={("release", "view"): None})
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        saved = []
        original_save = release_handler.save_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda h: saved.append(h)
        try:
            try:
                run_with_stub(stub, release_handler.main)
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("发布失败时应 SystemExit(1)")
            assert saved == [], "发布失败不得保存哈希，否则下次不会重试"
            assert "create" in stub.release_actions()
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            self.teardown_env(cwd, original)

    def test_prunes_old_releases_and_keeps_current(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        now_iso = release_handler.beijing_now().strftime("%Y-%m-%d")
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": f"rules-{now_iso}", "createdAt": old},
            {"tagName": "rules-1999-01-01", "createdAt": old},
            {"tagName": "v1.0.0", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        original_save = release_handler.save_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda h: None
        try:
            run_with_stub(stub, release_handler.main)
            flat = " ".join(stub.commands())
            assert "release delete rules-1999-01-01 --yes" in flat
            assert f"release delete rules-{now_iso}" not in flat, "当天标签不得被删"
            assert "git/refs/tags/rules-1999-01-01" in flat, "删除 release 后应同时删 tag"
            assert "v1.0.0" not in flat, "非 rules- 前缀的 Release 一律不得触碰"
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            self.teardown_env(cwd, original)

    def test_prune_never_touches_manual_releases(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        old = (release_handler.beijing_now() - datetime.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "v1.0.0", "createdAt": old},
            {"tagName": "v1.2.3-rc1", "createdAt": old},
            {"tagName": "manual-notes", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        original_save = release_handler.save_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda h: None
        try:
            run_with_stub(stub, release_handler.main)
            flat = " ".join(stub.commands())
            assert "delete" not in flat, f"人工 Release 不得被删: {flat}"
            assert "git/refs/tags" not in flat
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            self.teardown_env(cwd, original)

    def test_prune_delete_failure_is_not_fatal(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "rules-1999-01-01", "createdAt": old},
            {"tagName": "rules-1999-01-02", "createdAt": old},
        )
        stub = GhStub(fail_when=lambda cmd: len(cmd) > 1 and cmd[1] == "delete",
                      responses={("release", "list"): listing})
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        original_save = release_handler.save_last_hash
        saved = []
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda h: saved.append(h)
        try:
            try:
                run_with_stub(stub, release_handler.main)
            except SystemExit as e:
                raise AssertionError(f"单个删除失败不得中断脚本: {e}") from e
            flat = " ".join(stub.commands())
            assert flat.count("release delete") == 2, "首个删除失败后仍应继续清理其余旧 Release"
            assert "git/refs/tags" not in flat, "Release 删除失败时不得继续删 tag"
            assert saved == ["new"], "清理失败不得跳过 state/release.sha256 的写入"
            zip_name = f"merged-rules-{release_handler.beijing_now().strftime('%Y-%m-%d')}.zip"
            assert not os.path.exists(zip_name), "清理失败后 main() 仍应执行到收尾"
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            self.teardown_env(cwd, original)

    def test_step_summary_written(self, work_dir):
        cwd, original = self.setup_env(work_dir)
        summary = work_dir / "summary.md"
        prior = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        stub = GhStub()
        original_hash = release_handler.combined_products_hash
        original_load = release_handler.load_last_hash
        original_save = release_handler.save_last_hash
        release_handler.combined_products_hash = lambda *a, **k: ("new", 1, 1)
        release_handler.load_last_hash = lambda: None
        release_handler.save_last_hash = lambda h: None
        try:
            run_with_stub(stub, release_handler.main)
            text = summary.read_text(encoding="utf-8")
            assert "发布报告" in text
            assert "文本规则" in text and "MRS 规则" in text
        finally:
            release_handler.combined_products_hash = original_hash
            release_handler.load_last_hash = original_load
            release_handler.save_last_hash = original_save
            if prior is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = prior
            self.teardown_env(cwd, original)


class TestRunGhErrorHandling:
    def test_oserror_exits_nonzero(self):
        import subprocess

        def boom(*a, **k):
            raise OSError("gh not found")

        original = subprocess.run
        subprocess.run = boom
        try:
            try:
                release_handler.run_gh(["release", "list"])
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("OSError 应导致 SystemExit(1)")
        finally:
            subprocess.run = original


class TestConstants:
    def test_keep_days_positive(self):
        assert isinstance(release_handler.KEEP_DAYS, int)
        assert release_handler.KEEP_DAYS >= 1

    def test_target_config_maps_dir_to_extension(self):
        assert release_handler.TARGET_CONFIG == {
            "merged-rules": ".txt",
            "merged-rules-mrs": ".mrs",
        }

    def test_keep_days_within_list_limit(self):
        assert release_handler.KEEP_DAYS * 2 <= 50

    def test_beijing_offset_is_utc8(self):
        assert release_handler.beijing_now().utcoffset() == datetime.timedelta(hours=8)


class TestVerifyOnly:

    def setup_products(self, work_dir, *, paired=True):
        cwd = os.getcwd()
        original = (release_handler.RULESETS_DIR, release_handler.MERGED_DIR,
                    release_handler.MRS_DIR, release_handler.merge_product_entries)
        release_handler.merge_product_entries = lambda *a, **k: set()
        (work_dir / "rulesets").mkdir(parents=True)
        (work_dir / "rulesets" / "products.manifest").write_text(
            "block/domain/A/x.txt\n", encoding="utf-8")
        (work_dir / "merged-rules" / "block" / "domain" / "A").mkdir(parents=True)
        (work_dir / "merged-rules" / "block" / "domain" / "A" / "x.txt").write_text(
            "x", encoding="utf-8")
        mrs_dir = work_dir / "merged-rules-mrs" / "block" / "domain" / "A"
        mrs_dir.mkdir(parents=True)
        if paired:
            (mrs_dir / "x.mrs").write_text("y", encoding="utf-8")
        release_handler.RULESETS_DIR = "rulesets"
        release_handler.MERGED_DIR = "merged-rules"
        release_handler.MRS_DIR = "merged-rules-mrs"
        os.chdir(work_dir)
        return cwd, original

    def teardown_products(self, cwd, original):
        release_handler.RULESETS_DIR, release_handler.MERGED_DIR, \
            release_handler.MRS_DIR, release_handler.merge_product_entries = original
        os.chdir(cwd)

    def test_paired_products_pass_without_gh(self, work_dir):
        cwd, original = self.setup_products(work_dir)
        stub = GhStub()
        try:
            run_with_stub(stub, release_handler.verify_only)
            assert stub.calls == [], "预检不应调用 gh"
        finally:
            self.teardown_products(cwd, original)

    def test_missing_mrs_exits_nonzero(self, work_dir):
        cwd, original = self.setup_products(work_dir, paired=False)
        stub = GhStub()
        try:
            with pytest.raises(SystemExit) as exc:
                run_with_stub(stub, release_handler.verify_only)
            assert exc.value.code == 1
            assert stub.calls == []
        finally:
            self.teardown_products(cwd, original)

    def test_missing_baseline_exits_nonzero_when_rulesets_present(self, work_dir):
        cwd, original = self.setup_products(work_dir)
        try:
            os.unlink("rulesets/products.manifest")
            with pytest.raises(SystemExit) as exc:
                release_handler.verify_only()
            assert exc.value.code == 1
        finally:
            self.teardown_products(cwd, original)

    def test_main_dispatches_verify_only_without_publishing(self, work_dir, monkeypatch):
        cwd, original = self.setup_products(work_dir)
        calls = []
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--verify-only"])
        monkeypatch.setattr(release_handler, "verify_only", lambda: calls.append(True))
        try:
            run_with_stub(GhStub(), release_handler.main)
            assert calls == [True], "main() 应分发到 verify_only 并提前返回"
        finally:
            self.teardown_products(cwd, original)

