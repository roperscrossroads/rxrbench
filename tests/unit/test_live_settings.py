"""A test run reaches no live service through the site's .env (tests/conftest.py, LIVE_SETTINGS):
no plug notice, no post to a coordination bus, no facts commit, and no Ansible callback the
site enables for its own runs, such as one that publishes every run to a broker. They hold in
the test process, in a tool that loads .env again, and in a child process."""
import json
import os
import subprocess
import sys

from ansible_collections.rxrbench.core.plugins.module_utils import site
from tests.conftest import LIVE_SETTINGS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_the_live_settings_hold_in_the_test_process_after_env_is_loaded_again():
    site.load_env()
    assert {k: os.environ.get(k) for k in LIVE_SETTINGS} == LIVE_SETTINGS


def test_a_child_that_loads_env_itself_gets_them_too():
    code = ("import json, os, sys; sys.path.insert(0, 'collections'); "
            "from ansible_collections.rxrbench.core.plugins.module_utils import site; "
            "site.load_env(); print(json.dumps({k: os.environ.get(k) for k in %r}))" % sorted(LIVE_SETTINGS))
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == LIVE_SETTINGS


def test_a_play_runs_with_only_the_repositorys_own_callbacks():
    assert LIVE_SETTINGS["ANSIBLE_CALLBACKS_ENABLED"] == "rxrbench.core.timing", \
        "the callbacks ansible.cfg enables: empty would stop ansible-playbook outright"
    assert os.listdir(LIVE_SETTINGS["ANSIBLE_CALLBACK_PLUGINS"]) == [], "no site's callback to find"
    assert all(v == "" for k, v in LIVE_SETTINGS.items() if not k.startswith("ANSIBLE_"))
