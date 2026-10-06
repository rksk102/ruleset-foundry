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
