"""README 生成：路径锚定仓库根、写入原子化。"""
from pathlib import Path

import gen_readme
import pytest

REPO = Path(__file__).resolve().parent.parent


def isolate(work_dir, monkeypatch, readme):
    monkeypatch.setattr(gen_readme, "README_FILE", str(readme))
    monkeypatch.setattr(gen_readme, "DIR_RULES", str(work_dir / "merged-rules"))
    monkeypatch.setattr(gen_readme, "DIR_MRS", str(work_dir / "merged-rules-mrs"))


class TestReadmeWiring:

    def test_root_does_not_follow_cwd(self):
        assert Path(gen_readme.REPO_ROOT) == REPO

    def test_written_to_configured_path_not_cwd(self, work_dir, monkeypatch):
        readme = work_dir / "out" / "README.md"
        isolate(work_dir, monkeypatch, readme)
        monkeypatch.chdir(work_dir)

        gen_readme.main()

        assert readme.exists()
        assert not (work_dir / "README.md").exists()

    def test_failed_generation_keeps_previous_readme(self, work_dir, monkeypatch):
        readme = work_dir / "README.md"
        readme.write_text("旧内容\n", encoding="utf-8")
        isolate(work_dir, monkeypatch, readme)

        def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(gen_readme, "make_static_sections", boom)
        with pytest.raises(SystemExit) as exc:
            gen_readme.main()

        assert exc.value.code == 1
        assert readme.read_text(encoding="utf-8") == "旧内容\n"
