import os
import sys
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"

# 保留最近 N 个日志文件，避免无限增长
LOG_KEEP_COUNT = 20

_logger = None
_log_file_handle = None
_LOG_FILE = None


def _resolve_log_file():
    """延迟解析日志目录与文件路径，避免模块导入时即创建目录。

    优先从 config_loader 读取 paths.log_dir，缺失时回退到 "logs"。
    """
    global _LOG_FILE
    if _LOG_FILE is not None:
        return _LOG_FILE

    log_dir_name = "logs"
    try:
        from config_loader import get
        log_dir_name = get("paths", "log_dir", default="logs") or "logs"
    except Exception:
        pass

    log_dir = Path(log_dir_name)
    log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    return _LOG_FILE


def _cleanup_old_logs():
    """保留最近 LOG_KEEP_COUNT 个日志文件，删除更早的。"""
    try:
        log_file = _resolve_log_file()
        log_dir = log_file.parent
        logs = sorted(
            log_dir.glob("run-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in logs[LOG_KEEP_COUNT:]:
            old.unlink()
    except Exception:
        pass


def _init_logger():
    global _logger, _log_file_handle
    if _logger is not None:
        return

    log_file = _resolve_log_file()

    _logger = logging.getLogger("mihomo-rules")
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(console)

    _log_file_handle = logging.FileHandler(str(log_file), encoding="utf-8")
    _log_file_handle.setLevel(logging.DEBUG)
    _log_file_handle.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _logger.addHandler(_log_file_handle)

    _cleanup_old_logs()


def get_logger():
    _init_logger()
    return _logger


def info(msg, *args):
    _init_logger()
    _logger.info(str(msg), *args)


def debug(msg, *args):
    _init_logger()
    _logger.debug(str(msg), *args)


def warning(msg, *args):
    _init_logger()
    _logger.warning(f"{Colors.YELLOW}[警告] {msg}{Colors.RESET}", *args)


def error(msg, *args):
    _init_logger()
    _logger.error(f"{Colors.RED}[错误] {msg}{Colors.RESET}", *args)


def success(msg, *args):
    _init_logger()
    _logger.info(f"{Colors.GREEN}[成功] {msg}{Colors.RESET}", *args)


def group_start(title):
    _init_logger()
    title_str = str(title)
    print(f"::group::{title_str}")
    sys.stdout.flush()
    _logger.debug(f"[GROUP START] {title_str}")


def group_end():
    _init_logger()
    print("::endgroup::")
    sys.stdout.flush()
    _logger.debug("[GROUP END]")


def banner(text):
    _init_logger()
    print(f"\n{Colors.BOLD}{Colors.GREEN}{'=' * 60}")
    print(f" {text}")
    print(f"{'=' * 60}{Colors.RESET}\n")


def gh_error(msg):
    print(f"::error::{msg}")


def gh_warning(msg):
    print(f"::warning::{msg}")


def section(msg):
    _init_logger()
    _logger.info(f"\n{Colors.BOLD}{Colors.MAGENTA}>> {msg}{Colors.RESET}")
