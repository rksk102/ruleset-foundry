"""README 生成：路径锚定仓库根、写入原子化。"""

from pathlib import Path

import config_loader
import gen_readme
import pytest
import yaml
from support import isolate_readme_paths, write_paired_product, write_quickstart_mrs_placeholders

REPO = Path(__file__).resolve().parent.parent


def quickstart_merges():
    return [
        {
            "strategy": "block",
            "type": "domain",
            "owner": "rksk102",
            "filename": "all-adblock.txt",
            "inputs": ["block/domain/rksk102/ads.txt"],
        },
        {
            "strategy": "direct",
            "type": "ipcidr",
            "owner": "rksk102",
            "filename": "all-cnip.txt",
            "inputs": ["direct/ipcidr/rksk102/cn.txt"],
        },
        {
            "strategy": "policy",
            "type": "domain",
            "owner": "rksk102",
            "filename": "all-proxy.txt",
            "inputs": ["policy/domain/rksk102/proxy.txt"],
        },
    ]


def quickstart_references():
    return {
        "block": "block/domain/rksk102/all-adblock.txt",
        "direct": "direct/ipcidr/rksk102/all-cnip.txt",
        "policy": "policy/domain/rksk102/all-proxy.txt",
    }


def isolate(work_dir, monkeypatch, readme):
    isolate_readme_paths(monkeypatch, work_dir, readme)


def use_local_config(work_dir, monkeypatch, merges, references=None):
    data = {"merges": merges}
    if references is not None:
        data["quickstart"] = references
    config_file = work_dir / "config.yaml"
    config_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(config_loader, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(config_loader.utils, "REPO_ROOT", work_dir)
    monkeypatch.setenv("ARTIFACTS_BRANCH", "artifacts")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def assert_current_quickstart_urls(text):
    for reference in quickstart_references().values():
        mrs_path = "merged-rules-mrs/" + reference.removesuffix(".txt") + ".mrs"
        assert f"https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/{mrs_path}" in text


class TestReadmeWiring:
    def test_root_does_not_follow_cwd(self):
        assert Path(gen_readme.REPO_ROOT) == REPO

    def test_written_to_configured_path_not_cwd(self, work_dir, monkeypatch):
        readme = work_dir / "out" / "README.md"
        isolate(work_dir, monkeypatch, readme)
        monkeypatch.chdir(work_dir)
        write_quickstart_mrs_placeholders(work_dir)

        gen_readme.main(["--allow-placeholder", "--allow-empty"])

        assert readme.exists()
        assert not (work_dir / "README.md").exists()

    def test_explicit_references_allow_additional_task_for_same_strategy(self, work_dir, monkeypatch):
        additional = {
            "strategy": "block",
            "type": "domain",
            "owner": "other",
            "filename": "additional.txt",
            "inputs": ["block/domain/other/additional.txt"],
        }
        references = quickstart_references()
        use_local_config(work_dir, monkeypatch, [additional, *quickstart_merges()], references)
        monkeypatch.setenv("GITHUB_REPOSITORY", "rksk102/ruleset-foundry")
        readme = work_dir / "README.md"
        isolate(work_dir, monkeypatch, readme)
        write_paired_product(work_dir, references["block"], b"ads.example.com\n")
        write_paired_product(work_dir, references["direct"], b"192.0.2.0/24\n")
        write_paired_product(work_dir, references["policy"], b"proxy.example.com\n")
        write_paired_product(work_dir, "block/domain/other/additional.txt", b"extra.example.com\n")

        gen_readme.main([])

        quickstart, inventory = readme.read_text(encoding="utf-8").split("## 产物清单", 1)
        assert_current_quickstart_urls(quickstart)
        assert quickstart.count("format: mrs") == 3
        assert "additional.mrs" not in quickstart
        assert "/block/domain/other/additional.mrs" in inventory

    def test_local_preview_without_quickstart_references_remains_compatible(self, work_dir, monkeypatch):
        use_local_config(work_dir, monkeypatch, quickstart_merges())
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        readme = work_dir / "README.md"
        isolate(work_dir, monkeypatch, readme)
        write_quickstart_mrs_placeholders(work_dir)

        gen_readme.main(["--allow-placeholder", "--allow-empty"])

        assert_current_quickstart_urls(readme.read_text(encoding="utf-8"))

    def test_failed_generation_keeps_previous_readme(self, work_dir, monkeypatch):
        readme = work_dir / "README.md"
        readme.write_text("旧内容\n", encoding="utf-8")
        isolate(work_dir, monkeypatch, readme)

        def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(gen_readme, "make_static_sections", boom)
        with pytest.raises(SystemExit) as exc:
            gen_readme.main(["--allow-placeholder", "--allow-empty"])

        assert exc.value.code == 1
        assert readme.read_text(encoding="utf-8") == "旧内容\n"
