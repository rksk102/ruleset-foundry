import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))  # noqa: E402


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
