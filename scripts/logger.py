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

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

_logger = None
_log_file_handle = None


def _init_logger():
    global _logger, _log_file_handle
    if _logger is not None:
        return

    _logger = logging.getLogger("mihomo-rules")
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(console)

    _log_file_handle = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    _log_file_handle.setLevel(logging.DEBUG)
    _log_file_handle.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _logger.addHandler(_log_file_handle)


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
    _logger.warning(f"{Colors.YELLOW}[WARN] {msg}{Colors.RESET}", *args)


def error(msg, *args):
    _init_logger()
    _logger.error(f"{Colors.RED}[ERR] {msg}{Colors.RESET}", *args)


def success(msg, *args):
    _init_logger()
    _logger.info(f"{Colors.GREEN}[OK] {msg}{Colors.RESET}", *args)


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
