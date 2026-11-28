"""The test library: tests that live once in a site and run on any layout that binds their
roles to its boards (docs/ARCHITECTURE.md#runs-tests-and-campaigns).

A test is `<site>/tests/<name>.yml`: `needs.roles` says what each role's board must be, and
its steps are scenario steps whose `board` is `$<role>`. A layout binds roles to boards in
`bench_test_bindings`; only bound tests run, and the tool never picks a board for a role.
Runs on the controller; reads files and the boards' variables, never a device.
"""
import os

import yaml

#: What can carry a command to a board, as a role's `via` names it: its console (a broker),
#: ssh, or its family's own step tasks (a family with neither).
VIAS = ("console", "ssh", "steps")
NEEDS = ("families", "via", "features")
ROLE = "$"


class LibraryError(Exception):
    pass


def library_dir(layout_dir):
    """`<site>/tests` of the site a layout lives in (`<site>/layouts/<name>`)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(layout_dir))), "tests")


def _role_of(board):
    return board[len(ROLE):] if isinstance(board, str) and board.startswith(ROLE) else None


def check_test(name, test):
    """Problems with one library test's shape."""
    out = []
    if not isinstance(test, dict):
        return [f"{name}: a test is a mapping with needs and steps"]
    roles = (test.get("needs") or {}).get("roles")
    if not isinstance(roles, dict) or not roles:
        out.append(f"{name}: needs.roles names no role")
        roles = {}
    for role, need in roles.items():
        need = need or {}
        if not isinstance(need, dict):
            out.append(f"{name}: role {role}: its needs are a mapping ({', '.join(NEEDS)})")
            continue
        for k, v in need.items():
            if k not in NEEDS:
                out.append(f"{name}: role {role}: {k!r} is not a need ({', '.join(NEEDS)})")
            elif not isinstance(v, list) or not v:
                out.append(f"{name}: role {role}: {k} is a list")
        bad = [v for v in need.get("via") or [] if v not in VIAS]
        if bad:
            out.append(f"{name}: role {role}: via {bad}: one of {', '.join(VIAS)}")
    steps = test.get("steps")
    if not isinstance(steps, list) or not steps:
        out.append(f"{name}: no steps")
        steps = []
    used = set()
    for i, step in enumerate(steps):
        role = _role_of((step or {}).get("board"))
        if role is None:
            out.append(f"{name}: step {i + 1} names board {(step or {}).get('board')!r}: a library "
                       f"test names roles ({ROLE}a), a layout binds them to boards")
        elif role not in roles:
            out.append(f"{name}: step {i + 1} names role {role}, which needs.roles does not declare")
        else:
            used.add(role)
        if not (step or {}).get("cmd") and not (step or {}).get("cmds"):
            out.append(f"{name}: step {i + 1} has no cmd (or cmds)")
    for role in sorted(set(roles) - used):
        out.append(f"{name}: role {role} is declared but no step names it")
    return out


def load(directory):
    """({name: test}, problems) from every `*.yml` in `directory` (none: an empty library)."""
    tests, problems = {}, []
    if not os.path.isdir(directory):
        return tests, problems
    for f in sorted(os.listdir(directory)):
        if not f.endswith((".yml", ".yaml")):
            continue
        name = f.rsplit(".", 1)[0]
        try:
            with open(os.path.join(directory, f)) as fh:
                test = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            problems.append(f"{name}: not YAML ({e})")
            continue
        problems += check_test(name, test)
        tests[name] = test
    return tests, problems


def board_view(hostvars, groups):
    """What a role's needs are compared with: {family, via, features} of one board, from its
    variables (bench_family, bench_via, bench_step_tasks) and the groups it is in."""
    via = hostvars.get("bench_via")
    if via is None and hostvars.get("bench_step_tasks") is not None:
        via = "steps"
    features = sorted(g[len("feature_"):] for g in groups if g.startswith("feature_"))
    return {"family": hostvars.get("bench_family"), "via": via, "features": features}


def misses(need, view):
    """What a board misses of a role's needs: [] when it fits."""
    need = need or {}
    out = []
    if view.get("via") is None:
        out.append("nothing carries a command to it (no console, no ssh, no step tasks)")
    if need.get("families") and view.get("family") not in need["families"]:
        out.append(f"its family is {view.get('family')}, the role needs {' or '.join(need['families'])}")
    if need.get("via") and view.get("via") not in need["via"]:
        out.append(f"commands reach it by {view.get('via')}, the role needs {' or '.join(need['via'])}")
    lacking = [f for f in need.get("features") or [] if f not in view.get("features", [])]
    if lacking:
        out.append(f"its family gives it no feature {', '.join(lacking)}")
    return out


def bind(name, test, binding, views):
    """(scenario, problems) for one bound test: its steps with each role's board, or why not.
    `views` is {board: board_view} for the layout's boards."""
    roles = test["needs"]["roles"]
    binding = binding or {}
    problems = []
    for role in sorted(set(binding) - set(roles)):
        problems.append(f"{name}: binds role {role}, which the test does not have "
                        f"(roles: {', '.join(sorted(roles))})")
    for role in sorted(roles):
        board = binding.get(role)
        if board is None:
            fit = sorted(b for b, v in views.items() if not misses(roles[role], v))
            problems.append(f"{name}: no board is bound to role {role}; boards that fit it: "
                            f"{', '.join(fit) or 'none in this layout'} "
                            f"(bench_test_bindings: {{{name}: {{{role}: <board>}}}})")
        elif board not in views:
            problems.append(f"{name}: role {role} is bound to {board}, which is not a board of "
                            "this layout")
        else:
            why = misses(roles[role], views[board])
            if why:
                problems.append(f"{name}: role {role} is bound to {board}, which does not fit: "
                                + "; ".join(why))
    seen = {}
    for role, board in sorted(binding.items()):
        if board in seen:
            problems.append(f"{name}: roles {seen[board]} and {role} are bound to one board, {board}")
        seen.setdefault(board, role)
    if problems:
        return None, problems
    steps = [{**s, "board": binding[_role_of(s["board"])]} for s in test["steps"]]
    return {"name": name, "steps": steps, "test": name, "bindings": dict(binding)}, []


def bound_scenarios(tests, load_problems, bindings, views, inline_names=()):
    """(scenarios, problems): every test the layout binds, as scenarios. Any problem refuses the
    lot (nothing half runs): a binding's, a bound test's shape, a name an inline scenario has."""
    bindings = bindings or {}
    if not isinstance(bindings, dict):
        return [], [f"bench_test_bindings is {bindings!r}: a mapping {{<test>: {{<role>: <board>}}}}"]
    out, problems = [], []
    for name in sorted(bindings):
        if name not in tests:
            problems.append(f"{name}: bound, but the library has no such test (it has: "
                            f"{', '.join(sorted(tests)) or 'none'})")
            continue
        theirs = [p for p in load_problems if p.startswith(name + ":")]
        if theirs:
            problems += theirs
            continue
        if name in inline_names:
            problems.append(f"{name}: an inline scenario of this layout has the same name")
            continue
        scenario, why = bind(name, tests[name], bindings[name], views)
        problems += why
        if scenario:
            out.append(scenario)
    return out, problems
