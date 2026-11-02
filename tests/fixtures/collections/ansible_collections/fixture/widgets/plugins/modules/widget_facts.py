#!/usr/bin/python
"""What a widget runs, in the shape the tool compares (a plugin's module using core's code)."""
from ansible.module_utils.basic import AnsibleModule

from ansible_collections.rxrbench.core.plugins.module_utils import identity

DOCUMENTATION = r"""
module: widget_facts
short_description: A widget's identity
description: Returns the identity of a widget from the build id given.
options:
  build:
    description: The build id.
    type: str
    required: true
author: rxrbench tests
"""


def main():
    module = AnsibleModule(argument_spec={"build": {"type": "str", "required": True}},
                           supports_check_mode=True)
    module.exit_json(changed=False, identity=identity.make("widget", build=module.params["build"]))


if __name__ == "__main__":
    main()
