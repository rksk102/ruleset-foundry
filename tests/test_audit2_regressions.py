"""第二轮审查 P1 修复的回归测试：规则语义、清单清理、合并守卫、发布链路。"""
import os
import subprocess
import sys
from pathlib import Path

import convert_mrs
import main
import merger
import processor
import pytest
import release_handler

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")


class TestConfigDirsWiring:
    def test_product_dirs_and_readme_dirs_follow_config(self, work_dir):
        (work_dir / "config.yaml").write_text(
            'paths:\n  merged_output_dir: "out-txt"\n  mrs_output_dir: "out-mrs"\n',
            encoding="utf-8")
        child = (
            "import gen_readme, release_handler\n"
            "print(release_handler.product_dirs())\n"
            "print(sorted(release_handler.TARGET_CONFIG))\n"
            "print(gen_readme.DIR_RULES.endswith('out-txt'),"
            " gen_readme.DIR_MRS.endswith('out-mrs'))\n"
        )
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, "-c", child], cwd=str(work_dir),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=60)
        out = (proc.stdout or "").strip()
        assert "('out-txt', 'out-mrs')" in out, out or proc.stderr
        assert "['out-mrs', 'out-txt']" in out, out
        assert "True True" in out, out


class TestRuleLineSemantics:
    def test_wildcard_star_must_occupy_a_whole_label(self):
        domains, stats = processor.process_domain_detailed(
            ["*.example.com", "*.*.example.com", "*.foo*.com",
             "*.example.com/path", "*.a b.com", "*."], "exact")
        assert domains == ["*.*.example.com", "*.example.com"]
        assert stats["wildcard"] == 2
        assert stats["unrecognized"] == 4

    def test_ip_literals_are_dropped_instead_of_truncated(self):
        domains, stats = processor.process_domain_detailed(
            ["2001:db8::1", "1.2.3.4", "2001:db8::/32", "[2001:db8::1]",
             "example.com:8080", "host.example"], "exact")
        assert domains == ["example.com", "host.example"]
        assert stats["unrecognized"] == 4


class TestCleanOrphans:
    def test_directory_named_txt_does_not_crash_cleanup(self, work_dir, monkeypatch):
        root = work_dir / "rulesets"
        product = root / "block" / "domain" / "Owner" / "a.txt"
        product.parent.mkdir(parents=True)
        product.write_text("a.example\n", encoding="utf-8")
        (root / "foo.txt").mkdir()
        (root / "foo.txt" / "inner.txt").write_text("x.example\n", encoding="utf-8")
        monkeypatch.setattr(main, "RULESETS_DIR", root)

        main.clean_orphans([product])

        assert product.exists()
        assert not (root / "foo.txt").exists()


class TestMergerGuards:
    def test_merge_task_and_passthrough_may_not_share_a_path(self, work_dir, monkeypatch):
        src = work_dir / "rulesets"
        (src / "block" / "domain" / "Owner").mkdir(parents=True)
        (src / "block" / "domain" / "Owner" / "ads.txt").write_text(
            "a.example\n", encoding="utf-8")
        tasks = [{
            "strategy": "block", "type": "domain", "owner": "Owner",
            "filename": "ads.txt", "inputs": ["block/domain/Owner/ads.txt"],
        }]
        monkeypatch.setattr(merger, "SOURCE_DIR", str(src))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))
        monkeypatch.setattr(merger, "load_config", lambda: {"merges": tasks})

        with pytest.raises(SystemExit) as exc:
            merger.main()

        assert exc.value.code == 1

    def test_zero_rule_merge_result_is_refused(self, work_dir, monkeypatch):
        src = work_dir / "rulesets"
        (src / "block" / "domain" / "Owner").mkdir(parents=True)
        (src / "block" / "domain" / "Owner" / "ads.txt").write_text(
            "# 只有注释\n", encoding="utf-8")
        monkeypatch.setattr(merger, "SOURCE_DIR", str(src))
        monkeypatch.setattr(merger, "OUTPUT_DIR", str(work_dir / "merged"))

        with pytest.raises(ValueError, match="合并结果为空"):
            merger.process_task_logic(
                "block", "domain", "Owner", "out.txt",
                ["block/domain/Owner/ads.txt"], "测试",)


class TestConvertMrs:
    def test_set_config_field_accepts_unquoted_values(self):
        text = 'mihomo:\n  pinned_version: v1.0\n  kernel_sha256: \'aa\'\n'
        assert 'pinned_version: "v2.0"' in convert_mrs._set_config_field(
            text, "pinned_version", "v2.0")
        assert 'kernel_sha256: "bb"' in convert_mrs._set_config_field(
            text, "kernel_sha256", "bb")

    def test_non_linux_fails_with_a_clear_message(self, monkeypatch, caplog):
        monkeypatch.setattr(convert_mrs.sys, "platform", "win32")
        with pytest.raises(SystemExit) as exc:
            convert_mrs.ensure_kernel_platform()
        assert exc.value.code == 1
        assert "Linux" in caplog.text


class TestReleaseHandler:
    def stub_release_flow(self, monkeypatch, calls, asset_count):
        monkeypatch.setattr(release_handler, "combined_products_hash",
                            lambda *a, **k: ("new-hash", 1, 1))
        monkeypatch.setattr(release_handler, "product_dirs", lambda: ("txt", "mrs"))
        monkeypatch.setattr(release_handler, "enforce_products",
                            lambda *a: calls.append("enforce"))
        monkeypatch.setattr(release_handler, "zip_target_files", lambda d: ("z.zip", {}))
        monkeypatch.setattr(release_handler, "generate_release_notes", lambda *a: "notes")
        monkeypatch.setattr(release_handler, "publish_release", lambda *a: "ok")
        monkeypatch.setattr(release_handler, "release_asset_count", lambda tag: asset_count)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda *a: None)
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "")
        monkeypatch.setattr(release_handler, "load_last_hash", lambda: "hash")
        monkeypatch.setattr(release_handler.os.path, "exists", lambda p: False)

    def test_verification_runs_even_with_change_detection_off(self, monkeypatch):
        calls = []
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", False)
        self.stub_release_flow(monkeypatch, calls, asset_count=1)

        release_handler.main()

        assert calls == ["enforce"], "关闭变更检测也必须跑产物校验"

    def test_publish_without_assets_is_a_failure(self, monkeypatch, caplog):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        self.stub_release_flow(monkeypatch, [], asset_count=0)

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert "没有任何资产" in caplog.text

    def test_unconfirmed_asset_count_is_a_failure(self, monkeypatch, caplog):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        saved = []
        self.stub_release_flow(monkeypatch, [], asset_count=None)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        with pytest.raises(SystemExit) as exc:
            release_handler.main()

        assert exc.value.code == 1
        assert "无法确认" in caplog.text
        assert saved == [], "无法确认资产数时不得保存哈希"

    def test_asset_query_is_retried_before_succeeding(self, monkeypatch):
        monkeypatch.setattr(release_handler, "CHANGE_DETECTION", True)
        monkeypatch.setattr(release_handler, "ASSET_CONFIRM_DELAY", 0)
        attempts = []
        saved = []
        self.stub_release_flow(monkeypatch, [], asset_count=None)

        def flaky(tag):
            attempts.append(tag)
            return None if len(attempts) == 1 else 2

        monkeypatch.setattr(release_handler, "release_asset_count", flaky)
        monkeypatch.setattr(release_handler, "save_last_hash", lambda h: saved.append(h))

        release_handler.main()

        assert len(attempts) == 2, "首次查询失败应重试"
        assert saved == ["new-hash"]

    def test_release_asset_count_parses_gh_output(self, monkeypatch):
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "3")
        assert release_handler.release_asset_count("rules-2026-10-06") == 3
        monkeypatch.setattr(release_handler, "run_gh", lambda cmd: "")
        assert release_handler.release_asset_count("rules-2026-10-06") is None
