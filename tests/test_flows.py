"""进程内驱动 main() 与 convert_mrs.main()：补上编排路径的覆盖（T-1）。"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import convert_mrs
import main
import manifest
import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")

SOURCES = """[policy:reject]
[type:domain]
[domain-kind:exact]
https://raw.githubusercontent.com/o/keep/keep.txt
https://raw.githubusercontent.com/o/drop/drop.txt
"""


class TestConfigDirsWiring:
    def test_product_dirs_and_readme_dirs_follow_config(self, work_dir):
        (work_dir / "config.yaml").write_text(
            'paths:\n  merged_output_dir: "out-txt"\n  mrs_output_dir: "out-mrs"\n', encoding="utf-8"
        )
        child = (
            "import gen_readme, release_handler\n"
            "print(release_handler.product_dirs())\n"
            "print(sorted(release_handler.TARGET_CONFIG))\n"
            "print(gen_readme.DIR_RULES.endswith('out-txt'),"
            " gen_readme.DIR_MRS.endswith('out-mrs'))\n"
        )
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        assert "('out-txt', 'out-mrs')" in out, out or proc.stderr
        assert "['out-mrs', 'out-txt']" in out, out
        assert "True True" in out, out


PAYLOADS = {
    "keep.txt": b"a.example\nb.example\n",
    "drop.txt": b"c.example\n",
}


class TestMainFlow:
    def setup_run(self, monkeypatch, work_dir, sources=SOURCES, payloads=None, allow_partial=False):
        payloads = PAYLOADS if payloads is None else payloads
        rulesets = work_dir / "rulesets"
        sources_file = work_dir / "sources.urls"
        sources_file.write_text(sources, encoding="utf-8")
        monkeypatch.setattr(main, "SOURCES_FILE", str(sources_file))
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        monkeypatch.setattr(main, "MANIFEST_FILE", rulesets / "products.manifest")
        monkeypatch.setattr(main, "ALLOW_PARTIAL", allow_partial)
        monkeypatch.setattr(main, "MIN_SUCCESS_RATIO", 0.0)
        monkeypatch.setattr(main, "UNRECOGNIZED_WARN_RATIO", 0.10)
        monkeypatch.setattr(main, "UNRECOGNIZED_MIN_LINES", 20)

        async def fake_download_all(tasks):
            results = []
            for task in tasks:
                name = task["url"].rsplit("/", 1)[-1]
                body = payloads.get(name)
                results.append((task, body, None if body is not None else "下载失败"))
            return results

        monkeypatch.setattr(main, "download_all", fake_download_all)
        return rulesets

    def manifest_entries(self, rulesets):
        loaded = manifest.load_manifest(rulesets / "products.manifest")
        return sorted(loaded.entries) if loaded else None

    def disk_entries(self, rulesets):
        return sorted(str(p.relative_to(rulesets)).replace("\\", "/") for p in rulesets.rglob("*.txt"))

    def test_healthy_run_writes_a_manifest_matching_disk(self, monkeypatch, work_dir):
        rulesets = self.setup_run(monkeypatch, work_dir)

        main.main()

        assert self.manifest_entries(rulesets) == self.disk_entries(rulesets)
        assert self.disk_entries(rulesets) == ["block/domain/o/drop.txt", "block/domain/o/keep.txt"]

    def test_stale_orphan_is_cleaned_and_never_recorded(self, monkeypatch, work_dir):
        rulesets = self.setup_run(monkeypatch, work_dir)
        main.main()

        sources = SOURCES.replace("https://raw.githubusercontent.com/o/drop/drop.txt\n", "")
        self.setup_run(monkeypatch, work_dir, sources=sources)
        main.main()

        assert self.disk_entries(rulesets) == ["block/domain/o/keep.txt"]
        assert self.manifest_entries(rulesets) == ["block/domain/o/keep.txt"], "被清理掉的孤儿不得留在产物清单里"

    def test_degraded_run_keeps_previous_products_and_writes_no_manifest(self, monkeypatch, work_dir):
        rulesets = self.setup_run(monkeypatch, work_dir)
        main.main()
        before = self.manifest_entries(rulesets)

        payloads = dict(PAYLOADS, **{"drop.txt": None})
        self.setup_run(monkeypatch, work_dir, payloads=payloads)
        with pytest.raises(SystemExit) as exc:
            main.main()

        assert exc.value.code == 1
        assert [p for p in self.disk_entries(rulesets) if not p.endswith("sync-summary.txt")] == [
            "block/domain/o/drop.txt",
            "block/domain/o/keep.txt",
        ]
        assert self.manifest_entries(rulesets) == before

    def test_min_success_ratio_gate_blocks_publish(self, monkeypatch, work_dir):
        rulesets = self.setup_run(monkeypatch, work_dir)
        main.main()
        before = self.manifest_entries(rulesets)

        payloads = dict(PAYLOADS, **{"drop.txt": None})
        self.setup_run(monkeypatch, work_dir, payloads=payloads, allow_partial=True)
        monkeypatch.setattr(main, "MIN_SUCCESS_RATIO", 0.9)
        with pytest.raises(SystemExit) as exc:
            main.main()

        assert exc.value.code == 1
        assert self.manifest_entries(rulesets) == before


class TestConvertMrsFlow:
    def test_print_kernel_hash_prints_the_file_digest(self, monkeypatch, work_dir, capsys):
        kernel = work_dir / "mihomo"
        kernel.write_bytes(b"FAKE-ELF-BODY")
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(kernel))
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo", lambda skip_hash_check=False: None)
        monkeypatch.setattr(convert_mrs.sys, "argv", ["convert_mrs.py", "--print-kernel-hash"])

        convert_mrs.main()

        printed = capsys.readouterr().out.strip().splitlines()[-1]
        assert printed == hashlib.sha256(b"FAKE-ELF-BODY").hexdigest()

    def test_conversion_failure_exits_nonzero(self, monkeypatch, work_dir):
        product = work_dir / "merged-rules" / "block" / "domain" / "Owner" / "ads.txt"
        product.parent.mkdir(parents=True)
        product.write_text("a.example\n", encoding="utf-8")
        monkeypatch.setattr(convert_mrs, "SRC_ROOT", str(work_dir / "merged-rules"))
        monkeypatch.setattr(convert_mrs, "DST_ROOT", str(work_dir / "merged-rules-mrs"))
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", sys.executable)
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo", lambda: None)
        monkeypatch.setattr(convert_mrs.sys, "argv", ["convert_mrs.py"])

        with pytest.raises(SystemExit) as exc:
            convert_mrs.main()

        assert exc.value.code == 1

    def test_empty_product_is_skipped_not_failed(self, monkeypatch, work_dir):
        product = work_dir / "merged-rules" / "block" / "domain" / "Owner" / "ads.txt"
        product.parent.mkdir(parents=True)
        product.write_text("# 只有注释\n", encoding="utf-8")
        monkeypatch.setattr(convert_mrs, "SRC_ROOT", str(work_dir / "merged-rules"))
        monkeypatch.setattr(convert_mrs, "DST_ROOT", str(work_dir / "merged-rules-mrs"))
        monkeypatch.setattr(convert_mrs, "KERNEL_BIN", str(work_dir / "missing-kernel"))
        monkeypatch.setattr(convert_mrs, "ensure_kernel_platform", lambda: None)
        monkeypatch.setattr(convert_mrs, "get_latest_mihomo", lambda: None)
        monkeypatch.setattr(convert_mrs.sys, "argv", ["convert_mrs.py"])

        with pytest.raises(SystemExit) as exc:
            convert_mrs.main()

        assert exc.value.code == 0, "只有被跳过的文件时不算失败（配对校验在发布环节兜底）"
        assert not list(Path(convert_mrs.DST_ROOT).rglob("*.mrs"))


class TestDegradedSummary:
    def test_reasons_are_listed_and_cells_are_escaped(self, work_dir, monkeypatch):
        summary = work_dir / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        stats = main.SyncStats()
        stats.download_errors.append(("https://x.test/a|b.txt", "第一行\n第二行|尾巴"))
        stats.parse_errors.append(("https://x.test/c.txt", "解析失败: 多行\n错误"))

        main.generate_summary(stats, ["存在失败源（下载 1 / 解析 1）且 behavior.allow_partial=false"])

        text = summary.read_text(encoding="utf-8")
        assert "### 本次未发布（消费者继续使用上一版产物）" in text
        assert "- 存在失败源（下载 1 / 解析 1）且 behavior.allow_partial=false" in text
        assert "第一行 第二行\\|尾巴" in text
        assert "第一行\n第二行" not in text


EMPTY_PAYLOAD_CHILD = r"""
import main as M
async def fake(tasks):
    return [(t, b'payload: []\n', None) for t in tasks]
M.download_all = fake
M.main()
"""


class TestEmptyPayloadDoesNotSilentlyDelete:
    def test_empty_payload_exits_and_keeps_previous_products(self, work_dir):
        victim = work_dir / "rulesets" / "block" / "domain" / "old" / "keep.txt"
        victim.parent.mkdir(parents=True)
        victim.write_text("+.keep.example\n", encoding="utf-8")
        (work_dir / "config.yaml").write_text(
            "network:\n  timeout_seconds: 15\n  max_retries: 2\n  max_source_bytes: 67108864\n"
            'paths:\n  sources_file: "sources.urls"\n  rulesets_dir: "rulesets"\n'
            "behavior:\n  min_source_success_ratio: 0.0\n"
            "  allow_partial: false\n",
            encoding="utf-8",
        )
        (work_dir / "sources.urls").write_text(
            "[policy:block]\n[type:domain]\nhttps://raw.githubusercontent.com/example/repo/main/ads.txt\n",
            encoding="utf-8",
        )
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", EMPTY_PAYLOAD_CHILD],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        assert proc.returncode == 1, proc.stdout
        assert victim.exists()
        assert "未解析出任何行" in (proc.stdout or "")
