"""The tool's root comes from RXRBENCH_ROOT when set, else from the checkout core sits in
(docs/ARCHITECTURE.md#core-portability: a core installed apart from the checkout)."""
import importlib
import os

import ansible_collections.rxrbench.core.plugins.module_utils.site as site


def _reload(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RXRBENCH_ROOT", raising=False)
    else:
        monkeypatch.setenv("RXRBENCH_ROOT", value)
    return importlib.reload(site)


def test_the_root_is_the_checkout_by_default(monkeypatch):
    s = _reload(monkeypatch, None)
    assert os.path.isfile(os.path.join(s.REPO, "justfile"))
    assert s.EXAMPLE == os.path.join(s.REPO, "site.example")


def test_rxrbench_root_moves_the_root_and_what_hangs_off_it(monkeypatch, tmp_path):
    s = _reload(monkeypatch, str(tmp_path))
    try:
        assert s.REPO == str(tmp_path)
        assert s.EXAMPLE == os.path.join(str(tmp_path), "site.example")
        monkeypatch.setenv("RXRBENCH_SITE", "mysite")
        assert s.site_dir() == os.path.join(str(tmp_path), "mysite")
    finally:
        _reload(monkeypatch, None)
