"""The one gate every write to a device passes: the layout's go-ahead, checked where the write
happens, not only in the play that calls it.

A play asserts `bench_apply_allowed` (playbooks/tasks/gate.yml) and hands it to each writing
module as `apply_allowed`; the module refuses to write without it. A module run outside a
layout's apply (ad hoc, from another play) must therefore say `apply_allowed: true` itself.
Check mode passes: a plan writes nothing and needs no go-ahead.
"""

ARGS = {"apply_allowed": {"type": "bool", "default": False}}


def check(module):
    """Fail the module before it touches a device, unless this is a plan or the write is allowed."""
    if module.check_mode or module.params.get("apply_allowed"):
        return
    module.fail_json(msg="writing is not allowed: the layout does not set bench_apply_allowed "
                         "(a plan, --check, needs no go-ahead; a module run outside a layout's "
                         "apply must say apply_allowed: true)")
