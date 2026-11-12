"""README 生成：路径锚定仓库根、写入原子化。"""

from pathlib import Path

import gen_readme
import pytest
from support import isolate_readme_paths, write_quickstart_mrs_placeholders

REPO = Path(__file__).resolve().parent.parent


def isolate(work_dir, monkeypatch, readme):
    isolate_readme_paths(monkeypatch, work_dir, readme)


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
