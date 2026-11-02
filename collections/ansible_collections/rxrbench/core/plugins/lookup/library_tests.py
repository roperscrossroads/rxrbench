"""Lookup: the library tests a layout binds, as scenarios (module_utils/testlib.py)."""

DOCUMENTATION = r"""
name: library_tests
short_description: The library tests a layout binds, as scenarios
description:
  - Reads the test library of the site the layout lives in (C(<site>/tests/<name>.yml)) and the
    layout's C(bench_test_bindings), and returns each bound test as a scenario
    (C({name, steps, test, bindings})) with its roles replaced by the boards bound to them
    (docs/ARCHITECTURE.md#runs-tests-and-campaigns).
  - Any problem refuses the lot, before anything runs - a binding that names no board for a
    role (the error lists the boards that would fit), a bound board that does not meet its
    role's needs, a test the library does not have, a test whose own shape is wrong, or a name
    an inline scenario (C(bench_scenarios)) already has.
  - Returns an empty list when the layout binds nothing.
options:
  _terms:
    description: Not used.
    type: list
    elements: str
author:
  - Adam Roper (@roperscrossroads)
"""

EXAMPLES = r"""
- name: The library tests the layout binds, as scenarios
  ansible.builtin.set_fact:
    scenarios_bound: "{{ lookup('rxrbench.core.library_tests') }}"
"""

RETURN = r"""
_list:
  description: One element, the list of bound tests as scenarios.
  type: list
"""

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

from ansible_collections.rxrbench.core.plugins.module_utils import testlib


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        variables = variables or {}
        bindings = self._templar.template(variables.get("bench_test_bindings") or {})
        if not bindings:
            return [[]]
        layout_dir = variables.get("bench_layout_dir")
        if not layout_dir:
            raise AnsibleError("bench_test_bindings needs the layout's directory (bench_layout_dir, "
                               "which the rxrbench.core.devreg inventory sets)")
        groups = variables.get("groups") or {}
        hostvars = variables.get("hostvars") or {}
        views = {}
        for board in groups.get("boards") or []:
            hv = hostvars[board]
            member = [g for g, hosts in groups.items() if board in hosts]
            views[board] = testlib.board_view(
                {k: self._templar.template(hv.get(k)) for k in ("bench_family", "bench_via", "bench_step_tasks")},
                member)
        inline = [s.get("name") for s in self._templar.template(variables.get("bench_scenarios") or [])]
        tests, problems = testlib.load(testlib.library_dir(layout_dir))
        scenarios, why = testlib.bound_scenarios(tests, problems, bindings, views, inline)
        if why:
            raise AnsibleError("the layout's library tests are refused, nothing ran:\n  "
                               + "\n  ".join(why))
        return [scenarios]
