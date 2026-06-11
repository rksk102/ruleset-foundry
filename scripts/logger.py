"""
统一日志模块 — 所有脚本使用同一个 logger 封装。
支持 GitHub Actions 的 ::group:: 语法、彩色终端输出、文件持久化。
"""
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

# ---------- 日志目录 ----------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

# ---------- 全局 logger ----------
_logger = None
_log_file_handle = None


def _init_logger():
    global _logger, _log_file_handle
    if _logger is not None:
        return

    _logger = logging.getLogger("mihomo-rules")
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    # 控制台 handler（INFO 级别，简洁格式）
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(console)

    # 文件 handler（DEBUG 级别，详细格式）
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
    """获取统一 logger 实例"""
    _init_logger()
    return _logger


# ---------- 便捷函数 ----------

def info(msg, *args):
    _init_logger()
    _logger.info(str(msg), *args)


def debug(msg, *args):
    _init_logger()
    _logger.debug(str(msg), *args)


def warning(msg, *args):
    _init_logger()
    _logger.warning(f"{Colors.YELLOW}⚠ {msg}{Colors.RESET}", *args)


def error(msg, *args):
    _init_logger()
    _logger.error(f"{Colors.RED}✖ {msg}{Colors.RESET}", *args)


def success(msg, *args):
    _init_logger()
    _logger.info(f"{Colors.GREEN}✔ {msg}{Colors.RESET}", *args)


def group_start(title):
    """GitHub Actions 折叠组开始"""
    _init_logger()
    title_str = str(title)
    print(f"::group::{Colors.BOLD}{Colors.CYAN}{title_str}{Colors.RESET}")
    sys.stdout.flush()
    _logger.debug(f"[GROUP START] {title_str}")


def group_end():
    """GitHub Actions 折叠组结束"""
    _init_logger()
    print("::endgroup::")
    sys.stdout.flush()
    _logger.debug("[GROUP END]")


def banner(text):
    """彩色横幅"""
    _init_logger()
    print(f"\n{Colors.BOLD}{Colors.GREEN}{'=' * 60}")
    print(f" {text}")
    print(f"{'=' * 60}{Colors.RESET}\n")


def gh_error(msg):
    """输出 GitHub Actions 注解错误"""
    print(f"::error::{msg}")


def gh_warning(msg):
    """输出 GitHub Actions 注解警告"""
    print(f"::warning::{msg}")


def section(msg):
    """段落标题"""
    _init_logger()
    _logger.info(f"\n{Colors.BOLD}{Colors.MAGENTA}▸ {msg}{Colors.RESET}")
