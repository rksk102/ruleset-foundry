import release_handler


class GhStub:
    def __init__(self, fail_when=None):
        self.calls = []
        self.fail_when = fail_when or (lambda cmd: False)

    def __call__(self, cmd, fail_fast=False):
        self.calls.append(list(cmd))
        return None if self.fail_when(cmd) else "ok"


def run_with_stub(stub, fn):
    original = release_handler.run_gh
    release_handler.run_gh = stub
    try:
        return fn()
    finally:
        release_handler.run_gh = original


ZIP = "merged-rules-2026-10-05.zip"
NOTES = "## notes"


class TestPublishRelease:
    def test_existing_release_updates_in_place(self):
        stub = GhStub()
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result == "ok"
        assert stub.calls[0] == ["release", "upload", "rules-2026-10-05", ZIP, "--clobber"]
        assert stub.calls[1] == [
            "release", "edit", "rules-2026-10-05",
            "--title", "title",
            "--notes", NOTES,
            "--latest",
        ]
        assert len(stub.calls) == 2

    def test_existing_release_never_deletes(self):
        stub = GhStub()
        run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        flat = " ".join(" ".join(c) for c in stub.calls)
        assert "delete" not in flat
        assert "git/refs/tags" not in flat

    def test_missing_release_created(self):
        stub = GhStub()
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, False),
        )
        assert result == "ok"
        assert len(stub.calls) == 1
        assert stub.calls[0][:4] == ["release", "create", "rules-2026-10-05", ZIP]
        assert stub.calls[0][-1] == "--latest"

    def test_upload_failure_skips_edit(self):
        stub = GhStub(fail_when=lambda cmd: cmd[1] == "upload")
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result is None
        assert [c[1] for c in stub.calls] == ["upload"]

    def test_edit_failure_propagates(self):
        stub = GhStub(fail_when=lambda cmd: cmd[1] == "edit")
        result = run_with_stub(
            stub,
            lambda: release_handler.publish_release("rules-2026-10-05", ZIP, "title", NOTES, True),
        )
        assert result is None
        assert [c[1] for c in stub.calls] == ["upload", "edit"]
