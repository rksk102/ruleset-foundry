"""依赖清单与 uv 锁必须同步：改动 pyproject.toml 后需重新执行 uv lock。"""

import tomllib
from collections import Counter
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent
GROUPS = ("runtime", "dev")


def load_toml(name):
    return tomllib.loads((REPO / name).read_text(encoding="utf-8"))


def manifest_requirements(group):
    pyproject = load_toml("pyproject.toml")
    if group == "runtime":
        lines = pyproject["project"]["dependencies"]
    else:
        lines = pyproject.get("dependency-groups", {}).get("dev", [])
    return [Requirement(line) for line in lines]


def locked_packages():
    lock = load_toml("uv.lock")
    return {p["name"].lower().replace("_", "-"): p for p in lock["package"]}


class TestLockSync:
    @pytest.mark.parametrize("group", GROUPS)
    def test_direct_dependencies_match_lock(self, group):
        packages = locked_packages()
        assert packages, "uv.lock 未解析出任何包"
        requirements = manifest_requirements(group)
        assert requirements, f"pyproject.toml 未声明 {group} 依赖"
        for requirement in requirements:
            name = requirement.name.lower().replace("_", "-")
            assert name in packages, f"{name} 未出现在 uv.lock，请执行 uv lock"
            locked_version = packages[name]["version"]
            if requirement.specifier:
                assert requirement.specifier.contains(locked_version, prereleases=True), (
                    f"{name} 锁定 {locked_version} 不满足 pyproject.toml 的 {requirement.specifier}，"
                    "请执行 uv lock"
                )

    def test_runtime_dependencies_are_locked(self):
        # 旧 requirements.lock / requirements-dev.lock 的“运行时是开发锁子集”检查
        # 由单一 uv.lock 的结构保证；这里保留显式的运行时依赖存在性检查。
        packages = locked_packages()
        missing = [
            requirement.name
            for requirement in manifest_requirements("runtime")
            if requirement.name.lower().replace("_", "-") not in packages
        ]
        assert not missing, f"uv.lock 缺少运行时依赖: {missing}"

    def test_every_locked_package_is_hash_pinned(self):
        for name, package in locked_packages().items():
            source = package.get("source") or {}
            if source.get("virtual") or source.get("editable"):
                continue
            sdist = package.get("sdist") or {}
            wheels = package.get("wheels") or []
            assert sdist.get("hash") or wheels, f"uv.lock 中 {name}=={package['version']} 没有任何分发哈希"
            assert all(wheel.get("hash") for wheel in wheels), (
                f"uv.lock 中 {name}=={package['version']} 有 wheel 缺少 hash"
            )
            if sdist:
                assert sdist.get("hash"), f"uv.lock 中 {name}=={package['version']} 的 sdist 缺少 hash"

    def test_lock_requires_python_matches_project(self):
        pyproject = load_toml("pyproject.toml")
        lock = load_toml("uv.lock")
        assert lock["requires-python"] == pyproject["project"]["requires-python"]

    def test_no_duplicate_package_entries(self):
        names = [p["name"].lower().replace("_", "-") for p in load_toml("uv.lock")["package"]]
        duplicates = [name for name, count in Counter(names).items() if count > 1]
        assert not duplicates, f"uv.lock 存在重复包: {duplicates}"
