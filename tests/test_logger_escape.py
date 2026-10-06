import logging
from pathlib import Path

import logger as log

LOG_DIR = Path(__file__).resolve().parent.parent / ".tools" / "pytest-logs"


def _reset(monkeypatch, *, ci=False):
    for handler in logging.getLogger("mihomo-rules").handlers:
        handler.close()
    log._logger = None
    log._log_file_handle = None
    log._LOG_FILE = None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log, "_resolve_log_file", lambda: LOG_DIR / "run-test.log")
    if ci:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


class TestEscapeData:

    def test_percent_escaped_first(self):
        assert log._escape_data("50%") == "50%25"

    def test_carriage_return_escaped(self):
        assert log._escape_data("a\rb") == "a%0Db"

    def test_line_feed_escaped(self):
        assert log._escape_data("a\nb") == "a%0Ab"

    def test_literal_escape_sequence_is_double_escaped(self):
        assert log._escape_data("%0A") == "%250A"

    def test_plain_text_untouched(self):
        assert log._escape_data("下载完成") == "下载完成"

    def test_non_string_input(self):
        assert log._escape_data(None) == "None"
        assert log._escape_data(12) == "12"


class TestUnescapeData:

    def test_round_trip(self):
        for text in ["50%", "a\rb", "a\nb", "%0A", "%25", "多行\n文本 100%", ""]:
            assert log._unescape_data(log._escape_data(text)) == text

    def test_single_pass_does_not_redecode(self):
        assert log._unescape_data("%250A") == "%0A"


class TestCommandEscaping:

    def test_gh_error_escapes_newlines(self, capsys):
        log.gh_error("boom\n::error::injected")
        out = capsys.readouterr().out
        assert out == "::error::boom%0A::error::injected\n"
        assert not any(line.startswith("::error::injected") for line in out.splitlines())

    def test_gh_error_escapes_percent(self, capsys):
        log.gh_error("失败率 50%")
        assert capsys.readouterr().out == "::error::失败率 50%25\n"

    def test_group_start_escapes_newlines(self, monkeypatch, capsys):
        _reset(monkeypatch)
        log.group_start("组\n::endgroup::")
        out = capsys.readouterr().out
        assert "::group::组%0A::endgroup::" in out
        assert not any(line.strip() == "::endgroup::" for line in out.splitlines())

    def test_warning_cannot_inject_command_even_after_trim(self, monkeypatch, capsys):
        """The runner trims leading whitespace, so indenting is not a defence."""
        _reset(monkeypatch, ci=True)
        log.warning("ok\n::error::injected\n   ::add-mask::SECRET")
        out = capsys.readouterr().out
        parser = __import__("re").compile(r"^::([A-Za-z0-9_-]+)")
        parsed = [ln.strip() for ln in out.splitlines() if parser.match(ln.strip())]
        assert [p for p in parsed if "injected" in p or "add-mask" in p] == []
        assert not any(ln.strip().startswith("::error::injected") for ln in out.splitlines())
        assert not any(ln.strip().startswith("::add-mask::") for ln in out.splitlines())

    def test_error_message_stays_readable_and_not_a_command(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.error("ok\r\n::warning::injected")
        out = capsys.readouterr().out
        assert not any(line.startswith("::warning::") for line in out.splitlines())
        assert log._escape_data("ok\r\n::warning::injected") == "ok%0D%0A::warning::injected"

    def test_info_and_success_keep_newlines_unescaped(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.info("a\nb")
        log.success("c\nd")
        out = capsys.readouterr().out
        assert "a\nb" in out
        assert "c\nd" in out
        assert "%0A" not in out

    def test_section_stays_readable(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.section("标题\n第二行")
        out = capsys.readouterr().out
        assert out.startswith("\n")
        assert "标题\n第二行" in out
        assert "%0A" not in out

    def test_percent_signs_are_not_escaped_in_log_lines(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.info("覆盖率 12.0%")
        log.warning("阈值 50%")
        out = capsys.readouterr().out
        assert "12.0%" in out
        assert "50%" in out
        assert "%25" not in out


class TestConsoleReadability:

    def test_local_console_restores_newlines(self, monkeypatch, capsys):
        _reset(monkeypatch)
        log.warning("第一行\n第二行")
        out = capsys.readouterr().out
        assert "第一行\n第二行" in out
        assert "%0A" not in out

    def test_local_console_restores_percent(self, monkeypatch, capsys):
        _reset(monkeypatch)
        log.info("进度 50%")
        assert "进度 50%" in capsys.readouterr().out

    def test_ci_console_keeps_lines_readable(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.warning("第一行\n第二行")
        out = capsys.readouterr().out
        assert "第一行\n第二行" in out
        assert "%0A" not in out


class TestLogFile:

    def test_log_file_has_no_ansi_and_keeps_message(self, monkeypatch):
        _reset(monkeypatch, ci=True)
        log.warning("磁盘告警")
        logging.getLogger("mihomo-rules").handlers[1].flush()
        content = (LOG_DIR / "run-test.log").read_text(encoding="utf-8")
        assert "磁盘告警" in content
        assert "\x1b" not in content
