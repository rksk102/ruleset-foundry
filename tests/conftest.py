import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import logger


@pytest.fixture(autouse=True)
def _isolate_github_env(monkeypatch):
    """CI 会设置运行标识；测试用固定 generation 清单，需保持环境无关。"""
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)


@pytest.fixture(autouse=True)
def _isolate_logger():
    log_dir = Path(__file__).resolve().parent.parent / ".tools" / "pytest-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    previous = logger._LOG_FILE
    logger._LOG_FILE = log_dir / "run-test.log"
    yield
    for handler in logging.getLogger("mihomo-rules").handlers:
        handler.close()
    logging.getLogger("mihomo-rules").handlers.clear()
    logger._logger = None
    logger._log_file_handle = None
    logger._LOG_FILE = previous


@pytest.fixture
def work_dir():
    """Sandbox-friendly scratch dir: default perms, always removable, no tmp_path."""
    base = Path(__file__).resolve().parent.parent / ".tools" / "pytest-scratch"
    os.makedirs(base, exist_ok=True)
    path = base / uuid.uuid4().hex[:12]
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
