import utils


def products(root):
    txt = root / "merged-rules"
    mrs = root / "merged-rules-mrs"
    txt.mkdir()
    mrs.mkdir()
    (mrs / "rules.mrs").write_bytes(b"MRS-1")
    return txt, mrs


class TestCombinedHashIncludesPath:
    def test_txt_rename_changes_hash(self, work_dir):
        txt, mrs = products(work_dir)
        source = txt / "a.txt"
        source.write_text("a.com\n", encoding="utf-8")
        first, c1, c2 = utils.combined_products_hash(str(txt), str(mrs))

        source.rename(txt / "z.txt")
        second, c1_after, c2_after = utils.combined_products_hash(str(txt), str(mrs))

        assert first != second
        assert (c1, c2) == (c1_after, c2_after) == (1, 1)

    def test_mrs_rename_changes_hash(self, work_dir):
        txt, mrs = products(work_dir)
        first, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))

        (mrs / "rules.mrs").rename(mrs / "other.mrs")
        second, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))

        assert first != second

    def test_relative_subdir_changes_hash(self, work_dir):
        txt, mrs = products(work_dir)
        (txt / "sub").mkdir()
        source = txt / "sub" / "a.txt"
        source.write_text("a.com\n", encoding="utf-8")
        first, c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))
        assert c1 == 1

        source.rename(txt / "a.txt")
        second, _c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))

        assert first != second

    def test_count_unchanged_by_rename(self, work_dir):
        txt, mrs = products(work_dir)
        (txt / "a.txt").write_text("a.com\n", encoding="utf-8")
        (txt / "b.txt").write_text("b.com\n", encoding="utf-8")
        first, c1, _c2 = utils.combined_products_hash(str(txt), str(mrs))

        (txt / "b.txt").rename(txt / "c.txt")
        second, c1_after, _c2 = utils.combined_products_hash(str(txt), str(mrs))

        assert (c1, c1_after) == (2, 2)
        assert first != second


class TestDirHashIsPathSensitive:
    def test_rename_changes_skip_comments_digest(self, work_dir):
        root = work_dir / "rules"
        root.mkdir()
        target = root / "a.txt"
        target.write_text("# h\na.com\n", encoding="utf-8")
        first, c1 = utils.dir_hash(str(root), "*.txt", skip_comments=True)

        target.rename(root / "z.txt")
        second, c2 = utils.dir_hash(str(root), "*.txt", skip_comments=True)

        assert (c1, c2) == (1, 1)
        assert first != second

    def test_rename_changes_raw_digest(self, work_dir):
        root = work_dir / "rules"
        root.mkdir()
        target = root / "a.mrs"
        target.write_bytes(b"MRS")
        first, _c1 = utils.dir_hash(str(root), "*.mrs")

        target.rename(root / "z.mrs")
        second, _c2 = utils.dir_hash(str(root), "*.mrs")

        assert first != second


class TestDirHashSkipsHidden:
    def test_hidden_dir_is_ignored(self, work_dir):
        root = work_dir / "rules"
        (root / ".hidden").mkdir(parents=True)
        (root / ".hidden" / "a.txt").write_text("a.com\n", encoding="utf-8")
        assert utils.dir_hash(str(root), "*.txt") == ("", 0)
