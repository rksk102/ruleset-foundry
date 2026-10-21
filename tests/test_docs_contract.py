"""文档与实现的契约：语义表、产物路径、消费方式（behavior/format）的改动都必须在这里失败。"""

import re
from pathlib import Path

import gen_readme

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
            row = next((line for line in text.splitlines() if line.startswith(f"| `{form}`")), None)
            assert row is not None, f"语义表缺少 {form} 行"
            assert tuple(re.findall(r"[✅❌]", row)) == cells, row


class TestConsumerContract:
    def test_behavior_format_table_has_one_row_per_product_kind(self):
        text = gen_readme.make_inventory([], [])
        rows = [line for line in text.splitlines() if line.startswith("| `merged-rules")]
        cells = {tuple(cell.strip() for cell in row.strip("|").split("|")) for row in rows}
        assert ("`merged-rules/<策略>/domain/**/*.txt`", "`domain`", "`text`") in cells
        assert ("`merged-rules/<策略>/ipcidr/**/*.txt`", "`ipcidr`", "`text`") in cells
        assert ("`merged-rules-mrs/**/*.mrs`", "与同名 `.txt` 相同", "`mrs`") in cells

    def test_documents_behavior_and_format_everywhere_needed(self):
        text = gen_readme.make_intro() + gen_readme.make_inventory([], []) + gen_readme.make_static_sections()
        assert "format: text" in text
        assert "format: mrs" in text
        assert "behavior: ipcidr" in text

    def test_tables_collapsed_by_default(self):
        text = gen_readme.make_intro() + gen_readme.make_inventory([], []) + gen_readme.make_static_sections()
        assert "<details open>" not in text

    def test_readme_embeds_intro_and_static_sections(self):
        text = README.read_text(encoding="utf-8")
        assert gen_readme.make_intro() in text
        assert text.endswith(gen_readme.make_static_sections())


class TestDocumentedConfigAndWorkflow:
    def test_readme_declares_generated_origin(self):
        text = gen_readme.make_static_sections()
        assert "gen_readme.py" in text


class TestSecurityPolicy:
    SECURITY = ROOT / "SECURITY.md"

    def test_policy_is_concise_and_complete(self):
        text = self.SECURITY.read_text(encoding="utf-8")
        assert "## 范围" in text
        assert "## 信任模型" in text
        assert "## 报告漏洞" in text
        assert "## 不在范围内" in text
