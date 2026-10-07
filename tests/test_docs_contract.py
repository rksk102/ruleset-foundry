"""文档与实现的契约：语义表、产物路径、消费方式（behavior/format）的改动都必须在这里失败。"""
import re
from pathlib import Path

import gen_readme
import main

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

SEMANTICS_ROWS = {
    "+.example.com": ("✅", "✅", "✅"),
    ".example.com": ("❌", "✅", "✅"),
    "example.com": ("✅", "❌", "❌"),
    "*.example.com": ("❌", "✅", "❌"),
    "*.*.example.com": ("❌", "❌", "✅"),
}


class TestSemanticsTable:
    def test_every_row_matches_mihomo_semantics(self):
        text = gen_readme.make_static_sections()
        for form, cells in SEMANTICS_ROWS.items():
            row = next(
                (line for line in text.splitlines() if line.startswith(f"| `{form}`")), None
            )
            assert row is not None, f"语义表缺少 {form} 行"
            assert tuple(re.findall(r"[✅❌]", row)) == cells, row

    def test_readme_embeds_the_generated_static_sections(self):
        assert README.read_text(encoding="utf-8").endswith(gen_readme.make_static_sections())


class TestConsumerContract:
    def test_behavior_format_table_has_one_row_per_product_kind(self):
        text = gen_readme.make_static_sections()
        rows = [line for line in text.splitlines() if line.startswith("| `merged-rules")]
        cells = {
            tuple(cell.strip() for cell in row.strip("|").split("|")) for row in rows
        }
        assert ("`merged-rules/<策略>/domain/**/*.txt`", "`domain`", "`text`") in cells
        assert ("`merged-rules/<策略>/ipcidr/**/*.txt`", "`ipcidr`", "`text`") in cells
        assert ("`merged-rules-mrs/**/*.mrs`", "与同名 `.txt` 相同", "`mrs`") in cells

    def test_documents_behavior_and_format_everywhere_needed(self):
        text = gen_readme.make_static_sections()
        assert "format: text" in text
        assert "format: mrs" in text
        assert "behavior: ipcidr" in text

    def test_kernel_upgrade_steps_put_pin_before_hashing(self):
        text = gen_readme.make_static_sections()
        assert "2. 先改 `config.yaml` 的 `pinned_version` 与 `asset_name`" in text
        assert "3. 再运行 `python scripts/convert_mrs.py --print-kernel-hash`" in text
        assert "顺序颠倒" in text
        assert "只在 Linux 上可用" in text


class TestPublishedPathStability:
    def test_displayable_upstream_name_survives(self):
        task = {
            "url": "https://github.com/MetaCubeX/meta-rules-dat/raw/refs/heads/meta/"
                   "geo/geosite/category-ai-!cn.list",
            "policy": "policy",
            "type": "domain",
            "domain_kind": "exact",
        }
        _owner, _name, rel, _abs_path = main.build_filepath(task)
        assert rel.as_posix() == "policy/domain/MetaCubeX/category-ai-!cn.txt"


class TestDocumentedConfigAndWorkflow:

    CONFIG_KEYS = (
        "network.timeout_seconds", "network.max_retries", "network.max_source_bytes",
        "network.max_concurrency", "network.max_per_host", "network.max_retry_after_seconds",
        "paths.sources_file", "paths.rulesets_dir", "paths.merged_output_dir",
        "paths.mrs_output_dir", "paths.cache_dir", "paths.log_dir",
        "behavior.strict_mode", "behavior.release_change_detection",
        "behavior.release_keep_days", "behavior.conflict_policy",
        "behavior.unrecognized_warn_ratio", "behavior.min_source_success_ratio",
        "behavior.allow_partial", "mihomo.kernel_cache_path", "mihomo.repo_api",
        "mihomo.pinned_version", "mihomo.asset_name", "mihomo.kernel_sha256", "merges",
    )

    def test_config_reference_lists_every_key(self):
        text = gen_readme.make_static_sections()
        for key in self.CONFIG_KEYS:
            assert f"`{key}`" in text, key

    def test_merge_mechanism_and_local_dev_documented(self):
        text = gen_readme.make_static_sections()
        assert "规则合并任务（merges）" in text
        assert "本地开发与测试" in text
        assert "requirements-dev.lock" in text
        assert "--cov-fail-under=70" in text

    def test_readme_declares_generated_origin(self):
        text = gen_readme.make_static_sections()
        assert "gen_readme.py" in text
        assert "make_static_sections()" in text

    def test_kernel_auto_follow_trust_model_documented(self):
        text = gen_readme.make_static_sections()
        assert "自动跟随的信任模型" in text
        assert "没有 PR 评审" in text
