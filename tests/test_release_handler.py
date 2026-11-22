import datetime
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import manifest
import pytest
import release_handler
from support import GhStub, domain_mrs_bytes, ipcidr_mrs_bytes, write_paired_product


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
    @pytest.mark.parametrize(
        ("current", "last", "enabled", "force", "expected", "reason"),
        [
            ("h", "h", False, False, True, "关闭"),
            ("h", None, True, False, True, "首次"),
            ("h", "", True, False, True, "首次"),
            ("new", "old", True, False, True, "变化"),
            ("same", "same", True, False, False, "无变化"),
            ("same", "same", True, True, True, "强制"),
            ("same", "same", False, True, True, "强制"),
        ],
        ids=["disabled", "first", "empty-last", "changed", "same", "force-same", "force-disabled"],
    )
    def test_publish_decision(self, current, last, enabled, force, expected, reason):
        publish, why = release_handler.should_publish(current, last, enabled, force=force)
        assert publish is expected
        assert reason in why


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


class TestReleaseAssetOutput:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("rules.zip\t\nSHA256SUMS\t\n", {"rules.zip": "", "SHA256SUMS": ""}),
            ("rules.zip\tsha256:abcd\nSHA256SUMS\t\n", {"rules.zip": "sha256:abcd", "SHA256SUMS": ""}),
            ("rules.zip\t\nSHA256SUMS\tsha256:abcd\n", {"rules.zip": "", "SHA256SUMS": "sha256:abcd"}),
            ("rules.zip\tsha256:abcd\n", {"rules.zip": "sha256:abcd"}),
            ("rules.zip\t\r\nSHA256SUMS\t\r\n", {"rules.zip": "", "SHA256SUMS": ""}),
            ("rules.zip\n", None),
            ("rules.zip\t\textra\n", None),
        ],
        ids=["all-empty", "last-empty", "first-empty", "digest", "crlf", "missing-field", "extra-field"],
    )
    def test_process_output_preserves_empty_fields_and_rejects_bad_records(self, monkeypatch, output, expected):
        monkeypatch.setattr(
            release_handler.subprocess, "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
        )

        assert release_handler.release_assets("rules-test") == expected


class TestParseSha256sums:
    def test_duplicate_path_is_rejected(self):
        text = f"{'a' * 64}  a.txt\n{'b' * 64}  a.txt\n"
        with pytest.raises(ValueError, match="重复"):
            release_handler.parse_sha256sums(text)

    def test_valid_entries_are_parsed(self):
        text = f"{'a' * 64}  a.txt\n{'b' * 64}  dir/b.txt\n"
        assert release_handler.parse_sha256sums(text) == {"a.txt": "a" * 64, "dir/b.txt": "b" * 64}


class TestReleaseReusable:
    def test_reusable_when_digests_match(self, publish_workspace, release_planning_api, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_rejects_wrong_file_hash(self, publish_workspace, release_planning_api, monkeypatch):
        stub, _zip = remote_release_stub(
            publish_workspace, "abc123abc123", corrupt_file="merged-rules/block/domain/A/x.txt"
        )
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "逐文件哈希" in why

    def test_rejects_zip_digest_mismatch(self, publish_workspace, release_planning_api, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", corrupt_zip_digest=True)
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "zip 的远端 digest" in why

    def test_rejects_mismatched_nonempty_api_digest(self, publish_workspace, release_planning_api, monkeypatch):
        stub, zip_name = remote_release_stub(publish_workspace, "abc123abc123")
        stub.asset_digests[zip_name] = "sha256:" + "0" * 64
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "zip 的远端 digest" in why

    def test_rejects_missing_digest(self, publish_workspace, release_planning_api, monkeypatch):
        stub, zip_name = remote_release_stub(
            publish_workspace, "abc123abc123", corrupt_file="merged-rules/block/domain/A/x.txt"
        )
        stub.asset_digests[zip_name] = ""
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is False
        assert "逐文件哈希" in why

    def test_allows_empty_api_digests_when_all_content_matches(
        self, publish_workspace, release_planning_api, monkeypatch
    ):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", empty_digests=True)
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_crlf_sha256sums_uses_raw_bytes_for_digest(self, publish_workspace, release_planning_api, monkeypatch):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", newline="\r\n")
        patch_remote_release(monkeypatch, stub)
        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")
        assert ok is True, why

    def test_invalid_utf8_is_a_reuse_failure_not_an_exception(
        self, publish_workspace, release_planning_api, monkeypatch
    ):
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123")
        invalid = b"\xff\xfe"
        stub.asset_bytes["SHA256SUMS"] = invalid
        stub.asset_digests["SHA256SUMS"] = "sha256:" + hashlib.sha256(invalid).hexdigest()
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "UTF-8" in why

    def test_rejects_wrong_zip_with_self_consistent_sums(self, publish_workspace, release_planning_api, monkeypatch):
        entries = [
            (rel, b"remote-tampered" if rel.endswith(".txt") else data)
            for rel, data in workspace_product_bytes(publish_workspace).items()
        ]
        stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", zip_entries=entries)
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "ZIP" in why

    def test_rejects_duplicate_zip_entries(self, publish_workspace, release_planning_api, monkeypatch):
        entries = list(workspace_product_bytes(publish_workspace).items())
        entries.append(("merged-rules/block/domain/A/x.txt", b"x"))
        with pytest.warns(UserWarning, match="Duplicate name"):
            stub, _zip = remote_release_stub(publish_workspace, "abc123abc123", zip_entries=entries)
        patch_remote_release(monkeypatch, stub)

        ok, why = release_handler.verify_release_reusable("rules-abc123abc123", "abc123abc123")

        assert ok is False
        assert "重复" in why

    @pytest.mark.parametrize("extra_name", ["../escape.txt", "merged-rules/A/extra.txt"])
    def test_rejects_traversal_and_extra_zip_entries(
        self, publish_workspace, release_planning_api, monkeypatch, extra_name
    ):
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
    def test_enforces_zip_read_limits(self, publish_workspace, release_planning_api, monkeypatch, limit_name, value):
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

    def test_entry_limit_is_checked_before_zipfile_initialization(
        self, publish_workspace, release_planning_api, monkeypatch
    ):
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

    def test_oversized_zip_asset_is_reused_failure(self, publish_workspace, release_planning_api, monkeypatch):
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
        tag = current_expected_tag()
        stub = GhStub(latest_tag=tag)
        monkeypatch.setattr(release_handler, "run_gh", stub)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, "bad"))
        assert release_handler.needs_release_repair("new") is True

    def test_no_repair_when_tag_and_digests_match(self, monkeypatch):
        tag = current_expected_tag()
        stub = GhStub(latest_tag=tag)
        monkeypatch.setattr(release_handler, "run_gh", stub)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))
        assert release_handler.needs_release_repair("new") is False


class TestNeedsPublishMode:
    def test_prints_false_when_unchanged_and_release_complete(self, release_planning_api, monkeypatch, capsys):
        release_planning_api.publish_existing()
        capsys.readouterr()
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "h")
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--needs-publish"])

        release_handler.main()

        assert capsys.readouterr().out.strip() == "false"

    def test_prints_true_when_repair_needed(self, release_planning_api, monkeypatch, capsys):
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 1, 1))
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "h")
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

    @pytest.mark.parametrize(
        ("limit_name", "value", "content"),
        [
            ("MAX_ZIP_ENTRIES", 1, b"x"),
            ("MAX_ZIP_ENTRY_SIZE", 1, b"xx"),
            ("MAX_ZIP_TOTAL_SIZE", 1, b"xx"),
            ("MAX_ZIP_ASSET_BYTES", 1, b"x"),
        ],
    )
    def test_rejects_oversized_products_before_upload(self, zip_workspace, monkeypatch, limit_name, value, content):
        (zip_workspace / "merged-rules" / "A").mkdir(parents=True)
        (zip_workspace / "merged-rules" / "A" / "x.txt").write_bytes(content)
        (zip_workspace / "merged-rules" / "A" / "y.txt").write_bytes(b"y")
        monkeypatch.setattr(release_handler, limit_name, value)

        with pytest.raises(SystemExit) as exc:
            release_handler.zip_target_files("2026-10-05")

        assert exc.value.code == 1
        assert not (zip_workspace / "merged-rules-2026-10-05.zip").exists()


@pytest.fixture
def publish_workspace(work_dir, monkeypatch):
    monkeypatch.setattr(release_handler, "REPO_ROOT", "")
    monkeypatch.setattr(release_handler, "TARGET_CONFIG", {"merged-rules": ".txt", "merged-rules-mrs": ".mrs"})
    monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
    monkeypatch.setattr(release_handler, "KEEP_DAYS", 3)
    write_paired_product(work_dir, "block/domain/A/x.txt")
    monkeypatch.chdir(work_dir)
    return work_dir


def expected_tag(product_hash):
    return "rules-" + hashlib.sha256(product_hash.encode()).hexdigest()[:12]


def current_expected_tag():
    return "rules-" + release_handler.release_content_digest()[:12]


def stub_hash_state(monkeypatch, current="new", last=None, saved=None):
    monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: (current, 1, 1))
    monkeypatch.setattr(release_handler, "load_last_hash", lambda: last)
    monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
    if saved is not None:
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))
    else:
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: None)


class ReleaseState:
    def __init__(self, root, tag):
        self.root = root
        self.tag = tag
        self.sha = os.environ["GITHUB_SHA"]
        self.tags = {}
        self.tag_objects = {}
        self.builds = {}
        self.release = None
        self.assets = {}
        self.next_asset_id = 1
        self.latest = None
        self.calls = []

    def add_tag(self, sha=None, tag=None, generation=""):
        tag = tag or self.tag
        oid = hashlib.sha256(tag.encode()).hexdigest()[:40]
        record = {
            "sha": sha or self.sha, "digest": release_handler.release_content_digest(), "generation": generation,
            "repo": "rksk102/ruleset-foundry", "ref": "refs/heads/main",
            "path": ".github/workflows/pipeline.yml",
        }
        obj = {
            "sha": oid, "tag": tag, "object": {"type": "commit", "sha": record["sha"]},
            "message": release_handler.PROVENANCE_PREFIX + json.dumps(record, sort_keys=True, separators=(",", ":")),
        }
        self.tag_objects[oid] = self.tags[tag] = obj

    def add_asset(self, name, content, state="uploaded"):
        self.assets[name] = {
            "name": name, "id": self.next_asset_id, "state": state, "size": len(content),
            "digest": "sha256:" + hashlib.sha256(content).hexdigest() if state == "uploaded" else None,
            "content": content,
        }
        self.next_asset_id += 1

    def add_build(self, run_id, attempt, sha):
        repo = {"full_name": "rksk102/ruleset-foundry"}
        self.builds[run_id, attempt] = {
            "id": run_id, "run_attempt": attempt, "head_branch": "main", "head_sha": sha,
            "path": ".github/workflows/pipeline.yml", "event": "schedule",
            "repository": repo, "head_repository": repo,
        }

    def __call__(self, cmd, strip=True):
        self.calls.append(cmd)
        if cmd[:2] == ["release", "edit"]:
            self.latest = self.tag
            return "ok"
        if cmd[:2] == ["release", "list"]:
            return "[]"
        if cmd[:2] == ["api", "-X"]:
            cmd = ["api", cmd[3], *cmd[1:3], *cmd[4:]]
        if cmd[0] != "api":
            return "ok"
        endpoint = cmd[1]
        method = "GET"
        for flag in ("--method", "-X"):
            if flag in cmd:
                method = cmd[cmd.index(flag) + 1]
        if endpoint == "repos/{owner}/{repo}/releases":
            if method == "GET":
                return "" if self.release is None else json.dumps(self.release)
            assert method == "POST", cmd
            body = next(x[5:] for x in cmd if x.startswith("body="))
            self.release = {
                "id": 123, "tag_name": self.tag, "draft": True, "body": body,
                "target_commitish": next(x[17:] for x in cmd if x.startswith("target_commitish=")),
                "author": {"login": "github-actions[bot]"},
                "upload_url": "https://uploads.github.com/repos/rksk102/ruleset-foundry/releases/123/assets{?name,label}",
            }
            return json.dumps(self.release)
        if endpoint == "repos/{owner}/{repo}/releases/123/assets":
            return "\n".join(json.dumps({k: v for k, v in a.items() if k != "content"}) for a in self.assets.values())
        if endpoint.startswith("https://uploads.github.com/") and "--input" in cmd:
            path = Path(cmd[cmd.index("--input") + 1])
            if self.release is None or not self.release["draft"] or path.name in self.assets:
                return None
            self.add_asset(path.name, path.read_bytes())
            return "{}"
        if endpoint == "repos/{owner}/{repo}/releases/123":
            if method == "PATCH" and "draft=false" in cmd:
                self.release["draft"] = False
            if method == "PATCH" and "make_latest=true" in cmd:
                self.latest = self.tag
            return json.dumps(self.release)
        if endpoint == f"repos/{{owner}}/{{repo}}/releases/tags/{self.tag}":
            if self.release is None:
                return None
            return "\n".join(f"{name}\t" for name in self.assets)
        if method == "DELETE":
            if endpoint.startswith("repos/{owner}/{repo}/releases/assets/"):
                asset_id = int(endpoint.rsplit("/", 1)[-1])
                name = next((a["name"] for a in self.assets.values() if a["id"] == asset_id), None)
                if name is None:
                    return None
                del self.assets[name]
            return ""
        if endpoint == "repos/{owner}/{repo}/git/tags":
            sha = next(x[7:] for x in cmd if x.startswith("object="))
            tag = next(x[4:] for x in cmd if x.startswith("tag="))
            message = next(x[8:] for x in cmd if x.startswith("message="))
            oid = hashlib.sha256((tag + sha + message).encode()).hexdigest()[:40]
            self.tag_objects[oid] = {
                "sha": oid, "tag": tag, "object": {"type": "commit", "sha": sha}, "message": message,
            }
            return json.dumps(self.tag_objects[oid])
        if endpoint == "repos/{owner}/{repo}/git/refs":
            tag = next(x[14:] for x in cmd if x.startswith("ref=refs/tags/"))
            oid = next(x[4:] for x in cmd if x.startswith("sha="))
            if tag in self.tags:
                return None
            self.tags[tag] = self.tag_objects[oid]
            return "created"
        if endpoint.startswith("repos/{owner}/{repo}/git/matching-refs/tags/"):
            tag = endpoint.removeprefix("repos/{owner}/{repo}/git/matching-refs/tags/")
            if tag == "rules-":
                return "\n".join(f"refs/tags/{name}" for name in self.tags) if "--jq" in cmd else ""
            obj = self.tags.get(tag)
            if obj is None:
                if re.fullmatch(r"rules-[0-9a-f]{12}", tag):
                    return ""
                raise release_handler.ReleaseError(f"未知 tag: {tag}")
            return f"{'tag' if 'sha' in obj else 'commit'}\t{obj.get('sha', obj['object']['sha'])}"
        if endpoint.startswith("repos/{owner}/{repo}/git/tags/"):
            object_sha = endpoint.rsplit("/", 1)[-1]
            obj = self.tag_objects.get(object_sha) or next(
                (obj for obj in self.tags.values() if obj["sha"] == object_sha), None,
            )
            return json.dumps(obj) if obj is not None else None
        attempt_path = re.fullmatch(r"repos/\{owner\}/\{repo\}/actions/runs/(\d+)/attempts/(\d+)(/jobs)?", endpoint)
        if attempt_path:
            run = self.builds.get((int(attempt_path[1]), int(attempt_path[2])))
            if run is None:
                return None
            return f"{run['head_sha']}\tsuccess" if attempt_path[3] else json.dumps(run)
        raise AssertionError(cmd)

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def bytes(self, cmd, max_bytes=None):
        if cmd[0] == "release":
            name = cmd[cmd.index("-p") + 1]
            asset = self.assets.get(name)
        elif cmd[0] == "api":
            asset_id = int(cmd[1].rsplit("/", 1)[-1])
            asset = next((a for a in self.assets.values() if a["id"] == asset_id), None)
        else:
            raise AssertionError(cmd)
        if asset is None:
            return None
        content = asset["content"]
        return None if max_bytes is not None and len(content) > max_bytes else content

    def publish_current_bytes(self):
        zip_name, file_map = release_handler.zip_target_files(self.tag[6:])
        sums_name = release_handler.write_sha256sums(zip_name, file_map)
        for name in (zip_name, sums_name):
            path = Path(name)
            self.add_asset(name, path.read_bytes())
            path.unlink()

    def publish_existing(self, draft=False, generation="", *, lightweight_legacy=False):
        if lightweight_legacy:
            self.tags[self.tag] = {"object": {"type": "commit", "sha": self.sha}}
        else:
            self.add_tag(generation=generation)
        self.publish_current_bytes()
        first_provenance = self.tags[self.tag].get("message", "").splitlines()[:1]
        self.release = {
            "id": 123, "tag_name": self.tag, "draft": draft,
            "body": first_provenance[0] if first_provenance else "旧版自动构建说明", "target_commitish": self.sha,
            "author": {"login": "github-actions[bot]"},
            "upload_url": "https://uploads.github.com/repos/rksk102/ruleset-foundry/releases/123/assets{?name,label}",
        }
        self.latest = self.tag if not draft else None
        return self.release

    def run(self):
        plan = release_handler.prepare_release_plan(self.tag, release_handler.release_content_digest())
        release_handler.process_release(
            self.tag, release_handler.release_content_digest(), plan, "2026-11-21", "20:00:00"
        )


@pytest.fixture(autouse=True)
def release_test_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PUBLISH_BUILD_GENERATION", raising=False)
    monkeypatch.delenv("PUBLISH_GUARD", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "rksk102/ruleset-foundry")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")


@pytest.fixture
def release_state(publish_workspace, monkeypatch):
    state = ReleaseState(publish_workspace, current_expected_tag())
    monkeypatch.setattr(release_handler, "run_gh", state)
    monkeypatch.setattr(release_handler, "run_gh_bytes", state.bytes)
    return state


class TestReleaseObjectContracts:
    def test_missing_tag_is_created_at_build_sha_before_release(self, release_state, monkeypatch):
        build_sha = "a" * 40
        monkeypatch.setenv("GITHUB_SHA", build_sha)

        release_state.run()

        tag = release_state.tags[release_state.tag]
        create_ref = next(call for call in release_state.calls if call[:2] == ["api", "repos/{owner}/{repo}/git/refs"])
        assert f"ref=refs/tags/{release_state.tag}" in create_ref
        assert f"sha={tag['sha']}" in create_ref
        assert tag["object"]["sha"] == build_sha
        assert release_state.release["target_commitish"] == build_sha

    @pytest.mark.parametrize("concurrent_sha", ["a" * 40, "b" * 40], ids=["matching", "mismatched"])
    def test_concurrent_tag_target_is_rechecked_before_release(self, release_state, monkeypatch, concurrent_sha):
        original = release_state.__call__

        def concurrent_create(cmd, strip=True):
            if cmd[:2] == ["api", "repos/{owner}/{repo}/git/refs"]:
                release_state.add_tag(sha=concurrent_sha)
            return original(cmd, strip=strip)

        monkeypatch.setattr(release_handler, "run_gh", concurrent_create)
        if concurrent_sha == "a" * 40:
            release_state.run()
            assert release_state.latest == release_state.tag
        else:
            with pytest.raises(release_handler.ReleaseError, match="拒绝"):
                release_state.run()
            assert release_state.release is None

    def test_new_release_targets_build_sha_instead_of_moving_default_branch(self, release_state, monkeypatch):
        build_sha = "a" * 40
        monkeypatch.setenv("GITHUB_SHA", build_sha)
        release_state.run()
        assert release_state.release["target_commitish"] == build_sha
        assert release_state.tags[release_state.tag]["object"]["sha"] == build_sha

    def test_orphan_tag_with_different_target_prevents_remote_creation(self, release_state):
        release_state.tags[release_state.tag] = {
            "sha": "d" * 40, "tag": release_state.tag,
            "object": {"type": "commit", "sha": "b" * 40}, "message": "unknown",
        }
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release is None

    def test_orphan_tag_target_lookup_failure_prevents_remote_creation(self, release_state, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: None)
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release is None

    def test_matching_annotated_orphan_tag_allows_release_creation(self, release_state):
        release_state.add_tag()
        release_state.run()
        assert release_state.release["draft"] is False
        assert release_state.latest == release_state.tag

    def test_local_creation_resolves_full_sha_from_repo_head(self, release_state, monkeypatch):
        monkeypatch.delenv("GITHUB_SHA")
        sha = "c" * 40
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, sha + "\n", "")

        monkeypatch.setattr(release_handler.subprocess, "run", run)
        release_state.run()
        assert calls[0][0] == ["git", "rev-parse", "HEAD"]
        assert calls[0][1]["cwd"] == (release_handler.REPO_ROOT or None)
        assert release_state.tags[release_state.tag]["object"]["sha"] == sha

    @pytest.mark.parametrize("sha", ["", "abc123", "main", "a" * 41])
    def test_invalid_build_sha_prevents_remote_creation(self, release_state, monkeypatch, sha):
        monkeypatch.setenv("GITHUB_SHA", sha)
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release is None and release_state.latest is None

    def test_unavailable_local_head_prevents_remote_creation(self, release_state, monkeypatch):
        monkeypatch.delenv("GITHUB_SHA")
        def fail(command, **kwargs):
            raise subprocess.CalledProcessError(128, command, stderr="not a git repository")
        monkeypatch.setattr(release_handler.subprocess, "run", fail)
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release is None

    def test_existing_release_keeps_tag_when_build_sha_changes(self, release_state, monkeypatch):
        release_state.publish_existing()
        original_tag = release_state.tags[release_state.tag]
        monkeypatch.setenv("GITHUB_SHA", "b" * 40)

        release_state.run()

        assert release_state.tags[release_state.tag] is original_tag
        assert not any("--input" in call for call in release_state.calls)

    def test_existing_release_updates_in_place(self, release_state):
        release_state.publish_existing()
        before = {name: item["content"] for name, item in release_state.assets.items()}
        release_state.run()
        assert release_state.latest == release_state.tag
        assert before == {name: item["content"] for name, item in release_state.assets.items()}
        assert not any("--clobber" in call for call in release_state.calls)

    def test_extra_assets_uploaded_alongside_zip(self, release_state):
        release_state.run()
        uploads = [call for call in release_state.calls if "--input" in call]
        assert len(uploads) == 2
        assert {call[call.index("--input") + 1] for call in uploads} == set(release_state.assets)

    def test_extra_assets_included_on_create(self, release_state):
        release_state.run()
        assert set(release_state.assets) == release_handler.expected_assets(release_state.tag[6:])

    def test_missing_release_created(self, release_state, monkeypatch):
        def unexpected_process(*args, **kwargs):
            raise AssertionError("CI 构建 SHA 已提供，不应读取本地 HEAD")
        monkeypatch.setattr(release_handler.subprocess, "run", unexpected_process)
        release_state.run()
        assert release_state.release["draft"] is False
        assert release_state.latest == release_state.tag

    def test_upload_failure_skips_edit(self, release_state, monkeypatch):
        original = release_state.__call__
        monkeypatch.setattr(
            release_handler, "run_gh", lambda cmd: None if "--input" in cmd else original(cmd)
        )
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release["draft"] is True and release_state.latest is None

    def test_edit_failure_propagates(self, release_state, monkeypatch):
        original = release_state.__call__
        monkeypatch.setattr(
            release_handler, "run_gh", lambda cmd: None if "make_latest=true" in cmd else original(cmd)
        )
        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.latest is None

    def test_create_failure_propagates(self, release_state, monkeypatch):
        posts = []

        def fail_create(cmd):
            if cmd[:4] == ["api", "repos/{owner}/{repo}/releases", "--method", "POST"]:
                posts.append(cmd)
                return None
            return release_state(cmd)

        monkeypatch.setattr(release_handler, "run_gh", fail_create)
        with pytest.raises(release_handler.ReleaseError, match="无法创建 Release 草稿"):
            release_state.run()
        assert len(posts) == 1
        assert release_state.release is None and release_state.latest is None

    def test_created_draft_survives_failed_readback(self, release_state, monkeypatch):
        api = release_state
        failed_readbacks = []

        def fail_readback(cmd):
            if cmd[:2] == ["api", "repos/{owner}/{repo}/releases"] and "--paginate" in cmd and api.release:
                failed_readbacks.append(cmd)
                return None
            return api(cmd)

        monkeypatch.setattr(release_handler, "run_gh", fail_readback)
        with pytest.raises(release_handler.ReleaseError, match="无法确认目标 Release 状态"):
            api.run()
        assert len(failed_readbacks) == 1
        assert api.release["draft"] is True and api.latest is None
        assert not api.assets

        monkeypatch.setattr(release_handler, "run_gh", api)
        api.run()

        assert api.release["draft"] is False and api.latest == api.tag
        assert sum(cmd[:4] == ["api", "repos/{owner}/{repo}/releases", "--method", "POST"] for cmd in api.calls) == 1

    def test_new_release_identity_uses_product_hash(self, release_state):
        first = release_state.tag
        product = release_state.root / "merged-rules/block/domain/A/x.txt"
        product.write_bytes(product.read_bytes() + b"# description\n")
        assert current_expected_tag() != first

    def test_complete_assets_with_bad_digest_are_reuploaded(self, release_state):
        release_state.publish_existing()
        release_state.assets[f"merged-rules-{release_state.tag[6:]}.zip"]["digest"] = "sha256:" + "0" * 64
        with pytest.raises(release_handler.ReleaseError, match="禁止覆盖"):
            release_state.run()
        assert not any("--input" in call for call in release_state.calls)

    def test_incomplete_release_for_same_hash_is_repaired(self, release_state):
        release_state.publish_existing(draft=True)
        release_state.assets.pop("SHA256SUMS")
        release_state.run()
        assert release_state.release["draft"] is False
        assert release_state.latest == release_state.tag

    def test_republishes_when_asset_incomplete(self, release_state):
        release_state.publish_existing()
        release_state.assets.pop("SHA256SUMS")
        with pytest.raises(release_handler.ReleaseError, match="禁止覆盖"):
            release_state.run()
        assert release_state.latest == release_state.tag

    def test_annotated_tag_is_dereferenced_to_its_commit(self, monkeypatch):
        stub = GhStub(responses={
            ("api", "repos/{owner}/{repo}/git/matching-refs/tags/rules-"): "refs/tags/rules-aaaaaaaaaaaa"
        })
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.cleanup_orphan_rules_tags(
            set(), "rules-current", datetime.datetime.now(datetime.UTC)
        ) == 0
        assert not any("/commits/" in call for call in stub.commands())

    def test_deletes_orphan_hash_tag_whose_commit_is_old(self, monkeypatch):
        stub = GhStub(responses={
            ("api", "repos/{owner}/{repo}/git/matching-refs/tags/rules-"): "refs/tags/rules-aaaaaaaaaaaa"
        })
        monkeypatch.setattr(release_handler, "run_gh", stub)
        assert release_handler.cleanup_orphan_rules_tags(
            set(), "rules-current", datetime.datetime.now(datetime.UTC)
        ) == 0
        assert stub.release_actions() == []


class TestDraftRecovery:
    def test_cross_commit_orphan_tag_resumes_with_verified_build(self, release_state, monkeypatch):
        original_sha = "b" * 40
        release_state.add_tag(sha=original_sha, generation="github-123-attempt-1")
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-124-attempt-2")
        original = release_state.__call__

        def platform(cmd, strip=True):
            if cmd[:2] == ["api", "repos/{owner}/{repo}/actions/runs/123/attempts/1"]:
                return json.dumps({
                    "id": 123, "run_attempt": 1, "head_branch": "main", "head_sha": original_sha,
                    "path": release_handler.WORKFLOW_PATH, "event": "schedule",
                    "repository": {"full_name": "rksk102/ruleset-foundry"},
                    "head_repository": {"full_name": "rksk102/ruleset-foundry"},
                })
            if cmd[:2] == ["api", "repos/{owner}/{repo}/actions/runs/123/attempts/1/jobs"]:
                return f"{original_sha}\tsuccess"
            return original(cmd, strip=strip)

        monkeypatch.setattr(release_handler, "run_gh", platform)
        release_state.run()

        assert release_state.latest == release_state.tag
        assert release_state.release["draft"] is False
        assert release_state.release["target_commitish"] == original_sha

    def test_new_release_uses_draft_then_publishes_and_marks_latest(self, release_state):
        release_state.run()

        assert release_state.release["draft"] is False
        assert release_state.latest == release_state.tag
        assert set(release_state.assets) == release_handler.expected_assets(release_state.tag[6:])
        assert not any("--clobber" in c for c in release_state.calls)

    def test_ref_create_failure_is_recovered_from_tag_object(self, release_state, monkeypatch):
        original = release_state.__call__
        def failed_ref(cmd, strip=True):
            if cmd[:2] == ["api", "repos/{owner}/{repo}/git/refs"]:
                release_state.add_tag()
                return None
            return original(cmd)
        monkeypatch.setattr(release_handler, "run_gh", failed_ref)

        release_state.run()

        assert release_state.latest == release_state.tag

    def test_unknown_orphan_tag_cannot_resume(self, release_state):
        release_state.tags[release_state.tag] = {
            "sha": "d" * 40, "tag": release_state.tag,
            "object": {"type": "commit", "sha": "b" * 40}, "message": "unknown",
        }

        with pytest.raises(release_handler.ReleaseError):
            release_state.run()
        assert release_state.release is None and release_state.latest is None

    def test_upload_rechecks_guard(self, release_state, monkeypatch):
        checks = []
        monkeypatch.setattr(release_handler.publish_guard, "ensure_current", lambda: checks.append("guard"))

        release_state.run()

        assert len(checks) >= 3


@pytest.fixture
def release_planning_api(publish_workspace, monkeypatch):
    state = ReleaseState(publish_workspace, current_expected_tag())
    monkeypatch.setattr(release_handler, "run_gh", state)
    monkeypatch.setattr(release_handler, "run_gh_bytes", state.bytes)
    monkeypatch.setattr(release_handler, "latest_rules_release_tag", lambda: state.latest)
    return state


class TestMainOrchestration:
    def test_invalid_build_sha_does_not_create_release_or_advance_state(self, release_planning_api, monkeypatch):
        monkeypatch.setenv("GITHUB_SHA", "short-sha")
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        with pytest.raises(SystemExit):
            release_handler.main()

        assert release_planning_api.release is None and release_planning_api.latest is None
        assert saved == []

    def test_mismatched_orphan_tag_does_not_create_release_or_advance_state(self, release_planning_api, monkeypatch):
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)
        release_planning_api.tags[release_planning_api.tag] = {
            "sha": "d" * 40, "message": "foreign", "object": {"type": "commit", "sha": "b" * 40},
        }

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert release_planning_api.release is None and release_planning_api.latest is None
        assert not any("POST" in cmd or "PATCH" in cmd for cmd in release_planning_api.calls)
        assert saved == []

    def test_publishes_when_hash_changed(self, release_planning_api, monkeypatch):
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        release_handler.main()

        assert saved == ["new"], "发布成功后应保存新哈希"
        assert not [f for f in os.listdir(".") if f.startswith("merged-rules-") and f.endswith(".zip")], (
            "发布后应删除临时 zip"
        )
        assert not os.path.exists("SHA256SUMS"), "发布后应删除临时 SHA256SUMS"

    def test_comment_changes_release_version_without_changing_semantic_hash(self, release_planning_api, monkeypatch):
        product_hash = release_handler.combined_products_hash(*release_handler.product_dirs())[0]
        saved = []
        stub_hash_state(monkeypatch, current=product_hash, last=None, saved=saved)
        release_handler.main()
        first_tag = release_planning_api.tag

        product = release_planning_api.root / "merged-rules/block/domain/A/x.txt"
        product.write_bytes(b"# Desc: changed\n" + product.read_bytes())
        assert release_handler.combined_products_hash(*release_handler.product_dirs())[0] == product_hash
        second_tag = current_expected_tag()
        assert first_tag != second_tag

        release_planning_api.tag = second_tag
        release_planning_api.release = None
        release_planning_api.assets = {}
        release_handler.main()
        assert saved == [product_hash, product_hash]
        assert release_planning_api.latest == second_tag

    def test_retry_reuses_complete_release_for_same_hash(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        release_planning_api.latest = None
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        release_handler.main()

        assert release_planning_api.latest == release_planning_api.tag
        assert not any("--input" in call for call in release_planning_api.calls)
        assert saved == ["new"]

    def test_reuse_latest_failure_does_not_save_hash(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        release_planning_api.latest = None
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: None if "make_latest=true" in cmd else release_planning_api(cmd),
        )

        with pytest.raises(SystemExit):
            release_handler.main()
        assert saved == []
        assert not any("--input" in call for call in release_planning_api.calls)

    def test_incomplete_public_release_is_not_repaired_in_place(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        release_planning_api.assets.pop("SHA256SUMS")
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        with pytest.raises(SystemExit):
            release_handler.main()
        assert not any("--input" in call for call in release_planning_api.calls)
        assert saved == []

    @pytest.mark.parametrize("why", ["bad digest", "remote asset lookup failed"])
    def test_public_asset_verification_failure_stops_before_writes(self, release_planning_api, monkeypatch, why):
        release_planning_api.publish_existing()
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, why))

        with pytest.raises(SystemExit):
            release_handler.main()
        assert not any("--input" in call for call in release_planning_api.calls)
        assert saved == []

    def test_content_confirmation_failure_does_not_mark_latest(self, release_planning_api, monkeypatch):
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, "bad content"))
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        with pytest.raises(release_handler.ReleaseError):
            release_planning_api.run()
        assert release_planning_api.release["draft"] is True and release_planning_api.latest is None

    @pytest.mark.parametrize("force", [False, True], ids=["unchanged", "forced"])
    def test_published_release_reused_when_original_actions_run_was_deleted(
        self, release_planning_api, monkeypatch, force,
    ):
        api = release_planning_api
        api.publish_existing(generation="github-123-attempt-1")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_SHA", "b" * 40)
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-124-attempt-1")
        monkeypatch.setenv("FORCE_RELEASE", "true" if force else "false")
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)

        release_handler.main()

        assert api.latest == api.tag
        assert saved == (["same"] if force else [])
        assert not any("--input" in call or "/actions/runs/123/" in call[1] for call in api.calls)

    def test_lightweight_legacy_published_release_is_reused_without_rewriting_tag(
        self, release_planning_api, monkeypatch,
    ):
        api = release_planning_api
        api.publish_existing(lightweight_legacy=True)
        api.latest = None
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_SHA", "b" * 40)
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-124-attempt-1")
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)

        release_handler.main()

        assert api.latest == api.tag and saved == ["same"]
        assert api.tags[api.tag]["object"] == {"type": "commit", "sha": api.sha}
        assert not any("--input" in cmd or "/git/tags" in cmd[1] for cmd in api.calls if len(cmd) > 1)

    @pytest.mark.parametrize("field", ["author", "target_commitish"])
    def test_lightweight_legacy_published_rejects_mismatched_release_owner(self, release_state, field):
        api = release_state
        api.publish_existing(lightweight_legacy=True)
        api.release[field] = {"login": "other-account"} if field == "author" else "b" * 40

        with pytest.raises(release_handler.ReleaseError, match="旧版已公开"):
            api.run()

        assert not any("--method" in cmd or "--input" in cmd for cmd in api.calls)

    def test_lightweight_legacy_published_with_asset_mismatch_stays_rejected(self, release_state):
        api = release_state
        api.publish_existing(lightweight_legacy=True)
        api.add_asset("SHA256SUMS", b"different content")

        with pytest.raises(release_handler.ReleaseError, match="禁止覆盖资产"):
            api.run()

        assert api.assets["SHA256SUMS"]["content"] == b"different content"
        assert not any("--method" in cmd or "--input" in cmd for cmd in api.calls)

    def test_lightweight_legacy_draft_cannot_be_completed(self, release_state):
        release_state.publish_existing(draft=True, lightweight_legacy=True)
        release_state.add_asset("SHA256SUMS", b"", "starter")
        original = dict(release_state.assets["SHA256SUMS"])

        with pytest.raises(release_handler.ReleaseError, match="首次构建归属"):
            release_state.run()

        assert release_state.release["draft"] and release_state.latest is None
        assert release_state.assets["SHA256SUMS"] == original
        assert not any("--method" in cmd or "--input" in cmd for cmd in release_state.calls)

    @pytest.mark.parametrize(
        ("name", "pending_state"), [("SHA256SUMS", "starter"), ("zip", "starter")], ids=["sums-starter", "zip-starter"],
    )
    def test_draft_upload_failure_with_pending_asset_is_recoverable(
        self, release_planning_api, monkeypatch, name, pending_state,
    ):
        api = release_planning_api
        if name == "zip":
            name = f"merged-rules-{api.tag[6:]}.zip"
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-123-attempt-1")
        api.add_build(123, 1, api.sha)
        saved = []
        stub_hash_state(monkeypatch, saved=saved)

        def upload_failure(cmd, strip=True):
            if "--input" in cmd and cmd[cmd.index("--input") + 1] == name:
                api.add_asset(name, b"", pending_state)
                return None
            return api(cmd, strip=strip)

        monkeypatch.setattr(release_handler, "run_gh", upload_failure)
        with pytest.raises(SystemExit) as exc:
            release_handler.main()
        assert exc.value.code == 1
        assert api.release["draft"] and api.latest is None and not saved
        pending_id = api.assets[name]["id"]
        other_assets = {key: value for key, value in api.assets.items() if key != name}

        monkeypatch.setattr(release_handler, "run_gh", api)
        monkeypatch.setenv("GITHUB_SHA", "b" * 40)
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-124-attempt-1")
        release_handler.main()

        assert not api.release["draft"] and api.latest == api.tag
        assert api.release["target_commitish"] == api.sha
        assert saved == ["new"]
        assert api.assets[name]["state"] == "uploaded" and api.assets[name]["id"] != pending_id
        assert all(api.assets[key] == value for key, value in other_assets.items())

    def test_open_draft_asset_is_preserved_and_rechecked(self, release_state):
        api = release_state
        api.publish_existing(draft=True)
        sums = api.assets["SHA256SUMS"]["content"]
        api.add_asset("SHA256SUMS", b"", "open")
        open_id = api.assets["SHA256SUMS"]["id"]

        with pytest.raises(release_handler.ReleaseError, match="仍处于上传中"):
            api.run()
        assert api.assets["SHA256SUMS"]["id"] == open_id
        assert api.release["draft"] and api.latest is None
        assert not any("DELETE" in cmd or "--input" in cmd for cmd in api.calls)

        api.assets["SHA256SUMS"].update(
            state="uploaded", size=len(sums), content=sums, digest="sha256:" + hashlib.sha256(sums).hexdigest(),
        )
        api.run()
        assert api.latest == api.tag and not api.release["draft"]
        assert api.assets["SHA256SUMS"]["id"] == open_id

    def test_foreign_draft_does_not_delete_incomplete_asset(self, release_state, monkeypatch):
        api = release_state
        api.publish_existing(draft=True, generation="github-123-attempt-1")
        api.add_build(123, 1, api.sha)
        api.release["author"] = {"login": "other-account"}
        api.add_asset("SHA256SUMS", b"", "starter")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("PUBLISH_BUILD_GENERATION", "github-123-attempt-1")

        with pytest.raises(release_handler.ReleaseError, match="草稿的首次构建归属与 tag 不一致"):
            api.run()

        assert api.assets["SHA256SUMS"]["state"] == "starter"
        assert not any("DELETE" in cmd or "--input" in cmd for cmd in api.calls)

    @pytest.mark.parametrize("stage", ["draft", "published"])
    def test_new_asset_after_verification_blocks_promotion(self, release_planning_api, monkeypatch, stage):
        api = release_planning_api
        api.publish_existing(draft=True)
        saved = []
        stub_hash_state(monkeypatch, saved=saved)
        zip_id = api.assets[f"merged-rules-{api.tag[6:]}.zip"]["id"]

        def download(cmd, max_bytes=None):
            data = api.bytes(cmd, max_bytes)
            if stage == "draft" and cmd[1].endswith(f"/assets/{zip_id}"):
                api.add_asset("unexpected.txt", b"other content")
            return data

        def mutate_after_publish(cmd, strip=True):
            result = api(cmd, strip=strip)
            if stage == "published" and "draft=false" in cmd:
                api.add_asset("unexpected.txt", b"other content")
            return result

        monkeypatch.setattr(release_handler, "run_gh_bytes", download)
        monkeypatch.setattr(release_handler, "run_gh", mutate_after_publish)
        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert api.release["draft"] is (stage == "draft")
        assert api.latest is None and not saved

    @pytest.mark.parametrize("draft", [True, False], ids=["newly-public", "already-public"])
    def test_asset_bytes_changed_after_verification_block_latest(
        self, release_planning_api, monkeypatch, caplog, draft,
    ):
        api = release_planning_api
        api.publish_existing(draft=draft)
        api.latest = None
        saved = []
        stub_hash_state(monkeypatch, saved=saved)
        zip_asset = api.assets[f"merged-rules-{api.tag[6:]}.zip"]

        def download(cmd, max_bytes=None):
            data = api.bytes(cmd, max_bytes)
            if not draft and cmd[1].endswith(f"/assets/{zip_asset['id']}"):
                zip_asset["content"] = b"!" + zip_asset["content"][1:]
            return data

        def mutate_on_publish(cmd, strip=True):
            result = api(cmd, strip=strip)
            if "draft=false" in cmd:
                zip_asset["content"] = b"!" + zip_asset["content"][1:]
            return result

        monkeypatch.setattr(release_handler, "run_gh_bytes", download)
        monkeypatch.setattr(release_handler, "run_gh", mutate_on_publish)
        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert api.release["draft"] is False
        assert "123" in caplog.text and "ZIP 原始字节" in caplog.text
        assert not any("make_latest=true" in cmd for cmd in api.calls)
        assert api.latest is None and not saved

    def test_latest_failure_after_new_release_does_not_save_hash(self, release_planning_api, monkeypatch):
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: None if "make_latest=true" in cmd else release_planning_api(cmd),
        )

        with pytest.raises(SystemExit):
            release_handler.main()
        assert saved == []

    def test_force_release_reuses_complete_release_without_duplicate(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        release_planning_api.latest = None
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        monkeypatch.setenv("FORCE_RELEASE", "true")

        release_handler.main()

        assert saved == ["same"]
        assert not any("--input" in call for call in release_planning_api.calls)
        assert not os.path.exists("SHA256SUMS")

    def test_empty_api_digests_do_not_clobber_on_consecutive_runs(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        for asset in release_planning_api.assets.values():
            asset["digest"] = None
        saved = []
        stub_hash_state(monkeypatch, current="new", last="old", saved=saved)

        release_handler.main()
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "new")
        release_handler.main()

        assert not any("--input" in call for call in release_planning_api.calls)
        assert release_planning_api.latest == release_planning_api.tag
        assert saved == ["new"]

    @pytest.mark.parametrize("corrupt", [False, True], ids=["matching-content", "wrong-content"])
    def test_empty_digests_at_process_boundary_keep_reuse_and_confirmation_safe(
        self, release_planning_api, monkeypatch, corrupt,
    ):
        release_planning_api.publish_existing()
        for asset in release_planning_api.assets.values():
            asset["digest"] = None
        if corrupt:
            zip_asset = release_planning_api.assets[f"merged-rules-{release_planning_api.tag[6:]}.zip"]
            zip_asset["content"] += b"remote-tampered"
        saved = []
        downloaded = []
        real_download = release_planning_api.bytes
        def download(command, max_bytes=None):
            result = real_download(command, max_bytes)
            if result is not None:
                downloaded.append(command)
            return result
        monkeypatch.setattr(release_handler, "run_gh_bytes", download)
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: saved[-1] if saved else "previous")
        monkeypatch.setattr(release_handler, "save_last_hash", saved.append)

        if corrupt:
            with pytest.raises(SystemExit):
                release_handler.main()
            assert saved == [] and not any("--input" in call for call in release_planning_api.calls)
        else:
            release_handler.main()
            release_handler.main()
            semantic_hash = release_handler.combined_products_hash(
                *release_handler.product_dirs(), format_tag=manifest.MANIFEST_MAGIC
            )[0]
            assert saved == [semantic_hash]
            assert not any("--input" in call for call in release_planning_api.calls)
        assert downloaded, "空 digest 仍须实际读取 ZIP/SHA 字节"

    def test_skips_when_hash_unchanged(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing()
        stub_hash_state(monkeypatch, current="same", last="same")
        before = len(release_planning_api.calls)

        release_handler.main()

        assert not any("--method" in call or "--input" in call for call in release_planning_api.calls[before:])

    def unchanged_listing(self, *extra):
        """内容不变轮次的 Release 列表：当前标签 isLatest，且可附加过期记录。"""
        now_id = current_expected_tag()[len("rules-") :]
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return release_listing(
            {"tagName": f"rules-{now_id}", "createdAt": old, "isLatest": True},
            *extra,
        )

    def test_unchanged_round_still_retries_cleanup(self, publish_workspace, release_planning_api, monkeypatch):
        """9b：内容不变且 latest 健康时也必须执行保留期清理，而不是提前返回。"""
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = self.unchanged_listing({"tagName": "rules-19990101-0000", "createdAt": old})
        stub = GhStub(responses={("release", "list"): listing})
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        release_planning_api.publish_existing()
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[:2] == ["release", "list"] else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert "release delete rules-19990101-0000 --yes" in flat, "内容不变轮次仍必须清理过期 Release"
        assert "git/refs/tags/rules-19990101-0000" in flat, "过期 Release 的 tag 也应一并清理"
        actions = stub.release_actions()
        assert flat.count("release delete ") == 1
        assert "create" not in actions and "upload" not in actions, "内容不变时不得重新打包上传"
        assert "edit" not in actions, "latest 已健康时不得重新标记 latest"
        assert saved == [], "内容不变时不得保存哈希（保持原语义，避免无谓重发）"

    def test_unchanged_round_cleanup_failure_is_not_fatal(self, publish_workspace, release_planning_api, monkeypatch):
        """9b：清理失败在内容不变轮次同样只告警，不得中断运行或触发补发。"""
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = self.unchanged_listing({"tagName": "rules-19990101-0000", "createdAt": old})
        attempts = []

        def fail_delete(cmd):
            if cmd[:2] == ["release", "delete"]:
                attempts.append(list(cmd))
                return True
            return False

        stub = GhStub(fail_when=fail_delete, responses={("release", "list"): listing})
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        release_planning_api.publish_existing()
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        assert len(attempts) == 1, "清理必须被尝试"
        actions = stub.release_actions()
        assert "create" not in actions and "upload" not in actions and "edit" not in actions
        assert "git/refs/tags/" not in " ".join(stub.commands()), "Release 删除失败时不得继续删 tag"
        assert saved == []

    def test_cleanup_failure_is_retried_by_next_unchanged_round(
        self, publish_workspace, release_planning_api, monkeypatch
    ):
        """9b：上一轮清理失败后，内容不变的下一轮必须重试清理并成功。"""
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = self.unchanged_listing({"tagName": "rules-19990101-0000", "createdAt": old})
        attempts = []

        def fail_first_delete(cmd):
            if cmd[:2] == ["release", "delete"]:
                attempts.append(list(cmd))
                return len(attempts) == 1
            return False

        stub = GhStub(fail_when=fail_first_delete, responses={("release", "list"): listing})
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)
        release_planning_api.publish_existing()
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()
        release_handler.main()

        assert len(attempts) == 2, "两轮都必须尝试清理"
        assert "git/refs/tags/rules-19990101-0000" in " ".join(
            [*stub.commands(), *release_planning_api.commands()]
        ), "第二轮重试应完成清理"
        actions = stub.release_actions()
        assert "create" not in actions and "upload" not in actions, "重试清理不得触发重新发布"
        assert saved == [], "内容不变的两轮都不得保存哈希"

    def test_failed_tag_deletion_retried_after_release_is_deleted(self, release_planning_api, monkeypatch):
        api = release_planning_api
        old_tag = api.tag
        api.add_tag(generation="github-123-attempt-1")
        api.tags[old_tag]["tagger"] = {"date": "2026-01-01T00:00:00Z"}
        now = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
        release_exists = True
        delete_enabled = False
        ref_deletes = []

        def platform(cmd, strip=True):
            nonlocal release_exists
            if cmd[:2] == ["release", "list"]:
                if not release_exists:
                    return "[]"
                return release_listing({"tagName": old_tag, "createdAt": "2026-01-01T00:00:00Z"})
            if cmd[:2] == ["release", "delete"]:
                release_exists = False
                return ""
            if cmd[:3] == ["api", "-X", "DELETE"]:
                assert cmd[3] == f"repos/{{owner}}/{{repo}}/git/refs/tags/{old_tag}"
                ref_deletes.append(cmd[3])
                return "" if delete_enabled else None
            return api(cmd, strip=strip)

        monkeypatch.setattr(release_handler, "run_gh", platform)
        assert release_handler.cleanup_old_releases("rules-111111111111", now) == 0
        assert not release_exists and ref_deletes
        ref_deletes.clear()
        delete_enabled = True

        assert release_handler.cleanup_old_releases("rules-111111111111", now) == 1
        assert len(ref_deletes) == 1

    def test_republishes_when_release_missing(self, release_planning_api, monkeypatch):
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)

        release_handler.main()

        assert saved == ["same"]
        assert release_planning_api.latest == release_planning_api.tag

    def test_draft_with_missing_asset_is_completed_not_clobbered(self, release_planning_api, monkeypatch):
        release_planning_api.publish_existing(draft=True)
        release_planning_api.assets.pop("SHA256SUMS")
        saved = []
        stub_hash_state(monkeypatch, current="same", last="same", saved=saved)

        release_handler.main()

        assert saved == ["same"] and release_planning_api.latest == release_planning_api.tag
        assert not any("--clobber" in call for call in release_planning_api.calls)

    def test_aborts_when_product_counts_differ(self, release_planning_api, monkeypatch):
        monkeypatch.setattr(release_handler, "combined_products_hash", lambda *a, **k: ("h", 3, 2))

        with pytest.raises(SystemExit):
            release_handler.main()
        assert release_planning_api.release is None

    def test_failed_publish_exits_and_removes_zip(self, release_planning_api, monkeypatch):
        saved = []
        stub_hash_state(monkeypatch, current="new", last=None, saved=saved)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, "bad"))
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)

        with pytest.raises(SystemExit):
            release_handler.main()
        assert saved == [] and release_planning_api.latest is None
        assert not list(release_planning_api.root.glob("merged-rules-*.zip"))
        assert not (release_planning_api.root / "SHA256SUMS").exists()

    def test_prunes_old_releases_and_keeps_current(self, publish_workspace, release_planning_api, monkeypatch):
        now_id = current_expected_tag()[len("rules-") :]
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": f"rules-{now_id}", "createdAt": old},
            {"tagName": "rules-19990101-0000", "createdAt": old},
            {"tagName": "v1.0.0", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert "release delete rules-19990101-0000 --yes" in flat
        assert f"release delete rules-{now_id}" not in flat, "本轮标签不得被删"
        assert "git/refs/tags/rules-19990101-0000" in flat, "删除 release 后应同时删 tag"
        assert "v1.0.0" not in flat, "非 rules- 前缀的 Release 一律不得触碰"

    def test_prune_never_touches_manual_releases(self, publish_workspace, release_planning_api, monkeypatch):
        old = (release_handler.beijing_now() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "v1.0.0", "createdAt": old},
            {"tagName": "v1.2.3-rc1", "createdAt": old},
            {"tagName": "manual-notes", "createdAt": old},
            {"tagName": "rules-manual", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert "delete" not in flat, f"人工 Release 不得被删: {flat}"
        assert "git/refs/tags" not in flat

    def test_prune_delete_failure_is_not_fatal(self, publish_workspace, release_planning_api, monkeypatch):
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
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert flat.count("release delete") == 2, "首个删除失败后仍应继续清理其余旧 Release"
        assert "git/refs/tags" not in flat, "Release 删除失败时不得继续删 tag"
        assert saved == ["new"], "清理失败不得跳过 state/release.sha256 的写入"
        zip_name = f"merged-rules-{current_expected_tag()[len('rules-') :]}.zip"
        assert not os.path.exists(zip_name), "清理失败后 main() 仍应执行到收尾"
        assert not os.path.exists("SHA256SUMS"), "收尾应同时清理临时 SHA256SUMS"

    def test_step_summary_written(self, publish_workspace, release_planning_api, monkeypatch):
        summary = publish_workspace / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        stub_hash_state(monkeypatch)

        release_handler.main()

        text = summary.read_text(encoding="utf-8")
        assert "发布报告" in text
        assert "文本规则" in text and "MRS 规则" in text

    def test_unparsable_release_listing_degrades_to_warning(self, publish_workspace, release_planning_api, monkeypatch):
        stub = GhStub(responses={("release", "list"): "not-json"})
        stub_hash_state(monkeypatch)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert "release delete" not in flat, "列表无法解析时不得误删任何 Release"

    def test_unparsable_entry_is_skipped_and_others_pruned(self, publish_workspace, release_planning_api, monkeypatch):
        old = (release_handler.beijing_now() - datetime.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = release_listing(
            {"tagName": "rules-broken", "createdAt": "not-a-date"},
            {"tagName": "rules-19990101-0000", "createdAt": old},
        )
        stub = GhStub(responses={("release", "list"): listing})
        stub_hash_state(monkeypatch)
        monkeypatch.setattr(
            release_handler, "run_gh",
            lambda cmd: stub(cmd) if cmd[0] == "release" else release_planning_api(cmd),
        )

        release_handler.main()

        flat = " ".join([*stub.commands(), *release_planning_api.commands()])
        assert "release delete rules-19990101-0000 --yes" in flat
        assert "rules-broken" not in flat, "无法解析的记录应跳过而非中断清理"


class TestOrphanTagCleanup:
    REFS_ENDPOINT = "repos/{owner}/{repo}/git/matching-refs/tags/rules-"
    # 固定 SHA，避免测试里散落字面量
    COMMIT_SHA = "a" * 40
    TAG_OBJECT_SHA = "b" * 40
    TAG_TARGET_SHA = "c" * 40

    TAG = "rules-aaaaaaaaaaaa"
    REF_ENDPOINT = f"{REFS_ENDPOINT}{TAG[6:]}"
    OBJECT_ENDPOINT = f"repos/{{owner}}/{{repo}}/git/tags/{TAG_OBJECT_SHA}"
    COMMIT_ENDPOINT = f"repos/{{owner}}/{{repo}}/commits/{COMMIT_SHA}"

    def hash_tag_stub(self, ref_record, extra=None):
        responses = {
            ("api", self.REFS_ENDPOINT): f"refs/tags/{self.TAG}",
            ("api", self.REF_ENDPOINT): ref_record,
            **(extra or {}),
        }
        return GhStub(responses=responses)

    def annotated_response(self, *, object_type="commit", date=None):
        record = {
            "sha": self.TAG_TARGET_SHA,
            "digest": "a" * 64,
            "generation": "github-123-attempt-1",
            "path": ".github/workflows/pipeline.yml",
            "repo": "rksk102/ruleset-foundry",
            "ref": "refs/heads/main",
        }
        return json.dumps({
            "sha": self.TAG_OBJECT_SHA,
            "tag": self.TAG,
            "object": {"type": object_type, "sha": self.TAG_TARGET_SHA},
            "message": release_handler.PROVENANCE_PREFIX + json.dumps(record),
            "tagger": {"date": date} if date is not None else {},
        })

    def read_endpoints(self, stub):
        return [cmd[1] for cmd in stub.calls if cmd[:1] == ["api"] and cmd[1] != "-X"]

    def delete_calls(self, stub):
        return [call for call in stub.calls if call[:3] == ["api", "-X", "DELETE"]]

    def test_deletes_only_old_legacy_automatic_tags(self, monkeypatch, caplog):
        refs = "\n".join(
            f"refs/tags/{tag}"
            for tag in (
                "rules-20200101-0000",
                "rules-29990101-0000",
                "rules-cccccccccccc",
                "rules-dddddddddddd",
                "rules-manual",
                "rules-20200230-0000",
                "v1.0.0",
            )
        )
        stub = GhStub(responses={("api", self.REFS_ENDPOINT): refs})
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        cleaned = release_handler.cleanup_orphan_rules_tags(
            {"rules-cccccccccccc"}, "rules-dddddddddddd", cutoff
        )

        assert cleaned == 1
        assert self.delete_calls(stub) == [
            ["api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/tags/rules-20200101-0000"]
        ]
        flat = " ".join(stub.commands())
        assert "commits/" not in flat, "旧格式 tag 不应再查询 commit 日期"
        assert "git/ref/tags/" not in flat, "旧格式 tag 不应查询 ref 对象"
        assert "20200230" in caplog.text, "非法旧格式时间必须保留并告警"
        assert "rules-cccccccccccc" not in flat, "有 Release 的 tag 不参与对账"
        assert "rules-dddddddddddd" not in flat, "本轮 tag 不参与对账"
        assert "rules-manual" not in flat and "v1.0.0" not in flat

    @pytest.mark.parametrize("commit_age", ["2020-01-01T00:00:00Z", "2026-03-01T00:00:00Z"])
    def test_lightweight_hash_tag_is_kept_regardless_of_commit_age(self, monkeypatch, commit_age):
        stub = self.hash_tag_stub(f"commit\t{self.COMMIT_SHA}", {("api", self.COMMIT_ENDPOINT): commit_age})
        monkeypatch.setattr(release_handler, "run_gh", stub)

        assert release_handler.cleanup_orphan_rules_tags(
            set(), "rules-dddddddddddd", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        ) == 0
        assert self.read_endpoints(stub) == [self.REFS_ENDPOINT, self.REF_ENDPOINT]
        assert self.delete_calls(stub) == []

    def test_annotated_orphan_tag_without_recorded_creation_time_is_kept(self, monkeypatch):
        stub = self.hash_tag_stub(
            f"tag\t{self.TAG_OBJECT_SHA}",
            {("api", self.OBJECT_ENDPOINT): self.annotated_response()},
        )
        monkeypatch.setattr(release_handler, "run_gh", stub)

        assert release_handler.cleanup_orphan_rules_tags(
            set(), "rules-dddddddddddd", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        ) == 0
        assert self.read_endpoints(stub) == [self.REFS_ENDPOINT, self.REF_ENDPOINT, self.OBJECT_ENDPOINT]
        assert self.delete_calls(stub) == []

    def test_annotated_tag_pointing_at_non_commit_is_kept(self, monkeypatch, caplog):
        stub = self.hash_tag_stub(
            f"tag\t{self.TAG_OBJECT_SHA}",
            {("api", self.OBJECT_ENDPOINT): self.annotated_response(object_type="blob", date="2020-01-01T00:00:00Z")},
        )
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == 0
        assert self.delete_calls(stub) == [], "目标不是提交时必须 fail-safe 保留"
        assert "1 个无法确认创建时间的孤立 tag" in caplog.text
        assert self.TAG in caplog.text, "保留原因必须可追查"

    def test_unresolvable_tag_object_is_kept_with_warning(self, monkeypatch, caplog):
        stub = self.hash_tag_stub(f"tag\t{self.TAG_OBJECT_SHA}")
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == 0
        assert self.delete_calls(stub) == []
        assert self.read_endpoints(stub) == [self.REFS_ENDPOINT, self.REF_ENDPOINT, self.OBJECT_ENDPOINT]
        assert "1 个无法确认创建时间的孤立 tag" in caplog.text

    def test_unparseable_tag_ref_is_kept(self, monkeypatch, caplog):
        stub = self.hash_tag_stub("garbage")
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == 0
        assert self.read_endpoints(stub) == [self.REFS_ENDPOINT, self.REF_ENDPOINT]
        assert self.delete_calls(stub) == []
        assert "1 个无法确认创建时间的孤立 tag" in caplog.text

    @pytest.mark.parametrize(
        ("date", "expired"),
        [("not-a-date", False), ("2026-03-01T00:00:00Z", False), ("2020-01-01T00:00:00Z", True)],
        ids=["unparseable", "recent", "expired"],
    )
    def test_annotated_orphan_cleanup_uses_tag_date(self, monkeypatch, date, expired):
        stub = self.hash_tag_stub(
            f"tag\t{self.TAG_OBJECT_SHA}",
            {("api", self.OBJECT_ENDPOINT): self.annotated_response(date=date)},
        )
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert release_handler.cleanup_orphan_rules_tags(set(), "rules-dddddddddddd", cutoff) == int(expired)
        assert self.read_endpoints(stub) == [self.REFS_ENDPOINT, self.REF_ENDPOINT, self.OBJECT_ENDPOINT]
        assert self.delete_calls(stub) == (
            [["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{self.TAG}"]] if expired else []
        )

    def test_hash_tag_with_release_or_current_is_not_queried(self, monkeypatch):
        refs = "\n".join(
            ["refs/tags/rules-aaaaaaaaaaaa", "refs/tags/rules-bbbbbbbbbbbb", "refs/tags/rules-cccccccccccc"]
        )
        stub = GhStub(responses={("api", self.REFS_ENDPOINT): refs})
        monkeypatch.setattr(release_handler, "run_gh", stub)
        cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        cleaned = release_handler.cleanup_orphan_rules_tags(
            {"rules-aaaaaaaaaaaa"}, "rules-bbbbbbbbbbbb", cutoff
        )

        assert cleaned == 0
        protected_calls = [
            cmd for cmd in stub.calls[1:]
            if "rules-aaaaaaaaaaaa" in cmd[1] or "rules-bbbbbbbbbbbb" in cmd[1]
        ]
        assert protected_calls == [], "有 Release 和本轮 tag 不得查询或删除对象"

    def test_untrusted_annotated_orphan_is_kept(self, release_planning_api, monkeypatch, caplog):
        api = release_planning_api
        api.add_tag(generation="github-123-attempt-1")
        api.tags[api.tag]["tagger"] = {"date": "2020-01-01T00:00:00Z"}
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/repository")

        assert release_handler.cleanup_orphan_rules_tags(
            set(), "rules-dddddddddddd", datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        ) == 0

        assert "无法确认创建时间" in caplog.text
        assert not any("DELETE" in cmd for cmd in api.calls)
        assert not any("/actions/" in cmd[1] for cmd in api.calls if len(cmd) > 1)

    def test_orphan_delete_is_retried_by_next_reconciliation(self, monkeypatch):
        delete_attempts = []

        def fail_first_delete(cmd):
            if cmd[:3] == ["api", "-X", "DELETE"]:
                delete_attempts.append(cmd)
                return len(delete_attempts) == 1
            return False

        stub = GhStub(
            fail_when=fail_first_delete,
            responses={("api", self.REFS_ENDPOINT): "refs/tags/rules-20200101-0000"},
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

    def test_unicode_decode_error_does_not_escape(self, monkeypatch, caplog):
        """gh 输出非 UTF-8 时 text=True 会抛 UnicodeDecodeError，必须降级为失败返回。"""

        def boom(*a, **k):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(release_handler.subprocess, "run", boom)

        assert release_handler.run_gh(["release", "list"]) is None
        assert "解码失败" in caplog.text
        assert "release list" in caplog.text

    def test_bytes_download_enforces_asset_size_cap(self, monkeypatch):
        class Pipe:
            def __init__(self):
                self.chunks = [b"x" * 6, b"x" * 6, b""]

            def read(self, _size=-1):
                return self.chunks.pop(0)

            def close(self):
                pass

        class Process:
            def __init__(self):
                self.stdout = Pipe()
                self.killed = False
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.killed = True

            def poll(self):
                return self.returncode

        process = Process()
        monkeypatch.setattr(release_handler.subprocess, "Popen", lambda *a, **k: process)
        assert release_handler.run_gh_bytes(["release", "download"], max_bytes=10) is None
        assert process.killed is True

    def test_bytes_download_returns_streamed_content(self, monkeypatch):
        class Pipe:
            def __init__(self):
                self.chunks = [b"hello", b" world", b""]

            def read(self, _size=-1):
                return self.chunks.pop(0)

            def close(self):
                pass

        class Process:
            returncode = 0

            def __init__(self):
                self.stdout = Pipe()

            def wait(self, timeout=None):
                return 0

            def kill(self):
                raise AssertionError("不应终止成功进程")

            def poll(self):
                return 0

        monkeypatch.setattr(release_handler.subprocess, "Popen", lambda *a, **k: Process())
        assert release_handler.run_gh_bytes(["release", "download"], max_bytes=100) == b"hello world"

    def test_bytes_download_timeout_kills_process(self, monkeypatch):
        class Pipe:
            def read(self, _size=-1):
                return b""

            def close(self):
                pass

        class Process:
            def __init__(self):
                self.stdout = Pipe()
                self.killed = False
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("gh", timeout)
                return -9

            def kill(self):
                self.killed = True

            def poll(self):
                return -9 if self.killed else None

        process = Process()
        monkeypatch.setattr(release_handler.subprocess, "Popen", lambda *a, **k: process)
        assert release_handler.run_gh_bytes(["release", "download"], max_bytes=100) is None
        assert process.killed is True


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
    (mrs_dir / "x.mrs").write_bytes(domain_mrs_bytes(["x"]))
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

    def test_mismatched_txt_and_mrs_content_is_fatal(self, verify_workspace):
        (verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt").write_text(
            "one.example\ntwo.example\n", encoding="utf-8"
        )
        (verify_workspace / "merged-rules-mrs" / "block" / "domain" / "A" / "x.mrs").write_bytes(
            domain_mrs_bytes(["one.example"])
        )
        manifest.save_manifest(
            verify_workspace / "merged-rules" / "products.manifest",
            {"block/domain/A/x.txt": hashlib.sha256(b"one.example\ntwo.example\n").hexdigest()},
            generation="verify-gen",
        )
        with pytest.raises(manifest.ManifestError, match="内容"):
            release_handler.verify_products(require_baseline=True)

    def test_tampered_mrs_count_is_fatal(self, verify_workspace):
        (verify_workspace / "merged-rules-mrs" / "block" / "domain" / "A" / "x.mrs").write_bytes(
            domain_mrs_bytes(["x"], count=999999)
        )
        with pytest.raises(manifest.ManifestError, match="内容"):
            release_handler.verify_products(require_baseline=True)

    def test_domain_plus_expansion_and_duplicate_count_match(self, verify_workspace):
        text = b"+.example.com\nwww.example.com\nwww.example.com\n"
        (verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt").write_bytes(text)
        (verify_workspace / "merged-rules-mrs" / "block" / "domain" / "A" / "x.mrs").write_bytes(
            domain_mrs_bytes(["+.example.com", "www.example.com", "www.example.com"])
        )
        manifest.save_manifest(
            verify_workspace / "merged-rules" / "products.manifest",
            {"block/domain/A/x.txt": hashlib.sha256(text).hexdigest()},
            generation="verify-gen",
        )
        assert release_handler.verify_products(require_baseline=True) == 1

    def test_ip_network_canonicalization_matches_mrs(self, verify_workspace):
        (verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt").unlink()
        target = verify_workspace / "merged-rules" / "block" / "ipcidr" / "A"
        target.mkdir(parents=True)
        (target / "x.txt").write_bytes(b"1.0.0.1/24\n1.0.1.0/24\n")
        (verify_workspace / "merged-rules-mrs" / "block" / "domain" / "A" / "x.mrs").unlink()
        target_mrs = verify_workspace / "merged-rules-mrs" / "block" / "ipcidr" / "A"
        target_mrs.mkdir(parents=True)
        (target_mrs / "x.mrs").write_bytes(ipcidr_mrs_bytes(["1.0.0.0/23"], count=2))
        for name in ("rulesets", "merged-rules"):
            manifest.save_manifest(
                verify_workspace / name / "products.manifest",
                {"block/ipcidr/A/x.txt": hashlib.sha256(b"1.0.0.1/24\n1.0.1.0/24\n").hexdigest()},
                generation="verify-gen",
            )
        assert release_handler.verify_products(require_baseline=True) == 1

    def test_mixed_ip_families_match_mrs_order(self, verify_workspace):
        (verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt").unlink()
        text = b"::1/128\n1.0.0.0/24\n"
        target = verify_workspace / "merged-rules" / "block" / "ipcidr" / "A"
        target.mkdir(parents=True)
        (target / "x.txt").write_bytes(text)
        (verify_workspace / "merged-rules-mrs" / "block" / "domain" / "A" / "x.mrs").unlink()
        target_mrs = verify_workspace / "merged-rules-mrs" / "block" / "ipcidr" / "A"
        target_mrs.mkdir(parents=True)
        (target_mrs / "x.mrs").write_bytes(ipcidr_mrs_bytes(["1.0.0.0/24", "::1/128"]))
        for name in ("rulesets", "merged-rules"):
            manifest.save_manifest(
                verify_workspace / name / "products.manifest",
                {"block/ipcidr/A/x.txt": hashlib.sha256(text).hexdigest()},
                generation="verify-gen",
            )
        assert release_handler.verify_products(require_baseline=True) == 1

    def test_invalid_utf8_txt_is_fatal_even_with_valid_manifest(self, verify_workspace):
        text = b"# generated\n\xff\n"
        target = verify_workspace / "merged-rules" / "block" / "domain" / "A" / "x.txt"
        target.write_bytes(text)
        manifest.save_manifest(
            verify_workspace / "merged-rules" / "products.manifest",
            {"block/domain/A/x.txt": hashlib.sha256(text).hexdigest()},
            generation="verify-gen",
        )
        with pytest.raises(manifest.ManifestError, match="UTF-8"):
            release_handler.verify_products(require_baseline=True)

    def test_nonempty_garbage_mrs_exits_nonzero(self, verify_workspace):
        (verify_workspace / "merged-rules-mrs/block/domain/A/x.mrs").write_bytes(b"not an mrs\n")
        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only()
        assert exc.value.code == 1

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

    def test_require_baseline_rejects_missing_rulesets_dir(self, verify_workspace):
        """--require-baseline 时基线目录缺失必须硬失败，不得降级为告警。"""
        shutil.rmtree(verify_workspace / "rulesets")

        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only(require_baseline=True)

        assert exc.value.code == 1

    def test_require_baseline_rejects_missing_manifest(self, verify_workspace):
        os.unlink("rulesets/products.manifest")

        with pytest.raises(SystemExit) as exc:
            release_handler.verify_only(require_baseline=True)

        assert exc.value.code == 1

    def test_require_baseline_passes_with_valid_baseline(self, verify_workspace):
        stub = GhStub()
        run_with_stub(stub, lambda: release_handler.verify_only(require_baseline=True))
        assert stub.calls == [], "预检不应调用 gh"

    def test_default_mode_still_degrades_without_baseline_dir(self, verify_workspace):
        """默认调用保持原语义：无基线目录、但有成对产物时只告警。"""
        shutil.rmtree(verify_workspace / "rulesets")

        assert release_handler.verify_products() is None
        assert release_handler.verify_products(require_baseline=False) is None

    def test_explicit_require_baseline_raises_without_rulesets_dir(self, verify_workspace):
        shutil.rmtree(verify_workspace / "rulesets")

        with pytest.raises(manifest.ManifestError, match=release_handler.BASELINE_MISSING):
            release_handler.verify_products(require_baseline=True)

    def test_main_cli_require_baseline_missing_dir_exits_nonzero(self, verify_workspace, monkeypatch):
        shutil.rmtree(verify_workspace / "rulesets")
        monkeypatch.setattr(
            sys, "argv", ["release_handler.py", "--verify-only", "--require-baseline"]
        )

        with pytest.raises(SystemExit) as exc:
            run_with_stub(GhStub(), release_handler.main)

        assert exc.value.code == 1

    def test_main_dispatches_verify_only_without_publishing(self, verify_workspace, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "argv", ["release_handler.py", "--verify-only"])
        monkeypatch.setattr(
            release_handler, "verify_only", lambda **kw: calls.append(kw.get("require_baseline"))
        )
        run_with_stub(GhStub(), release_handler.main)
        assert calls == [False], "main() 应分发到 verify_only 并提前返回，且默认不要求基线"

    def test_main_passes_require_baseline_flag(self, verify_workspace, monkeypatch):
        calls = []
        monkeypatch.setattr(
            sys, "argv", ["release_handler.py", "--verify-only", "--require-baseline"]
        )
        monkeypatch.setattr(
            release_handler, "verify_only", lambda **kw: calls.append(kw.get("require_baseline"))
        )
        run_with_stub(GhStub(), release_handler.main)
        assert calls == [True], "main() 必须把 --require-baseline 传入 verify_only"

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
        monkeypatch.setattr(release_handler, "process_release", lambda *a: calls.append("release"))
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (True, "ok"))
        def prepare(_tag, _digest):
            if names is None:
                raise release_handler.ReleaseError("无法确认资产")
            if not set(self.FULL) <= names:
                raise release_handler.ReleaseError(f"缺少资产 {sorted(set(self.FULL) - names)}")
            return ({"id": 123, "draft": False}, {}, "d" * 40, assets(123))
        monkeypatch.setattr(release_handler, "prepare_release_plan", prepare)
        def assets(_release_id):
            if names is None:
                raise release_handler.ReleaseError("无法确认资产")
            return {name: {"id": 1, "name": name, "state": "uploaded", "digest": None} for name in names}
        monkeypatch.setattr(release_handler, "release_asset_records", assets)
        monkeypatch.setattr(
            release_handler, "latest_rules_release_tag",
            lambda: "rules-" + release_handler.release_content_digest()[:12],
        )
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

        assert calls == ["enforce", "release"]

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

    def retry_draft_confirmation(self, monkeypatch, failure):
        tag = "rules-test"
        release = {"id": 123, "tag_name": tag, "draft": True}
        assets = {name: {"name": name, "id": index, "state": "uploaded", "digest": None}
                  for index, name in enumerate(release_handler.expected_assets("test"), 1)}
        attempts = []
        published = []

        def records(_release_id):
            attempts.append("assets")
            if failure == "query" and len(attempts) == 1:
                raise release_handler.ReleaseError("暂时无法读取")
            return {} if failure == "empty" and len(attempts) == 1 else assets

        def verify(*_args, **_kwargs):
            attempts.append("content")
            valid = bool(_kwargs["records"]) and not (
                failure == "content" and attempts.count("content") == 1
            )
            return valid, "内容一致" if valid else "不一致"

        def publish(cmd, **_kwargs):
            published.append(cmd)
            return json.dumps({"id": 123, "tag_name": tag, "draft": False})

        monkeypatch.setattr(release_handler, "release_asset_records", records)
        monkeypatch.setattr(release_handler, "verify_release_reusable", verify)
        monkeypatch.setattr(release_handler, "find_release", lambda _tag: release)
        monkeypatch.setattr(release_handler, "_release_tag_ref", lambda _tag: "a" * 40)
        monkeypatch.setattr(release_handler, "run_gh", publish)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        release_handler._complete_draft(tag, release, "a" * 40, {}, [])
        assert published and published[0][0] == "api"
        return attempts

    def test_asset_query_is_retried_before_succeeding(self, monkeypatch):
        assert self.retry_draft_confirmation(monkeypatch, "query").count("assets") == 3

    def test_zero_asset_count_is_retried_before_succeeding(self, monkeypatch):
        assert self.retry_draft_confirmation(monkeypatch, "empty").count("assets") == 3

    def test_content_mismatch_after_upload_is_a_failure(self, monkeypatch):
        saved = []
        assets = {name: {"name": name, "id": index, "state": "uploaded", "digest": None}
                  for index, name in enumerate(release_handler.expected_assets("test"), 1)}
        monkeypatch.setattr(release_handler, "release_asset_records", lambda _id: assets)
        monkeypatch.setattr(release_handler, "verify_release_reusable", lambda *a, **k: (False, "bad zip"))
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)

        with pytest.raises(release_handler.ReleaseError, match="bad zip"):
            release_handler._complete_draft("rules-test", {"id": 123}, "a" * 40, assets, [])

        assert saved == []

    def test_content_confirmation_is_retried_before_succeeding(self, monkeypatch):
        assert self.retry_draft_confirmation(monkeypatch, "content").count("content") == 2

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
