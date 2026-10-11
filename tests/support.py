class GhStub:
    def __init__(self, fail_when=None, responses=None, asset_count="3",
                 release_exists=True):
        self.calls = []
        self.fail_when = fail_when or (lambda cmd: False)
        self.responses = responses or {}
        self.asset_count = asset_count
        self.release_exists = release_exists

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.fail_when(cmd):
            return None
        key = (cmd[0], cmd[1]) if len(cmd) > 1 else (cmd[0],)
        if key in self.responses:
            return self.responses[key]
        if key == ("release", "list"):
            return "[]"
        if key == ("release", "view"):
            if "assets" in cmd:
                return self.asset_count
            return '{"tagName": "rules-x"}' if self.release_exists else None
        return "ok"

    def actions(self):
        return [tuple(call[:2]) for call in self.calls]

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def release_actions(self):
        return [call[1] for call in self.calls if call[0] == "release" and len(call) > 1]
