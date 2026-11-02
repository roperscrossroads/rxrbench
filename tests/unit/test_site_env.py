"""The repo's .env, read the way `just` reads it."""
import os

from ansible_collections.rxrbench.core.plugins.module_utils import site


def test_env_values_lose_trailing_comments_and_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "A_PLAIN=https://example.test/topic   # where plug notices go\n"
        "A_TIGHT=value#not-a-comment\n"
        "A_DQ=\"quoted # kept\"   # comment\n"
        "A_SQ='single'\n"
        "A_EMPTY=   # off\n")
    for k in ("A_PLAIN", "A_TIGHT", "A_DQ", "A_SQ", "A_EMPTY"):
        monkeypatch.delenv(k, raising=False)
    site.load_env(str(env))
    assert os.environ["A_PLAIN"] == "https://example.test/topic"
    assert os.environ["A_TIGHT"] == "value#not-a-comment"
    assert os.environ["A_DQ"] == "quoted # kept"
    assert os.environ["A_SQ"] == "single"
    assert os.environ["A_EMPTY"] == ""


def test_env_does_not_override_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A_SET=from-file\n")
    monkeypatch.setenv("A_SET", "from-env")
    site.load_env(str(env))
    assert os.environ["A_SET"] == "from-env"
