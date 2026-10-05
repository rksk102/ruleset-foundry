from pathlib import Path

import main
import pytest

SOURCE_URL = "https://github.com/o/r/raw/m/x.txt"


def load_sources(monkeypatch, work_dir, content):
    target = work_dir / "sources.urls"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(main, "SOURCES_FILE", str(target))
    monkeypatch.setattr(main, "RULESETS_DIR", work_dir / "rulesets")
    return main.parse_sources()


def single_task(monkeypatch, work_dir, content):
    return load_sources(monkeypatch, work_dir, content)[0]


class TestMarkerWhitelistRejected:
    def test_policy_parent_traversal_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir,
                         f"[policy:../../../etc]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_policy_absolute_path_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir, f"[policy:/tmp/evil]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_policy_drive_path_rejected(self, monkeypatch, work_dir):
        for marker in ("[policy:C:/Windows/Temp]", "[policy:C:Windows]", "[policy:u:x]"):
            with pytest.raises(SystemExit) as exc:
                load_sources(monkeypatch, work_dir, f"{marker}\n{SOURCE_URL}\n")
            assert exc.value.code == 1, marker

    def test_policy_leading_dot_and_bare_dotdot_rejected(self, monkeypatch, work_dir):
        for marker in ("[policy:.hidden]", "[policy:..]", "[policy:block/../direct]"):
            with pytest.raises(SystemExit):
                load_sources(monkeypatch, work_dir, f"{marker}\n{SOURCE_URL}\n")

    def test_type_parent_traversal_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit) as exc:
            load_sources(monkeypatch, work_dir, f"[type:../../..]\n{SOURCE_URL}\n")
        assert exc.value.code == 1

    def test_type_drive_path_rejected(self, monkeypatch, work_dir):
        with pytest.raises(SystemExit):
            load_sources(monkeypatch, work_dir, f"[type:C:/win]\n{SOURCE_URL}\n")

    def test_valid_markers_still_accepted(self, monkeypatch, work_dir):
        tasks = load_sources(monkeypatch, work_dir, "\n".join([
            "[policy:reject]", "[type:ipcidr]", SOURCE_URL,
            "[policy:direct]", "[type:domain]", SOURCE_URL,
            "[policy:custom-list]", SOURCE_URL,
        ]) + "\n")
        assert [t["policy"] for t in tasks] == ["block", "direct", "custom-list"]
        assert [t["type"] for t in tasks] == ["ipcidr", "domain", "domain"]


class TestUrlDerivedComponents:
    def test_userinfo_host_cannot_become_drive(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://u:p@evil.com:8443/x.txt\n")
        owner, name, rel, abs_path = main.build_filepath(task)
        assert (owner, name) == ("u_p_evil.com_8443", "x.txt")
        assert rel.as_posix() == "policy/domain/u_p_evil.com_8443/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())
        assert main.ensure_within_rulesets(abs_path) == Path(abs_path).resolve()

    def test_dotdot_path_segments_stay_inside(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://evil.com/a/../../x.txt\n")
        _owner, _name, rel, abs_path = main.build_filepath(task)
        assert rel.as_posix() == "policy/domain/evil.com/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())

    def test_dotdot_filename_falls_back(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "https://evil.com/dir/..\n")
        _owner, name, rel, abs_path = main.build_filepath(task)
        assert name == "rules.txt"
        assert rel.as_posix() == "policy/domain/evil.com/rules.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())

    def test_collision_slug_is_sanitised(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir,
                           "https://github.com/u:x/repo/raw/m/x.txt\n")
        plan = {str(main.Path("policy/domain/u_x/x.txt")): ["u:x__repo"]}
        owner, _name, rel, abs_path = main.build_filepath(task, plan, {})
        assert owner == "u_x__repo"
        assert rel.as_posix() == "policy/domain/u_x__repo/x.txt"
        assert Path(abs_path).resolve().is_relative_to((work_dir / "rulesets").resolve())


class TestNormalPaths:
    def test_normal_source_lands_where_expected(self, monkeypatch, work_dir):
        task = single_task(monkeypatch, work_dir, "\n".join([
            "[policy:block]", "[type:domain]",
            "https://github.com/Loyalsoldier/clash-rules/raw/r/reject.txt",
        ]) + "\n")
        owner, name, rel, abs_path = main.build_filepath(task)
        assert (owner, name) == ("Loyalsoldier", "reject.txt")
        assert rel.as_posix() == "block/domain/Loyalsoldier/reject.txt"
        assert abs_path == main.RULESETS_DIR / rel

    def test_real_sources_file_unchanged(self, monkeypatch, work_dir):
        monkeypatch.setattr(main, "SOURCES_FILE", "sources.urls")
        tasks = main.parse_sources()
        assert len(tasks) > 5
        for task in tasks:
            assert task["policy"] in ("block", "direct", "policy")
            assert task["type"] in ("domain", "ipcidr")
            rel = main._base_rel_path(task)
            assert ".." not in rel.parts


class TestLandingAssertion:
    def test_out_of_tree_path_rejected(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        with pytest.raises(SystemExit) as exc:
            main.ensure_within_rulesets(work_dir / "escaped" / "x.txt")
        assert exc.value.code == 1

    def test_absolute_outside_paths_rejected(self, monkeypatch, work_dir):
        monkeypatch.setattr(main, "RULESETS_DIR", work_dir / "rulesets")
        for target in ("C:/Windows/Temp/x.txt", "U:/p@evil.com:8443/x.txt",
                       "\\tmp\\evil\\x.txt", "../outside/x.txt"):
            with pytest.raises(SystemExit):
                main.ensure_within_rulesets(target)

    def test_inside_rulesets_allowed(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        target = rulesets / "policy" / "domain" / "x.txt"
        assert main.ensure_within_rulesets(target) == target.resolve()
        assert main.ensure_within_rulesets(rulesets) == rulesets.resolve()

    def test_process_group_refuses_to_write_outside(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        escaped = work_dir / "escaped" / "x.txt"
        group = {
            "path": str(escaped),
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(0, {"url": SOURCE_URL})],
            "sources": [SOURCE_URL],
        }
        with pytest.raises(SystemExit) as exc:
            main.process_group(group, {0: b"example.com\n"})
        assert exc.value.code == 1
        assert not escaped.exists()

    def test_process_group_still_writes_inside(self, monkeypatch, work_dir):
        rulesets = work_dir / "rulesets"
        rulesets.mkdir(parents=True)
        monkeypatch.setattr(main, "RULESETS_DIR", rulesets)
        target = rulesets / "block" / "domain" / "o" / "x.txt"
        group = {
            "path": str(target),
            "policy": "block",
            "type": "domain",
            "domain_kind": "exact",
            "members": [(0, {"url": SOURCE_URL})],
            "sources": [SOURCE_URL],
        }
        count, errors = main.process_group(group, {0: b"example.com\nsub.example.org\n"})
        assert count == 2
        assert errors == {"download": [], "parse": []}
        assert target.read_text(encoding="utf-8").splitlines() == [
            f"# Source: {SOURCE_URL}", "example.com", "sub.example.org"]
