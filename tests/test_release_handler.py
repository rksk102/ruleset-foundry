import datetime
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import zipfile

import manifest
import pytest
import release_handler
from support import GhStub


def release_listing(*releases):
    return json.dumps(list(releases))


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
        publish, why = release_handler.should_publish("h", "", True)
        assert publish is True
        assert "首次" in why

    def test_changed_hash_publishes(self):
        publish, why = release_handler.should_publish("new", "old", True)
        assert publish is True
        assert "变化" in why

    def test_same_hash_skips(self):
        publish, why = release_handler.should_publish("same", "same", True)
        assert publish is False
        assert "无变化" in why

    def test_force_overrides_same_hash(self):
        publish, why = release_handler.should_publish("same", "same", True, force=True)
        assert publish is True
        assert "强制" in why

    def test_force_overrides_disabled_detection(self):
        publish, why = release_handler.should_publish("same", "same", False, force=True)
        assert publish is True
        assert "强制" in why


class TestLatestRulesRelease:
    def test_requires_is_latest_flag(self, monkeypatch):
        listing = release_listing(
            {"tagName": "v1.0.0"},
            {"tagName": "rules-0123456789ab"},
            {"tagName": "rules-20261004-2100"},
        )
        stub = GhStub(responses={("release", "list"): listing})
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.latest_rules_release_tag() is None

    def test_returns_latest_rules_release(self, monkeypatch):
        listing = release_listing(
            {"tagName": "v1.0.0"},
            {"tagName": "rules-0123456789ab", "isLatest": True},
        )
        stub = GhStub(responses={("release", "list"): listing})
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.latest_rules_release_tag() == "rules-0123456789ab"

    def test_prefers_explicit_latest_over_list_order(self, monkeypatch):
        listing = release_listing(
            {"tagName": "rules-bbbbbbbbbbbb", "isLatest": False},
            {"tagName": "rules-aaaaaaaaaaaa", "isLatest": True},
        )
        stub = GhStub(responses={("release", "list"): listing})
        monkeypatch.setattr(release_handler, "run_gh", stub)

        assert release_handler.latest_rules_release_tag() == "rules-aaaaaaaaaaaa"

    def test_returns_none_on_gh_failure(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: None)
        assert release_handler.latest_rules_release_tag() is None

    def test_returns_none_on_malformed_json(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "not-json")
        assert release_handler.latest_rules_release_tag() is None

    def test_returns_none_on_non_list_json(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "null")
        assert release_handler.latest_rules_release_tag() is None

    def test_needs_repair_when_no_rules_release(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "[]")
        assert release_handler.needs_release_repair() is True

    def test_needs_repair_when_assets_incomplete(self, monkeypatch):
        stub = GhStub(latest_tag="rules-20261004-2100", asset_names="merged-rules-20261004-2100.zip")
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.needs_release_repair() is True

    def test_no_repair_when_assets_complete(self, monkeypatch):
        stub = GhStub(latest_tag="rules-20261004-2100")
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.needs_release_repair() is False


def workspace_product_bytes(work_dir):
    files = {}
    for folder, ext in (("merged-rules", ".txt"), ("merged-rules-mrs", ".mrs")):
        for path in sorted((work_dir / folder).rglob(f"*{ext}")):
            rel = path.relative_to(work_dir).as_posix()
            files[rel] = path.read_bytes()
    return files


def remote_release_stub(
    work_dir,
    tag_id,
    corrupt_file=None,
    corrupt_zip_digest=False,
    zip_entries=None,
    empty_digests=False,
    newline="\n",
):
    product_bytes = workspace_product_bytes(work_dir)
    files = {rel: hashlib.sha256(data).hexdigest() for rel, data in product_bytes.items()}
    if corrupt_file:
        files[corrupt_file] = "0" * 64
    zip_name = f"merged-rules-{tag_id}.zip"
    buffer = io.BytesIO()
    entries = list(zip_entries) if zip_entries is not None else sorted(product_bytes.items())
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for rel, data in entries:
            zipf.writestr(rel, data)
    zip_bytes = buffer.getvalue()
    zip_digest = hashlib.sha256(zip_bytes).hexdigest()
    sums_zip_digest = hashlib.sha256(b"other-zip").hexdigest() if corrupt_zip_digest else zip_digest
    sums = newline.join(
        [f"{sums_zip_digest}  {zip_name}"] + [f"{d}  {r}" for r, d in sorted(files.items())]
    ) + newline
    sums_bytes = sums.encode("utf-8")
    stub = GhStub(
        asset_digests={
            zip_name: "" if empty_digests else "sha256:" + zip_digest,
            "SHA256SUMS": "" if empty_digests else "sha256:" + hashlib.sha256(sums_bytes).hexdigest(),
        },
        sha256sums=sums,
    )
    stub.asset_bytes = {zip_name: zip_bytes, "SHA256SUMS": sums_bytes}
    return stub, zip_name


def patch_remote_release(monkeypatch, stub):
    monkeypatch.setattr(release_handler, "run_gh", stub)

    def download(cmd, max_bytes=None):
        asset_name = cmd[cmd.index("-p") + 1]
        data = stub.asset_bytes.get(asset_name)
        if data is not None and max_bytes is not None and len(data) > max_bytes:
            return None
        return data

    monkeypatch.setattr(release_handler, "run_gh_bytes", download, raising=False)


class TestParseSha256sums:
    def test_duplicate_path_is_rejected(self):
        text = f"{'a' * 64}  a.txt\n{'b' * 64}  a.txt\n"
        with pytest.raises(ValueError, match="重复"):
            release_handler.parse_sha256sums(text)

    def test_valid_entries_are_parsed(self):
        text = f"{'a' * 64}  a.txt\n{'b' * 64}  dir/b.txt\n"
        assert release_handler.parse_sha256sums(text) == {"a.txt": "a" * 64, "dir/b.txt": "b" * 64}


class TestReleaseReusable:
    def test_reusable_when_digests_match(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_rejects_wrong_file_hash(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(
            publish_workspace, "abc123abc123", corrupt_file="merged-rules/A/x.txt"
        )
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "逐文件哈希" in why

    def test_rejects_zip_digest_mismatch(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", corrupt_zip_digest=True)
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "zip 的远端 digest" in why

    def test_rejects_mismatched_nonempty_api_digest(self, publish_workspace, monkeypatch):
        stub, zip_name = remote_release_stub(publish_workspace, "abc123abc123")
        stub.asset_digests[zip_name] = "sha256:" + "0" * 64
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "zip 的远端 digest" in why

    def test_rejects_missing_digest(self, publish_workspace, monkeypatch):
        stub, zip_name = remote_release_stub(
            publish_workspace, "abc123abc123", corrupt_file="merged-rules/A/x.txt"
        )
        stub.asset_digests[zip_name] = ""
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "逐文件哈希" in why

    def test_allows_empty_api_digests_when_all_content_matches(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", empty_digests=True)
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_crlf_sha256sums_uses_raw_bytes_for_digest(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", newline="\r\n")
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_invalid_utf8_is_a_reuse_failure_not_an_exception(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        invalid = b"\xff\xfe"
        stub.asset_bytes["SHA256SUMS"] = invalid
        stub.asset_digests["SHA256SUMS"] = "sha256:" + hashlib.sha256(invalid).hexdigest()
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "UTF-8" in why

    def test_rejects_wrong_zip_with_self_consistent_sums(self, publish_workspace, monkeypatch):
        entries = [
            (rel, b"remote-tampered" if rel.endswith(".txt") else data)
            for rel, data in workspace_product_bytes(publish_workspace).items()
        ]
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", zip_entries=entries)
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "ZIP" in why

    def test_rejects_duplicate_zip_entries(self, publish_workspace, monkeypatch):
        entries = list(workspace_product_bytes(publish_workspace).items())
        entries.append(("merged-rules/A/x.txt", b"x"))
        with pytest.warns(UserWarning, match="Duplicate name"):
            stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", zip_entries=entries)
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "重复" in why

    @pytest.mark.parametrize("extra_name", ["../escape.txt", "merged-rules/A/extra.txt"])
    def test_rejects_traversal_and_extra_zip_entries(self, publish_workspace, monkeypatch, extra_name):
        entries = list(workspace_product_bytes(publish_workspace).items())
        entries.append((extra_name, b"extra"))
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", zip_entries=entries)
        patch_remote_release(monkeypatch, stub)

        ok, _why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False

    @pytest.mark.parametrize(
        ("limit_name", "value"),
        [("MAX_ZIP_ENTRIES", 1), ("MAX_ZIP_ENTRY_SIZE", 0), ("MAX_ZIP_TOTAL_SIZE", 1)],
    )
    def test_enforces_zip_read_limits(self, publish_workspace, monkeypatch, limit_name, value):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        patch_remote_release(monkeypatch, stub)
        monkeypatch.setattr(release_handler, limit_name, value, raising=False)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "限制" in why

    def test_rejects_symlink_zip_entries(self, work_dir):
        products = {"merged-rules/A/x.txt": b"good"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipf:
            for rel, data in products.items():
                info = zipfile.ZipInfo(rel)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipf.writestr(info, data)
        hashes = {rel: hashlib.sha256(data).hexdigest() for rel, data in products.items()}

        ok, why = release_handler.verify_zip_content(buffer.getvalue(), hashes)

        assert ok is False
        assert "普通文件" in why

    def test_allows_unix_regular_entry_with_permissions_only_mode(self):
        products = {"merged-rules/A/x.txt": b"good"}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipf:
            for rel, data in products.items():
                info = zipfile.ZipInfo(rel)
                info.create_system = 3
                info.external_attr = 0o600 << 16
                zipf.writestr(info, data)
        hashes = {rel: hashlib.sha256(data).hexdigest() for rel, data in products.items()}

        ok, why = release_handler.verify_zip_content(buffer.getvalue(), hashes)

        assert ok is True, why

    def test_entry_limit_is_checked_before_zipfile_initialization(self, publish_workspace, monkeypatch):
        stub, zip_name = remote_release_stub(publish_workspace, "abc123abc123")
        local_hashes = {
            rel: hashlib.sha256(data).hexdigest() for rel, data in workspace_product_bytes(publish_workspace).items()
        }
        monkeypatch.setattr(release_handler, "MAX_ZIP_ENTRIES", 1)

        def must_not_parse(*_args, **_kwargs):
            raise AssertionError("条目数预检前不得初始化 ZipFile")

        monkeypatch.setattr(release_handler.zipfile, "ZipFile", must_not_parse)
        ok, why = release_handler.verify_zip_content(stub.asset_bytes[zip_name], local_hashes)

        assert ok is False
        assert "限制" in why

    def test_oversized_zip_asset_is_reused_failure(self, publish_workspace, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        patch_remote_release(monkeypatch, stub)
        monkeypatch.setattr(release_handler, "MAX_ZIP_ASSET_BYTES", 1)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "无法下载" in why

    def test_needs_repair_when_latest_is_not_current_content(self, monkeypatch):
        stub = GhStub(latest_tag="rules-20261004-2100")
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.needs_release_repair("new") is True

    def test_needs_repair_when_digests_do_not_match(self, monkeypatch):
        tag = expected_tag("new")
        stub = GhStub(latest_tag=tag)
        monkeypatch.setattr(release_handler, "run_gh", stub)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, "bad"))
        assert release_handler.needs_release_repair("new") is True

    def test_no_repair_when_tag_and_digests_match(self, monkeypatch):
        tag = expected_tag("new")
        stub = GhStub(latest_tag=tag)
        monkeypatch.setattr(release_handler, "run_gh", stub)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))
        assert release_handler.needs_release_repair("new") is False


class TestNeedsPublishMode:
    def test_prints_false_when_unchanged_and_release_complete(self, monkeypatch, capsys):
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "h")
        monkeypatch.setattr(release_handler, "needs_release_repair", lambda *a, **k: False)
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--needs-publish"])

        release_handler.main()

        assert capsys.readouterr().out.strip() == "false"

    def test_prints_true_when_repair_needed(self, monkeypatch, capsys):
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "h")
        monkeypatch.setattr(release_handler, "needs_release_repair", lambda *a, **k: True)
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--needs-publish"])

        release_handler.main()

        assert capsys.readouterr().out.strip() == "true"


class TestWriteSha256sums:
    def test_zip_and_files_listed(self, zip_workspace):
        (zip_workspace / "merged-rules" / "A").mkdir(parents=True)
        (zip_workspace / "merged-rules" / "A" / "x.txt").write_text("x", encoding="utf-8")
        (zip_workspace / "z.zip").write_bytes(b"zip-bytes")
        file_map = {"merged-rules": ["merged-rules/A/x.txt"]}

        out = release_handler.write_sha256sums("z.zip", file_map)

        lines = (zip_workspace / out).read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"{hashlib.sha256(b'zip-bytes').hexdigest()}  z.zip"
        assert lines[1] == f"{hashlib.sha256(b'x').hexdigest()}  merged-rules/A/x.txt"
        assert len(lines) == 2

    def test_backslashes_normalized_to_posix(self, zip_workspace):
        (zip_workspace / "merged-rules").mkdir()
        (zip_workspace / "merged-rules" / "a.txt").write_text("a", encoding="utf-8")
        (zip_workspace / "z.zip").write_bytes(b"z")
        file_map = {"merged-rules": ["merged-rules\\a.txt"]}

        out = release_handler.write_sha256sums("z.zip", file_map)

        text = (zip_workspace / out).read_text(encoding="utf-8")
        assert "merged-rules/a.txt" in text
        assert "\\" not in text


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
            "2026-10-05", "06:12:00", {"txt-out": ["txt-out/a.txt"], "mrs-out": ["mrs-out/b.mrs"]}
        )
        assert "| 文本规则 | `txt-out` | **1** |" in out
        assert "| MRS 规则 | `mrs-out` | **1** |" in out
        assert "**2**" in out

def posix_paths(paths):
    return [p.replace("\\", "/") for p in paths]


@pytest.fixture
def zip_workspace(work_dir, monkeypatch):
    """REPO_ROOT 必须是相对路径：zip 的相对名由它算出来。"""
    monkeypatch.setattr(release_handler, "REPO_ROOT", "")
    monkeypatch.setattr(release_handler, "TARGET_CONFIG", {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"})
    monkeypatch.chdir(work_dir)
    return work_dir


class TestZipTargetFiles:
    def test_packs_matching_extensions_only(self, zip_workspace):
        (zip_workspace / "merged-rules" / "A").mkdir(parents=True)
        (zip_workspace / "merged-rules" / "A" / "x.txt").write_text("x", encoding="utf-8")
        (zip_workspace / "merged-rules" / "A" / "skip.md").write_text("y", encoding="utf-8")
        (zip_workspace / "merged-rules-mrs" / "A").mkdir(parents=True)
        (zip_workspace / "merged-rules-mrs" / "A" / "x.mrs").write_text("z", encoding="utf-8")

        zip_name, manifest = release_handler.zip_target_files("2026-10-05")

        assert zip_name == "merged-rules-2026-10-05.zip"
        assert posix_paths(manifest["merged-rules"]) == ["merged-rules/A/x.txt"]
        assert posix_paths(manifest["merged-rules-mrs"]) == ["merged-rules-mrs/A/x.mrs"]
        with zipfile.ZipFile(zip_name) as z:
            names = sorted(n.replace("\\", "/") for n in z.namelist())
        assert names == ["merged-rules-mrs/A/x.mrs", "merged-rules/A/x.txt"]

    def test_missing_dir_skipped_not_fatal(self, zip_workspace):
        (zip_workspace / "merged-rules").mkdir()
        (zip_workspace / "merged-rules" / "a.txt").write_text("x", encoding="utf-8")

        _zip_name, manifest = release_handler.zip_target_files("2026-10-05")

        assert posix_paths(manifest["merged-rules"]) == ["merged-rules/a.txt"]
        assert "merged-rules-mrs" not in manifest

    def test_hidden_files_and_dirs_skipped(self, zip_workspace):
        (zip_workspace / "merged-rules" / ".hidden").mkdir(parents=True)
        (zip_workspace / "merged-rules" / ".hidden" / "x.txt").write_text("x", encoding="utf-8")
        (zip_workspace / "merged-rules" / "A").mkdir()
        (zip_workspace / "merged-rules" / "A" / ".y.txt").write_text("y", encoding="utf-8")
        (zip_workspace / "merged-rules" / "A" / "z.txt").write_text("z", encoding="utf-8")

        _zip_name, manifest = release_handler.zip_target_files("2026-10-05")

        assert posix_paths(manifest["merged-rules"]) == ["merged-rules/A/z.txt"]

    def test_no_files_exits_nonzero(self, zip_workspace):
        (zip_workspace / "merged-rules").mkdir()
        with pytest.raises(SystemExit) as exc:
            release_handler.zip_target_files("2026-10-05")
        assert exc.value.code == 1


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
            "release",
            "edit",
            "rules-2026-10-05",
            "--title",
            "title",
            "--notes",
            NOTES,
            "--latest",
        ]
        assert len(stub.calls) == 2

    def test_extra_assets_uploaded_alongside_zip(self):
        stub = GhStub()
        run_with_stub(
            stub,
            lambda: release_handler.publish_release(
                "rules-2026-10-05", ZIP, "title", NOTES, True, extra_assets=["SHA256SUMS"]
            ),
        )
        assert stub.calls[0] == ["release", "upload", "rules-2026-10-05", ZIP, "SHA256SUMS", "--clobber"]

    def test_extra_assets_included_on_create(self):
        stub = GhStub()
        run_with_stub(
            stub,
            lambda: release_handler.publish_release(
                "rules-2026-10-05", ZIP, "title", NOTES, False, extra_assets=["SHA256SUMS"]
            ),
        )
        create = next(c for c in stub.calls if c[1] == "create")
        assert ZIP in create and "SHA256SUMS" in create

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


@pytest.fixture
def publish_workspace(work_dir, monkeypatch):
    monkeypatch.setattr(release_handler, "REPO_ROOT", "")
    monkeypatch.setattr(release_handler, "TARGET_CONFIG", {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"})
    monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
    monkeypatch.setattr(release_handler, "KEEP_DAYS", 3)
    (work_dir / "merged-rules" / "A").mkdir(parents=True)
    (work_dir / "merged-rules" / "A" / "x.txt").write_text("x", encoding="utf-8")
    (work_dir / "merged-rules-mrs" / "A").mkdir(parents=True)
    (work_dir / "merged-rules-mrs" / "A" / "x.mrs").write_text("y", encoding="utf-8")
    monkeypatch.chdir(work_dir)
    return work_dir


def expected_tag(product_hash):
    return "rules-" + hashlib.sha256(product_hash.encode()).hexdigest()[:12]


def stub_hash_state(monkeypatch, current="new", last=None, saved=None):
    monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: (current, 1, 1))
    monkeypatch.setattr(release_handler, "load_last_hash", lambda: last)
    monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a: (True, "ok"))
    monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
    if saved is not None:
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))
    else:
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: None)


class TestMainOrchestration:
    def test_publishes_when_hash_changed(self, publish_workspace, monkeypatch):
        stub = GhStub(responses={"release": "ok"})
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        run_with_stub(stub, release_handler.main)

        assert saved == ["new"], "发布成功后应保存新哈希"
        assert not [f for f in os.listdir(".") if f.startswith("merged-rules-") and f.endswith(".zip")], (
            "发布后应删除临时 zip"
        )
        assert not os.path.exists("SHA256SUMS"), "发布后应删除临时 SHA256SUMS"

    def test_new_release_identity_uses_product_hash(self, publish_workspace, monkeypatch):
        product_hash = "b" * 64
        release_id = hashlib.sha256(product_hash.encode()).hexdigest()[:12]
        stub = GhStub(release_exists=False)
        stub_hash_state(monkeypatch, current=product_hash, last=None)

        run_with_stub(stub, release_handler.main)

        create = next(call for call in stub.calls if call[:2] == ["release", "create"])
        assert create[2] == f"rules-{release_id}"
        assert create[3] == f"merged-rules-{release_id}.zip"

    def test_retry_reuses_complete_release_for_same_hash(self, publish_workspace, monkeypatch):
        product_hash = "a" * 64
        saved = []
        stub = GhStub()
        stub_hash_state(monkeypatch, current=product_hash, last="old", saved=saved)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))

        run_with_stub(stub, release_handler.main)

        actions = stub.release_actions()
        assert actions.count("edit") == 1, "复用完整 Release 时应重新标记 latest"
        assert "create" not in actions and "upload" not in actions, "同哈希 Release 资产完整时不得重复上传"
        assert saved == [product_hash], "复用完整 Release 后仍应推进状态哈希"

    def test_reuse_latest_failure_does_not_save_hash(self, publish_workspace, monkeypatch):
        product_hash = "d" * 64
        saved = []
        stub = GhStub(fail_when=lambda cmd: cmd[:2] == ["release", "edit"])
        stub_hash_state(monkeypatch, current=product_hash, last="old", saved=saved)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))

        with pytest.raises(SystemExit) as exc:
            run_with_stub(stub, release_handler.main)

        assert exc.value.code == 1
        assert saved == [], "latest 标记失败时不得保存哈希"
        assert "upload" not in stub.release_actions()

    def test_incomplete_release_for_same_hash_is_repaired(self, publish_workspace, monkeypatch):
        product_hash = "c" * 64
        release_id = hashlib.sha256(product_hash.encode()).hexdigest()[:12]
        saved = []
        stub = GhStub(asset_names=f"merged-rules-{release_id}.zip")
        stub_hash_state(monkeypatch, current=product_hash, last="old", saved=saved)

        run_with_stub(stub, release_handler.main)

        assert "upload" in stub.release_actions(), "缺少 SHA256SUMS 时应修复现有 Release"
        assert saved == [product_hash]

    def test_complete_assets_with_bad_digest_are_reuploaded(self, publish_workspace, monkeypatch):
        product_hash = "e" * 64
        saved = []
        stub = GhStub()
        stub_hash_state(monkeypatch, current=product_hash, last="old", saved=saved)
        checks = iter([(False, "bad"), (True, "ok")])
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a: next(checks))

        run_with_stub(stub, release_handler.main)

        actions = stub.release_actions()
        assert "upload" in actions and "create" not in actions
        assert saved == [product_hash]

    def test_force_release_reuses_complete_release_without_duplicate(self, publish_workspace, monkeypatch):
        stub = GhStub(responses={"release": "ok"})
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        monkeypatch.setenv("FORCE_RELEASE", "true")
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))

        run_with_stub(stub, release_handler.main)

        assert saved == ["same"], "FORCE_RELEASE=true 时内容无变化也应保存哈希"
        actions = stub.release_actions()
        assert actions.count("edit") == 1
        assert "create" not in actions and "upload" not in actions
        assert not os.path.exists("SHA256SUMS")

    def test_empty_api_digests_do_not_clobber_on_consecutive_runs(self, publish_workspace, monkeypatch):
        product_hash = "f" * 64
        tag_id = release_handler.release_id_for_hash(product_hash)
        stub, _zip = remote_release_stub(publish_workspace, tag_id, empty_digests=True)
        stub.latest_tag = f"rules-{tag_id}"
        patch_remote_release(monkeypatch, stub)
        saved = []
        real_verify = release_handler.verify_release_reusable
        stub_hash_state(monkeypatch, current=product_hash, last="old", saved=saved)
        monkeypatch.setattr(release_handler, "verify_release_reusable", real_verify)

        run_with_stub(stub, release_handler.main)
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: product_hash)
        run_with_stub(stub, release_handler.main)

        actions = stub.release_actions()
        assert "upload" not in actions and "create" not in actions
        assert actions.count("edit") == 1
        assert saved == [product_hash]

    def test_skips_when_hash_unchanged(self, publish_workspace, monkeypatch):
        stub = GhStub(latest_tag=expected_tag("same"))
        stub_hash_state(monkeypatch, current="same", last="same")
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))

        run_with_stub(stub, release_handler.main)

        mutating = [
            c
            for c in stub.calls
            if c[0] == "release" and len(c) > 1 and c[1] in ("create", "upload", "edit", "delete")
        ]
        assert mutating == [], "内容无变化且当天 Release 完整时不应做任何发布变更"

    def test_republishes_when_release_missing(self, publish_workspace, monkeypatch):
        """哈希未变但最近一次 Release 不存在（如强制补发失败/被删）时应补发自愈。"""
        stub = GhStub(release_exists=False, responses={"release": "ok"})
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)

        run_with_stub(stub, release_handler.main)

        assert saved == ["same"]
        assert any(call[1] in ("create", "upload") for call in stub.calls if call[0] == "release")

    def test_republishes_when_asset_incomplete(self, publish_workspace, monkeypatch):
        """哈希未变但最近一次 Release 缺 SHA256SUMS 时应补发自愈。"""
        tag = expected_tag("same")
        release_id = tag.removeprefix("rules-")
        stub = GhStub(latest_tag=tag, asset_names=f"merged-rules-{release_id}.zip")
        saved = []
        real_verify = release_handler.verify_release_reusable
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        checks = []

        def verify_before_repair(*args):
            checks.append(args)
            return real_verify(*args) if len(checks) == 1 else (True, "ok")

        monkeypatch.setattr(release_handler, "verify_release_reusable", verify_before_repair)

        run_with_stub(stub, release_handler.main)

        assert saved == ["same"]
        assert any(call[1] in ("create", "upload") for call in stub.calls if call[0] == "release")

    def test_aborts_when_product_counts_differ(self, publish_workspace, monkeypatch):
        stub = GhStub()
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 3, 2))

        with pytest.raises(SystemExit) as exc:
            run_with_stub(stub, release_handler.main)

        assert exc.value.code == 1
        assert stub.calls == []

    def test_failed_publish_exits_and_removes_zip(self, publish_workspace, monkeypatch):
        stub = GhStub(
            fail_when=lambda cmd: len(cmd) > 1 and cmd[1] in ("create", "upload", "edit"),
            responses={("release", "view"): None},
        )
        saved = []
        stub_hash_state(monkeypatch, current="new", last=None, saved=saved)

        with pytest.raises(SystemExit) as exc:
            run_with_stub(stub, release_handler.main)

        assert exc.value.code == 1
        assert saved == [], "发布失败不得保存哈希，否则下次不会重试"
        assert "create" in stub.release_actions()

    def test_prunes_old_releases_and_keeps_current(self, publish_workspace, monkeypatch):
        now_id = hashlib.sha256(b"new").hexdigest()[:12]
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": f"rules-{now_id}", "createdAt": old},
            {"tagName": "rules-19990101-0000", "createdAt": old},
            {"tagName": "v1.0.0", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)

        run_with_stub(stub, release_handler.main)

        flat = " ".join(stub.commands())
        assert "release delete rules-19990101-0000 --yes" in flat
        assert f"release delete rules-{now_id}" not in flat, "本轮标签不得被删"
        assert "git/refs/tags/rules-19990101-0000" in flat, "删除 release 后应同时删 tag"
        assert "v1.0.0" not in flat, "非 rules- 前缀的 Release 一律不得触碰"

    def test_prune_never_touches_manual_releases(self, publish_workspace, monkeypatch):
        old = (release_handler.beijing_now() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "v1.0.0", "createdAt": old},
            {"tagName": "v1.2.3-rc1", "createdAt": old},
            {"tagName": "manual-notes", "createdAt": old},
            {"tagName": "rules-manual", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)

        run_with_stub(stub, release_handler.main)

        flat = " ".join(stub.commands())
        assert "delete" not in flat, f"人工 Release 不得被删: {flat}"
        assert "git/refs/tags" not in flat

    def test_prune_delete_failure_is_not_fatal(self, publish_workspace, monkeypatch):
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "rules-19990101-0000", "createdAt": old},
            {"tagName": "rules-19990102-0000", "createdAt": old},
        )
        stub = GhStub(
            fail_when=lambda cmd: len(cmd) > 1 and cmd[1] == "delete", responses={("release", "list"): listing}
        )
        saved = []
        stub_hash_state(monkeypatch, saved=saved)

        run_with_stub(stub, release_handler.main)

        flat = " ".join(stub.commands())
        assert flat.count("release delete") == 2, "首个删除失败后仍应继续清理其余旧 Release"
        assert "git/refs/tags" not in flat, "Release 删除失败时不得继续删 tag"
        assert saved == ["new"], "清理失败不得跳过 state/release.sha256 的写入"
        zip_name = f"merged-rules-{hashlib.sha256(b'new').hexdigest()[:12]}.zip"
        assert not os.path.exists(zip_name), "清理失败后 main() 仍应执行到收尾"
        assert not os.path.exists("SHA256SUMS"), "收尾应同时清理临时 SHA256SUMS"

    def test_step_summary_written(self, publish_workspace, monkeypatch):
        summary = publish_workspace / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        stub = GhStub()
        stub_hash_state(monkeypatch)

        run_with_stub(stub, release_handler.main)

        text = summary.read_text(encoding="utf-8")
        assert "发布报告" in text
        assert "文本规则" in text and "MRS 规则" in text

    def test_unparsable_release_listing_degrades_to_warning(self, publish_workspace, monkeypatch):
        stub = GhStub(responses={("release", "list"): "not-json"})
        stub_hash_state(monkeypatch)

        run_with_stub(stub, release_handler.main)

        flat = " ".join(stub.commands())
        assert "release delete" not in flat, "列表无法解析时不得误删任何 Release"

    def test_unparsable_entry_is_skipped_and_others_pruned(self, publish_workspace, monkeypatch):
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "rules-broken", "createdAt": "not-a-date"},
            {"tagName": "rules-19990101-0000", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)

        run_with_stub(stub, release_handler.main)

        flat = " ".join(stub.commands())
        assert "release delete rules-19990101-0000 --yes" in flat
        assert "rules-broken" not in flat, "无法解析的记录应跳过而非中断清理"


class TestOrphanTagCleanup:
    REFS_ENDPOINT = "repos/{owner}/{repo}/git/matching-refs/tags/rules-"

    def test_deletes_only_old_orphan_automatic_tags(self, monkeypatch):
        old = "2020-01-01T00:00:00Z"
        recent = "2999-01-01T00:00:00Z"
        refs = "\n".join(
            f"refs/tags/{tag}"
            for tag in (
                "rules-aaaaaaaaaaaa",
                "rules-bbbbbbbbbbbb",
                "rules-cccccccccccc",
                "rules-dddddddddddd",
                "rules-eeeeeeeeeeee",
                "rules-manual",
                "rules-1999-01-01",
                "v1.0.0",
            )
        )
        stub = GhStub(
            responses={
                ("api", self.REFS_ENDPOINT): refs,
                ("api", "repos/{owner}/{repo}/commits/rules-aaaaaaaaaaaa"): old,
                ("api", "repos/{owner}/{repo}/commits/rules-bbbbbbbbbbbb"): recent,
                ("api", "repos/{owner}/{repo}/commits/rules-eeeeeeeeeeee"): "not-a-date",
            }
        )
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        cleaned = release_handler.cleanup_orphan_rules_tags(
            {"rules-cccccccccccc"}, "rules-dddddddddddd", cutoff
        )

        assert cleaned == 1
        deletes = [call for call in stub.calls if call[:3] == ["api", "-X", "DELETE"]]
        assert deletes == [
            ["api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/tags/rules-aaaaaaaaaaaa"]
        ]
        flat = " ".join(stub.commands())
        assert "commits/rules-cccccccccccc" not in flat
        assert "commits/rules-dddddddddddd" not in flat
        assert "commits/rules-manual" not in flat

    def test_orphan_delete_is_retried_by_next_reconciliation(self, monkeypatch):
        delete_attempts = []

        def fail_first_delete(cmd):
            if cmd[:3] == ["api", "-X", "DELETE"]:
                delete_attempts.append(cmd)
                return len(delete_attempts) == 1
            return False

        stub = GhStub(
            fail_when=fail_first_delete,
            responses={
                ("api", self.REFS_ENDPOINT): "refs/tags/rules-aaaaaaaaaaaa",
                ("api", "repos/{owner}/{repo}/commits/rules-aaaaaaaaaaaa"): "2020-01-01T00:00:00Z",
            },
        )
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == 0
        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == 1

        assert len(delete_attempts) == 2


class TestRunGhErrorHandling:
    def test_oserror_exits_nonzero(self):
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

    def test_bytes_download_enforces_asset_size_cap(self, monkeypatch):
        def fake_run(_cmd, **kwargs):
            assert "capture_output" not in kwargs
            kwargs["stdout"].write(b"x" * 100)
            return subprocess.CompletedProcess(_cmd, 0, stderr=b"")

        monkeypatch.setattr(release_handler.subprocess, "run", fake_run)
        assert release_handler.run_gh_bytes(["release", "download"], max_bytes=10) is None


@pytest.fixture
def verify_workspace(work_dir, monkeypatch):
    monkeypatch.setattr(release_handler, "RULESETS_DIR", "rulesets")
    monkeypatch.setattr(release_handler, "MERGED_DIR", "merged-rules")
    monkeypatch.setattr(release_handler, "MRS_DIR", "merged-rules-mrs")
    monkeypatch.setattr(release_handler, "merge_product_entries", lambda *a, **k: set())
    (work_dir / "rulesets").mkdir(parents=True)
    manifest.save_manifest(
        work_dir / "rulesets" / "products.manifest",
        {"block/domain/A/x.txt": "0" * 64},
        generation="verify-gen",
    )
    (work_dir / "merged-rules" / "block" / "domain" / "A").mkdir(parents=True)
    (work_dir / "merged-rules" / "block" / "domain" / "A" / "x.txt").write_text("x", encoding="utf-8")
    manifest.save_manifest(
        work_dir / "merged-rules" / "products.manifest",
        {"block/domain/A/x.txt": hashlib.sha256(b"x").hexdigest()},
        generation="verify-gen",
    )
    mrs_dir = work_dir / "merged-rules-mrs" / "block" / "domain" / "A"
    mrs_dir.mkdir(parents=True)
    (mrs_dir / "x.mrs").write_text("y", encoding="utf-8")
    monkeypatch.chdir(work_dir)
    return work_dir


class TestVerifyOnly:
    def test_paired_products_pass_without_gh(self, verify_workspace):
        stub = GhStub()
        run_with_stub(stub, release_handler.verify_only)
        assert stub.calls == [], "预检不应调用 gh"

    def test_missing_mrs_exits_nonzero(self, verify_workspace):
        os.unlink("merged-rules-mrs/block/domain/A/x.mrs")
        stub = GhStub()
        with pytest.raises(SystemExit) as exc:
            run_with_stub(stub, release_handler.verify_only)
        assert exc.value.code == 1
        assert stub.calls == []

    def test_empty_mrs_exits_nonzero(self, verify_workspace):
        (verify_workspace / "merged-rules-mrs/block/domain/A/x.mrs").write_bytes(b"")
        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only()
        assert exc.value.code == 1

    def test_non_regular_mrs_is_explicitly_rejected(self, verify_workspace, caplog):
        target = verify_workspace / "merged-rules-mrs/block/domain/A/x.mrs"
        target.unlink()
        target.mkdir()
        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only()
        assert exc.value.code == 1
        assert "非普通文件" in caplog.text

    def test_tampered_merged_product_is_fatal(self, verify_workspace):
        target = verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt"
        target.write_text("tampered", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only()
        assert exc.value.code == 1

    def test_missing_baseline_exits_nonzero_when_rulesets_present(self, verify_workspace):
        os.unlink("rulesets/products.manifest")
        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only()
        assert exc.value.code == 1

    def test_main_dispatches_verify_only_without_publishing(self, verify_workspace, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--verify-only"])
        monkeypatch.setattr(release_handler, "verify_only", lambda: calls.append(True))
        run_with_stub(GhStub(), release_handler.main)
        assert calls == [True], "main() 应分发到 verify_only 并提前返回"

    def test_main_dispatches_print_hash_without_publishing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--print-hash"])
        monkeypatch.setattr(
            release_handler,
            "combined_products_hash",
            lambda *a, **k: ("abc|def|1|1", 1, 1),
        )
        monkeypatch.setattr(release_handler, "product_dirs", lambda: ("txt", "mrs"))
        release_handler.main()
        assert capsys.readouterr().out.strip() == "abc|def|1|1"


class TestAssetConfirmation:
    FULL = frozenset({"z.zip", "SHA256SUMS"})
    PARTIAL = frozenset({"z.zip"})

    def stub_release_flow(self, monkeypatch, calls, names):
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("new-hash", 1, 1))
        monkeypatch.setattr(release_handler, "product_dirs", lambda: ("txt", "mrs"))
        monkeypatch.setattr(release_handler, "enforce_products", lambda *a: calls.append("enforce"))
        monkeypatch.setattr(release_handler, "expected_assets", lambda tag_id: set(self.FULL))
        monkeypatch.setattr(release_handler, "zip_target_files", lambda d: ("z.zip", {}))
        monkeypatch.setattr(release_handler, "write_sha256sums", lambda *a: "SHA256SUMS")
        monkeypatch.setattr(release_handler, "generate_release_notes", lambda *a: "notes")
        monkeypatch.setattr(release_handler, "publish_release", lambda *a, **k: "ok")
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a: (True, "ok"))
        monkeypatch.setattr(release_handler, "release_asset_names", lambda tag: names)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda *a: None)
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "")
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "hash")
        monkeypatch.setattr(release_handler.os.path, "exists", lambda p: False)

    def test_verification_runs_even_with_change_detection_off(self, monkeypatch):
        calls = []
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", False)
        self.stub_release_flow(monkeypatch, calls, names=self.FULL)

        release_handler.main()

        assert calls == ["enforce"]

    def test_publish_without_assets_is_a_failure(self, monkeypatch, caplog):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        self.stub_release_flow(monkeypatch, [], names=set())

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert "缺少资产" in caplog.text

    def test_partial_assets_are_a_failure(self, monkeypatch, caplog):
        """zip 在但 SHA256SUMS 缺失时必须判定失败，否则哈希落盘后不再重试。"""
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        saved = []
        self.stub_release_flow(monkeypatch, [], names=self.PARTIAL)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert "缺少资产" in caplog.text
        assert "SHA256SUMS" in caplog.text
        assert saved == []

    def test_unconfirmed_asset_count_is_a_failure(self, monkeypatch, caplog):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        saved = []
        self.stub_release_flow(monkeypatch, [], names=None)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert "无法确认" in caplog.text
        assert saved == []

    def test_asset_query_is_retried_before_succeeding(self, monkeypatch):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        attempts = []
        saved = []
        self.stub_release_flow(monkeypatch, [], names=None)

        def flaky(tag):
            attempts.append(tag)
            return None if len(attempts) == 1 else set(self.FULL)

        monkeypatch.setattr(release_handler, "release_asset_names", flaky)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        release_handler.main()

        assert len(attempts) == 2
        assert saved == ["new-hash"]

    def test_zero_asset_count_is_retried_before_succeeding(self, monkeypatch):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        attempts = []
        saved = []
        self.stub_release_flow(monkeypatch, [], names=None)

        def flaky(tag):
            attempts.append(tag)
            return set() if len(attempts) == 1 else set(self.FULL)

        monkeypatch.setattr(release_handler, "release_asset_names", flaky)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        release_handler.main()

        assert len(attempts) == 2
        assert saved == ["new-hash"]

    def test_content_mismatch_after_upload_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        saved = []
        self.stub_release_flow(monkeypatch, [], names=self.FULL)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a: (False, "bad zip"))
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert saved == []

    def test_content_confirmation_is_retried_before_succeeding(self, monkeypatch):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        attempts = []
        saved = []
        self.stub_release_flow(monkeypatch, [], names=self.FULL)

        def verify(*args):
            attempts.append(args)
            return (len(attempts) > 1, "mismatch" if len(attempts) == 1 else "ok")

        monkeypatch.setattr(release_handler, "verify_release_reusable", verify)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        release_handler.main()

        assert len(attempts) == 2
        assert saved == ["new-hash"]

    def test_release_asset_names_parses_gh_output(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "z.zip\nSHA256SUMS\n")
        assert release_handler.release_asset_names("rules-2026-10-06") == {"z.zip", "SHA256SUMS"}
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "")
        assert release_handler.release_asset_names("rules-2026-10-06") == set()
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: None)
        assert release_handler.release_asset_names("rules-2026-10-06") is None

    def test_confirm_requires_expected_names(self, monkeypatch):
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        monkeypatch.setattr(release_handler, "release_asset_names", lambda tag: set(self.PARTIAL))
        assert release_handler.confirm_release_assets("t", set(self.FULL)) == self.PARTIAL, "不全时应返回最后集合"
        monkeypatch.setattr(release_handler, "release_asset_names", lambda tag: set(self.FULL))
        assert release_handler.confirm_release_assets("t", set(self.FULL)) == self.FULL
