"""tools/privacy_check.py: the site's hardware identifiers are private without a list to keep."""
import importlib.util
import os
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("privacy_check", os.path.join(REPO, "tools", "privacy_check.py"))
privacy_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy_check)


@pytest.fixture
def a_site(tmp_path, monkeypatch):
    """A site with one registered board and its survey facts, as the environment points at it."""
    (tmp_path / "facts").mkdir()
    (tmp_path / "registry.yaml").write_text(textwrap.dedent("""\
        version: 1
        devices:
          head1:
            match: {usb_serial: 1A1B1C1D1E1F2021}
            identity: {node_id: '0x2f3f4f5f'}
            reach: {ble: {addr: 'AA:BB:CC:11:22:33'}}
        """))
    (tmp_path / "facts" / "head1.yaml").write_text(textwrap.dedent("""\
        probes:
          zephyrtastic: {node_id: '0x6a7b8c9d', class: 12}
        history:
          meshtastic:
            - {node_id: '0x00000001'}
        """))
    monkeypatch.setenv("RXRBENCH_SITE", str(tmp_path))
    monkeypatch.delenv("DEVREG_REGISTRY", raising=False)
    monkeypatch.delenv("RXRBENCH_FACTS", raising=False)
    return tmp_path


def hits(text, pats):
    return [h.split(" (pattern ")[1] for h in privacy_check.scan("f", text.encode(), pats)]


def test_the_sites_hardware_ids_are_patterns(a_site):
    pats = privacy_check.hardware_patterns()
    srcs = {src for _, src in pats}
    assert "site hardware id 1A1B1C1D1E1F2021" in srcs
    assert "site hardware id AA:BB:CC:11:22:33" in srcs
    assert "site hardware id 0x2f3f4f5f" in srcs and "site hardware id 0x6a7b8c9d" in srcs
    assert not any("0x00000001" in s for s in srcs), "a placeholder is not hardware"


def test_every_spelling_of_a_node_id_is_a_hit(a_site):
    pats = privacy_check.hardware_patterns()
    assert hits("node: 0x6a7b8c9d", pats)
    assert hits("ids = {'x': '6A7B8C9D'}", pats), "bare, upper-case"
    assert hits('"node_id": "0x2f3f4f5f"', pats)
    assert not hits("offset 0x6a7b8c9d00", pats), "part of a longer number is not the id"
    assert not hits("node: 0x0a0b0c0d", pats), "an example id"


def test_a_chip_id_in_lower_case_is_a_hit(a_site):
    pats = privacy_check.hardware_patterns()
    assert hits("serial 1a1b1c1d1e1f2021 seen", pats)
    assert hits("/dev/serial/by-id/usb-Seeed_XIAO_1A1B1C1D1E1F2021-if00", pats), \
        "inside a by-id name, after an underscore"
    assert hits("aabbcc112233 is the MAC as a facts key", pats)


@pytest.mark.parametrize("token", ["0A0B0C0D0E0F1011", "0123456789abcdef", "FEDCBA9876543210",
                                   "AAAABBBBCCCCDDDD", "02:00:00:00:00:10", "0A1B2C3D4E5F"])
def test_a_patterned_id_is_plainly_made_up(token):
    assert privacy_check.made_up(token)


def test_an_id_from_no_site_here_is_refused_unless_reviewed():
    """Another bench's board ids are in no site file on this workstation (they reached tests
    twice): a chip-id or MAC shaped token must be patterned or on the reviewed list."""
    line = b"usb-Board_E7C21F9A04B3D856-if00 and aa:17:3c:9e:20:5b and serial 5F31C0A2E99B7D14"  # privacy: allow
    hits = privacy_check.id_hits("t.py", line, set())
    assert len(hits) == 3, hits
    listed = {"E7C21F9A04B3D856", "AA173C9E205B", "5F31C0A2E99B7D14"}  # privacy: allow
    assert privacy_check.id_hits("t.py", line, listed) == []


def test_a_guid_group_and_a_sha_are_not_ids():
    line = b'"0FC63DAF-8483-4772-8E79-3D69D8477DE4" sha256 ' + b"ab" * 32
    assert privacy_check.id_hits("t.py", line, set()) == []
