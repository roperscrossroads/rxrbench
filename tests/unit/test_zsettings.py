"""The zephyr_settings helper against a fake settings shell that shows at most 256 bytes of a
value, as Zephyr's does (SETTINGS_MAX_VAL_LEN), and a broker that redacts a named secret. A value
the shell cannot show whole is refused, never half-checked."""
import pytest

from ansible_collections.rxrbench.zephyr.plugins.module_utils import zsettings


class Shell:
    def __init__(self):
        self.store, self.scrubbed, self.writes = {}, [], 0

    def exec(self, cmd, deadline=8.0, secret=None):
        words = cmd.split()
        if words[0] != "settings":
            return f"{words[0]}: command not found\n"
        if words[1] == "write":
            self.writes += 1
            self.store[words[3]] = words[4]
            return ""
        v = self.store.get(words[3])
        if v is None:
            return "Setting not found\n"
        out = v[:256] + ("\n(The output has been truncated)" if len(v) > 256 else "")
        if secret and secret in out:
            self.scrubbed.append(secret)
            out = out.replace(secret, zsettings.REDACTED)
        return out + "\n"


CERT = "MIIB" + "x" * 500


def test_a_value_longer_than_the_shell_reads_back_is_refused_before_it_is_written():
    b = Shell()
    with pytest.raises(zsettings.SettingsError, match="504 bytes.*at most 256.*nothing written"):
        zsettings.write(b, "tls/cert", CERT, secret=True)
    assert b.writes == 0 and zsettings.too_long("x", "y" * 256) is None


def test_a_read_the_shell_cut_short_is_an_error_and_its_shown_part_is_still_scrubbed():
    b = Shell()
    b.store["tls/cert"] = CERT                       # put there some other way
    with pytest.raises(zsettings.SettingsError, match="cannot be read"):
        zsettings.read(b, "tls/cert", secret=CERT)
    assert b.scrubbed == [CERT[:256]]


def test_values_the_shell_shows_whole_are_written_and_read():
    b = Shell()
    zsettings.write(b, "tls/cert", CERT[:256], secret=True)
    assert zsettings.read(b, "tls/cert", secret=CERT[:256]) == CERT[:256]
    zsettings.write(b, "net/name", "pico2w")
    assert zsettings.read(b, "net/name") == "pico2w" and zsettings.read(b, "no/such") is None


def test_a_value_that_reads_back_wrong_is_an_error():
    class Lossy(Shell):
        def exec(self, cmd, deadline=8.0, secret=None):
            if cmd.split()[1] == "write":
                cmd = cmd[:-1] + "?"
            return super().exec(cmd, deadline, secret)
    with pytest.raises(zsettings.SettingsError, match="reads back as something else"):
        zsettings.write(Lossy(), "app/psk", "0123456789abcdef", secret=True)


def test_a_value_too_long_to_read_differs_from_any_declared_one_and_is_overwritten():
    b = Shell()
    b.store["tls/cert"] = CERT                       # an old, long value
    token = "t" * 128
    assert zsettings.differs(b, "tls/cert", token, secret=token)
    zsettings.write(b, "tls/cert", token, secret=True)
    assert zsettings.read(b, "tls/cert", secret=token) == token
    with pytest.raises(zsettings.Truncated):         # a read that keeps the value still fails
        b.store["x"] = CERT
        zsettings.read(b, "x")


class DigestShell(Shell):
    """A board whose firmware prints a setting's digest (zapp's `zapp digest <name>`)."""

    def __init__(self):
        super().__init__()
        self.reads = []

    def exec(self, cmd, deadline=8.0, secret=None):
        words = cmd.split()
        if words[:2] == ["zapp", "digest"]:
            v = self.store.get(words[2])
            if v is None:
                return f"digest {words[2]} none\n"
            n, h = zsettings.digest_of(v)
            return f"digest {words[2]} {n} {h}\n"
        if words[1] == "read":
            self.reads.append(words[3])
        return super().exec(cmd, deadline, secret)


def test_a_long_value_is_written_and_checked_by_the_firmwares_digest():
    """A 504-byte certificate, refused through the shell alone, is kept when the
    firmware prints a setting's digest; and a secret is compared without being printed."""
    b = DigestShell()
    zsettings.write(b, "tls/cert", CERT, secret=True, digest_command="zapp digest")
    assert b.store["tls/cert"] == CERT and b.reads == [], "never read back through the shell"
    assert not zsettings.differs(b, "tls/cert", CERT, secret=CERT, digest_command="zapp digest")
    assert zsettings.differs(b, "tls/cert", CERT + "y", secret=CERT, digest_command="zapp digest")
    assert zsettings.differs(b, "no/such", "x", secret="x", digest_command="zapp digest")
    assert zsettings.too_long("tls/cert", CERT, "zapp digest") is None


def test_a_firmware_without_the_digest_command_is_an_error_not_a_match():
    b = Shell()
    b.store["tls/cert"] = "x"
    with pytest.raises(zsettings.SettingsError, match="printed no digest line"):
        zsettings.differs(b, "tls/cert", "x", secret="x", digest_command="zapp digest")


def test_a_wifi_key_is_a_declared_secret_of_its_own_kind():
    from ansible_collections.rxrbench.zephyr.plugins.plugin_utils.families import zephyr_settings
    fam = zephyr_settings.Family()
    hv = {"zephyr": {"device_key": "app/key", "secrets": {"psk": "app/psk", "wifi_psk": "wifi/psk"}}}
    kinds = {s["kind"]: s["origin"] for s in fam.board_secrets(hv)}
    assert kinds == {"device_key": "device", "psk": "declared", "wifi_psk": "declared"}
