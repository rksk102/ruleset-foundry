"""Regression tests for defects found by adversarial review (red team R1/R2/R6)."""
import os
import subprocess
import sys
from pathlib import Path

import main
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO / "scripts")


def write_sources(directory, body):
    (directory / "sources.urls").write_text(body, encoding="utf-8")
    return directory


def plan_from(directory, monkeypatch, body):
    write_sources(directory, body)
    monkeypatch.setattr(main, "SOURCES_FILE", str(directory / "sources.urls"))
    tasks = main.parse_sources()
    return tasks, main.plan_groups(tasks)


URL_A = "https://raw.githubusercontent.com/example/repo/main/ads.txt"
URL_B = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/reject-list.txt"


class TestNoMixedKindPerProduct:
    def test_single_kind_source_is_not_mixed(self, work_dir, monkeypatch):
        _tasks, groups = plan_from(
            work_dir, monkeypatch,
            f"[policy:block]\n[type:domain]\n[domain-kind:suffix]\n{URL_B}\n",
        )
        assert [g.get("domain_kind") for g in groups] == ["suffix"]

    def test_same_url_declared_twice_with_same_kind_is_not_mixed(self, work_dir, monkeypatch):
        _tasks, groups = plan_from(
            work_dir, monkeypatch,
            f"[policy:block]\n[type:domain]\n[domain-kind:suffix]\n{URL_B}\n{URL_B}\n",
        )
        assert [g.get("domain_kind") for g in groups] == ["suffix"]

    def test_real_sources_file_has_no_mixed_products(self):
        groups = main.plan_groups(main.parse_sources())
        mixed = [g["path"] for g in groups if g.get("domain_kind") == "mixed"]
        assert mixed == [], f"这些产物会回落为 exact：{mixed}"

    def test_real_sources_file_marks_dlc_sources_as_suffix(self):
        tasks = main.parse_sources()
        suffix = {t["url"] for t in tasks if t.get("domain_kind") == "suffix"}
        for name in ("reject-list.txt", "win-extra.txt", "win-spy.txt",
                     "direct-list.txt", "proxy-list.txt"):
            assert any(u.endswith(name) for u in suffix), f"{name} 未被标为 suffix"

    def test_no_url_declared_with_two_kinds(self):
        tasks = main.parse_sources()
        kinds = {}
        for t in tasks:
            kinds.setdefault(t["url"], set()).add(t.get("domain_kind"))
        offenders = {u: k for u, k in kinds.items() if len(k) > 1}
        assert offenders == {}, f"同一 URL 声明了多种 domain-kind：{offenders}"


class TestDomainKindMarkerValidation:
    def test_unknown_value_exits(self, work_dir, monkeypatch, capsys):
        write_sources(work_dir, f"[domain-kind:exactt]\n{URL_A}\n")
        monkeypatch.setattr(main, "SOURCES_FILE", str(work_dir / "sources.urls"))
        with pytest.raises(SystemExit) as exc:
            main.parse_sources()
        assert exc.value.code == 1

    def test_empty_value_exits(self, work_dir, monkeypatch):
        write_sources(work_dir, f"[domain-kind:]\n{URL_A}\n")
        monkeypatch.setattr(main, "SOURCES_FILE", str(work_dir / "sources.urls"))
        with pytest.raises(SystemExit) as exc:
            main.parse_sources()
        assert exc.value.code == 1

    def test_case_insensitive_valid_value(self, work_dir, monkeypatch):
        write_sources(work_dir, f"[domain-kind:SUFFIX]\n{URL_A}\n")
        monkeypatch.setattr(main, "SOURCES_FILE", str(work_dir / "sources.urls"))
        tasks = main.parse_sources()
        assert tasks[0]["domain_kind"] == "suffix"

    def test_marker_after_url_does_not_apply_backwards(self, work_dir, monkeypatch):
        write_sources(work_dir, f"[domain-kind:exact]\n{URL_A}\n[domain-kind:suffix]\n{URL_B}\n")
        monkeypatch.setattr(main, "SOURCES_FILE", str(work_dir / "sources.urls"))
        tasks = main.parse_sources()
        by_url = {t["url"]: t["domain_kind"] for t in tasks}
        assert by_url[URL_A] == "exact"
        assert by_url[URL_B] == "suffix"


EMPTY_PAYLOAD_CHILD = r"""
import main as M
async def fake(tasks):
    return [(t, b'payload: []\n', None) for t in tasks]
M.download_all = fake
M.main()
"""


class TestEmptyPayloadDoesNotSilentlyDelete:
    def test_empty_payload_exits_and_keeps_previous_products(self, work_dir):
        (work_dir / "rulesets" / "block" / "domain" / "old").mkdir(parents=True)
        victim = work_dir / "rulesets" / "block" / "domain" / "old" / "keep.txt"
        victim.write_text("+.keep.example\n", encoding="utf-8")
        (work_dir / "config.yaml").write_text(
            'network:\n  timeout_seconds: 15\n  max_retries: 2\n  max_source_bytes: 67108864\n'
            'paths:\n  sources_file: "sources.urls"\n  rulesets_dir: "rulesets"\n'
            'behavior:\n  strict_mode: false\n  min_source_success_ratio: 0.0\n'
            '  allow_partial: false\n',
            encoding="utf-8",
        )
        write_sources(work_dir, f"[policy:block]\n[type:domain]\n{URL_A}\n")

        env = dict(os.environ)
        env["PYTHONPATH"] = SCRIPTS
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-c", EMPTY_PAYLOAD_CHILD],
            cwd=str(work_dir), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=120,
        )
        assert proc.returncode == 1, proc.stdout
        assert victim.exists(), "空规则集不应清掉上一轮产物"
        assert "未解析出任何行" in (proc.stdout or "")
