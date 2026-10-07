"""sources.urls 标记契约：大小写、尾随注释、未知标记 fail-closed。"""
from pathlib import Path

import main
import pytest

REPO = Path(__file__).resolve().parent.parent
REAL_SOURCES = REPO / "sources.urls"

URL = "https://raw.githubusercontent.com/owner/repo/main/list.txt"


def parse(monkeypatch, work_dir, body):
    path = work_dir / "sources.urls"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(main, "SOURCES_FILE", str(path))
    return main.parse_sources()


class TestMarkerParsing:
    def test_keys_are_case_insensitive(self, work_dir, monkeypatch):
        tasks = parse(monkeypatch, work_dir,
                      "[Policy:direct]\n[Type:ipcidr]\n[Domain-Kind:suffix]\n" + URL + "\n")
        assert tasks[0]["policy"] == "direct"
        assert tasks[0]["type"] == "ipcidr"
        assert tasks[0]["domain_kind"] == "suffix"

    def test_trailing_comment_is_allowed(self, work_dir, monkeypatch):
        tasks = parse(monkeypatch, work_dir,
                      "[policy:block] # 广告拦截\n[type:domain]  # 说明\n" + URL + "\n")
        assert tasks[0]["policy"] == "block"
        assert tasks[0]["type"] == "domain"

    def test_unknown_marker_key_reports_line_number(self, work_dir, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            parse(monkeypatch, work_dir, "[policy:block]\n[polcy:direct]\n" + URL + "\n")
        assert exc.value.code == 1
        assert "sources.urls:2" in capsys.readouterr().out

    def test_marker_with_trailing_junk_exits(self, work_dir, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            parse(monkeypatch, work_dir, "[policy:block] 多余内容\n" + URL + "\n")
        assert exc.value.code == 1

    def test_invalid_domain_kind_still_exits(self, work_dir, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            parse(monkeypatch, work_dir, "[domain-kind:exactt]\n" + URL + "\n")
        assert exc.value.code == 1

    def test_inline_marker_is_ignored(self, work_dir, monkeypatch):
        tasks = parse(monkeypatch, work_dir, "前缀 [policy:block] 后缀\n" + URL + "\n")
        assert tasks[0]["policy"] == "policy"

    def test_plain_line_without_url_is_ignored(self, work_dir, monkeypatch):
        tasks = parse(monkeypatch, work_dir, "不是链接\n" + URL + "\n")
        assert len(tasks) == 1


class TestRealSourcesContract:
    def test_real_file_parses(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCES_FILE", str(REAL_SOURCES))
        assert len(main.parse_sources()) >= 10

    def test_every_task_is_wellformed(self, monkeypatch):
        monkeypatch.setattr(main, "SOURCES_FILE", str(REAL_SOURCES))
        for task in main.parse_sources():
            assert task["url"].startswith("https://")
            assert task["policy"] in ("block", "direct", "policy")
            assert task["type"] in ("domain", "ipcidr")
            assert task["domain_kind"] in ("exact", "suffix")
