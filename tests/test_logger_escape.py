import logging
from pathlib import Path

import config_loader
import logger as log
import pytest
import utils

LOG_DIR = Path(__file__).resolve().parent.parent / ".tools" / "pytest-logs"


def _reset(monkeypatch, *, ci=False):
    for handler in logging.getLogger("mihomo-rules").handlers:
        handler.close()
    log._logger = None
    log._log_file_handle = None
    log._LOG_FILE = None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log, "_resolve_log_file", lambda: LOG_DIR / "run-test.log")
    log_path = LOG_DIR / "run-test.log"
    log_start = log_path.stat().st_size if log_path.exists() else 0
    if ci:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return log_start


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

    def test_info_cannot_inject_command(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.info("ok\n::error::injected")
        out = capsys.readouterr().out
        assert not any(line.startswith("::error::injected") for line in out.splitlines())
        assert "%3A%3Aerror::injected" in out

    def test_bare_carriage_return_is_split(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.info("ok\r::error::injected")
        out = capsys.readouterr().out
        assert not any(line.startswith("::error::injected") for line in out.splitlines())
        assert "%3A%3Aerror::injected" in out

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


class TestControlSequenceEscaping:
    def test_osc_csi_and_esc_sequences_are_escaped(self, monkeypatch, capsys):
        _reset(monkeypatch, ci=True)
        log.info("OSC:\x1b]52;payload\x07 CSI:\x1b[2J ESC:\x1bP")
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "\\x1b]52;payload\\x07" in out
        assert "\\x1b[2J" in out
        assert "\\x1bP" in out

    def test_control_characters_escaped_in_log_file(self, monkeypatch):
        start = _reset(monkeypatch, ci=True)
        log.info("\x1b]52;payload\x07\x85")
        logging.getLogger("mihomo-rules").handlers[1].flush()
        with (LOG_DIR / "run-test.log").open(encoding="utf-8") as handle:
            handle.seek(start)
            content = handle.read()
        assert "\x1b" not in content
        assert "\x07" not in content
        assert "\\x1b]52;payload\\x07\\x85" in content

    def test_unicode_url_and_allowed_whitespace_preserved(self):
        text = "中文 https://example.com/a%20b\n下一行\t缩进"
        assert log._render(text) == text


class TestConsoleReadability:
    def test_local_console_restores_newlines(self, monkeypatch, capsys):
        _reset(monkeypatch)
        log.warning("第一行\n第二行")
        out = capsys.readouterr().out
        assert "第一行\n第二行" in out
        assert "%0A" not in out


class TestLogConfigurationFailure:
    @pytest.mark.parametrize("failure", ["outside-link", "protected-link", "invalid-config", "unreadable-config"])
    def test_configuration_failure_uses_console_without_file_side_effects(
        self, work_dir, monkeypatch, capsys, failure,
    ):
        root = work_dir / "repo"
        root.mkdir()
        config = root / "config.yaml"
        config.write_text("paths:\n  log_dir: logs\n", encoding="utf-8")
        if failure == "invalid-config":
            config.write_text("behavior: [broken\n", encoding="utf-8")
        target = root / "docs" if failure == "protected-link" else work_dir / "outside"
        target.mkdir()
        for index in range(log.LOG_KEEP_COUNT + 1):
            (target / f"run-{index:03d}.log").write_text("preserve\n", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        (root / "logs").symlink_to(target, target_is_directory=True)
        monkeypatch.chdir(root)
        monkeypatch.setattr(utils, "REPO_ROOT", root)
        monkeypatch.setattr(config_loader, "_CONFIG_FILE", config)
        monkeypatch.setattr(log, "_LOG_FILE", None)
        monkeypatch.setattr(log, "_logger", None)
        if failure == "unreadable-config":
            real_open = config_loader.open if hasattr(config_loader, "open") else open

            def unreadable(path, *args, **kwargs):
                if Path(path) == config:
                    raise PermissionError("configuration unreadable")
                return real_open(path, *args, **kwargs)

            monkeypatch.setattr(config_loader, "open", unreadable, raising=False)

        log.error("original operation failed")

        output = capsys.readouterr().out
        assert {p.name: p.read_bytes() for p in target.iterdir()} == before
        assert "original operation failed" in output
        assert "文件日志不可用" in output
        assert not any(isinstance(handler, logging.FileHandler) for handler in log._logger.handlers)
        assert sorted(p.name for p in root.iterdir()) == (
            ["config.yaml", "docs", "logs"] if failure == "protected-link" else ["config.yaml", "logs"]
        )

    def test_valid_configuration_creates_file_and_preserves_message(self, work_dir, monkeypatch):
        root = work_dir / "repo"
        root.mkdir()
        config = root / "config.yaml"
        config.write_text("paths:\n  log_dir: run-logs\n", encoding="utf-8")
        monkeypatch.chdir(root)
        monkeypatch.setattr(utils, "REPO_ROOT", root)
        monkeypatch.setattr(config_loader, "_CONFIG_FILE", config)
        monkeypatch.setattr(log, "_LOG_FILE", None)
        monkeypatch.setattr(log, "_logger", None)

        log.info("written to validated log directory")

        files = list((root / "run-logs").glob("run-*.log"))
        assert len(files) == 1
        assert "written to validated log directory" in files[0].read_text(encoding="utf-8")


class TestLogFile:
    def test_log_file_has_no_ansi_and_keeps_message(self, monkeypatch):
        start = _reset(monkeypatch, ci=True)
        log.warning("磁盘告警")
        logging.getLogger("mihomo-rules").handlers[1].flush()
        with (LOG_DIR / "run-test.log").open(encoding="utf-8") as handle:
            handle.seek(start)
            content = handle.read()
        assert "磁盘告警" in content
        assert "\x1b" not in content

    def test_log_file_strips_ansi_from_message(self, monkeypatch):
        start = _reset(monkeypatch, ci=True)
        log.info("\x1b[31m红色\x1b[0m")
        logging.getLogger("mihomo-rules").handlers[1].flush()
        with (LOG_DIR / "run-test.log").open(encoding="utf-8") as handle:
            handle.seek(start)
            content = handle.read()
        assert "红色" in content
        assert "\x1b" not in content
