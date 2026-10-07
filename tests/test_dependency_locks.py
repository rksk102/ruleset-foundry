"""依赖清单与哈希锁必须同步：Dependabot 只改 requirements*.txt 时这里会红。"""
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent
PAIRS = (
    ("requirements.txt", "requirements.lock"),
    ("requirements-dev.txt", "requirements-dev.lock"),
)
PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)")


def locked_entries(lock_name):
    entries = []
    for line in (REPO / lock_name).read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(line)
        if match:
            entries.append({
                "name": match.group(1).lower().replace("_", "-"),
                "version": match.group(2),
                "hashes": 0,
            })
        elif entries and "--hash=" in line:
            entries[-1]["hashes"] += line.count("--hash=")
    return entries


def manifest_requirements(manifest_name):
    requirements = []
    for raw in (REPO / manifest_name).read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if line and not line.startswith("-"):
            requirements.append(Requirement(line))
    return requirements


class TestLockSync:

    @pytest.mark.parametrize("manifest_name,lock_name", PAIRS)
    def test_locked_versions_match_manifest(self, manifest_name, lock_name):
        locked = {e["name"]: e["version"] for e in locked_entries(lock_name)}
        assert locked, f"{lock_name} 未解析出任何锁定版本"
        for requirement in manifest_requirements(manifest_name):
            name = requirement.name.lower().replace("_", "-")
            assert name in locked, f"{name} 未出现在 {lock_name}，请重新生成锁文件"
            if requirement.specifier:
                assert requirement.specifier.contains(locked[name], prereleases=True), (
                    f"{name} 锁定 {locked[name]} 不满足 {manifest_name} 的 {requirement.specifier}，"
                    "请在改动清单后重新生成锁文件"
                )

    @pytest.mark.parametrize("_manifest_name,lock_name", PAIRS)
    def test_every_locked_package_is_hash_pinned(self, _manifest_name, lock_name):
        for entry in locked_entries(lock_name):
            assert entry["hashes"] > 0, (
                f"{lock_name} 中 {entry['name']}=={entry['version']} 缺少 --hash，"
                "pip install --require-hashes 会失败"
            )
