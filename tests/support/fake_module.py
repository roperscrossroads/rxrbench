"""An AnsibleModule stand-in for driving a module's main() in a test: the params as the
argument spec would fill them (defaults, `path` expanded, `bool` coerced, required checked),
and exit_json / fail_json as exceptions that carry what the module returned."""
import os


class Exit(Exception):
    def __init__(self, **kw):
        super().__init__(kw)
        self.result = kw


class Fail(Exception):
    def __init__(self, **kw):
        super().__init__(kw.get("msg"))
        self.result = kw


class FakeModule:
    def __init__(self, params, check_mode, argument_spec, supports_check_mode=False, **_):
        self.check_mode = check_mode
        missing = [k for k, s in argument_spec.items() if s.get("required") and k not in params]
        if missing:
            raise Fail(msg="missing required arguments: " + ", ".join(missing))
        unknown = sorted(set(params) - set(argument_spec))
        if unknown:
            raise Fail(msg="unsupported parameters: " + ", ".join(unknown))
        self.params = {}
        for k, s in argument_spec.items():
            v = params.get(k, s.get("default"))
            if v is not None and s.get("type") == "path":
                v = os.path.expanduser(v)
            if v is not None and s.get("type") == "bool":
                v = bool(v)
            self.params[k] = v

    def exit_json(self, **kw):
        kw.setdefault("changed", False)
        raise Exit(**kw)

    def fail_json(self, **kw):
        kw.setdefault("failed", True)
        raise Fail(**kw)


def run(mod, monkeypatch, params, check_mode=False):
    """`mod.main()` with its AnsibleModule replaced: ('exit' | 'fail', what it returned)."""
    monkeypatch.setattr(mod, "AnsibleModule", lambda **kw: FakeModule(params, check_mode, **kw))
    try:
        mod.main()
    except Exit as e:
        return "exit", e.result
    except Fail as e:
        return "fail", e.result
    raise AssertionError("main() returned without exit_json or fail_json")
