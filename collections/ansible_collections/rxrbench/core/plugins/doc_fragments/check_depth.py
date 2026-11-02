"""The check_depth option every reading module shares (docs/ARCHITECTURE.md#checking-depth)."""


class ModuleDocFragment:  # pylint: disable=too-few-public-methods
    DOCUMENTATION = r"""
options:
  check_depth:
    description:
      - How much a read takes before it decides (C(bench_check_depth)). C(full), the default,
        reads everything the decision could depend on. C(spot) reads what the design of that
        read says is enough for its decision (docs/ARCHITECTURE.md#checking-depth).
      - Some moments always read in full, whatever is asked (a first flash, a family switch,
        no full record yet); the result's C(checked) says which depth was used and why.
      - In a plan at C(spot), a flasher that offers it takes what the board runs from its last
        full read on the bench host, while the plug watcher has seen nothing on its port since
        ("Identity from a record"); an apply always asks the board. C(identity_checked) says
        which it was and why.
    type: str
    choices: [full, spot]
    default: full
"""
