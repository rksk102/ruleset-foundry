import json


class GhStub:
    def __init__(self, fail_when=None, responses=None, asset_names=None, release_exists=True, latest_tag=None):
        self.calls = []
        self.fail_when = fail_when or (lambda cmd: False)
        self.responses = responses or {}
        self.asset_names = asset_names
        self.release_exists = release_exists
        self.latest_tag = latest_tag

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.fail_when(cmd):
            return None
        if cmd[0] == "release" and len(cmd) > 1 and cmd[1] in ("create", "upload"):
            self.release_exists = True
            self.asset_names = None  # 发布成功后恢复为完整集合推导
        key = (cmd[0], cmd[1]) if len(cmd) > 1 else (cmd[0],)
        if key in self.responses:
            return self.responses[key]
        if key == ("release", "list"):
            if self.latest_tag:
                return json.dumps([{"tagName": self.latest_tag, "createdAt": "2026-01-01T00:00:00Z"}])
            return "[]"
        if key == ("release", "view"):
            if "assets" in cmd:
                if not self.release_exists:
                    return None
                if self.asset_names is not None:
                    return self.asset_names
                date = cmd[2].removeprefix("rules-")
                return f"merged-rules-{date}.zip\nSHA256SUMS"
            return '{"tagName": "rules-x"}' if self.release_exists else None
        return "ok"

    def actions(self):
        return [tuple(call[:2]) for call in self.calls]

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def release_actions(self):
        return [call[1] for call in self.calls if call[0] == "release" and len(call) > 1]
