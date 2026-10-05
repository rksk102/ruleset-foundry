import logging
import logging.handlers
import re
import sys
from pathlib import Path

from utils import beijing_now


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

LOG_KEEP_COUNT = 20

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_logger = None
_log_file_handle = None
_LOG_FILE = None


class _StripAnsiFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str) and "\x1b" in record.msg:
            record.msg = _ANSI_RE.sub("", record.msg)
        return True


class _BeijingFormatter(logging.Formatter):
    """以北京时间渲染 asctime，避免与产物/README 时间戳语义不一致。"""

    def formatTime(self, record, datefmt=None):
        dt = beijing_now()
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


def _resolve_log_file():
    """延迟解析日志文件路径，优先读取 config 的 paths.log_dir。"""
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
    _LOG_FILE = log_dir / f"run-{beijing_now().strftime('%Y%m%d-%H%M%S')}.log"
    return _LOG_FILE


def _cleanup_old_logs():
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
    _log_file_handle.setFormatter(_BeijingFormatter(
        "[%(asctime)s] %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _log_file_handle.addFilter(_StripAnsiFilter())
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


def gh_error(msg):
    print(f"::error::{msg}")


def section(msg):
    _init_logger()
    _logger.info(f"\n{Colors.BOLD}{Colors.MAGENTA}>> {msg}{Colors.RESET}")
