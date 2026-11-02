"""tools/stage_mcuboot.py: a Zephyr application signed for MCUboot, staged for an update over
MCUmgr. What it is comes from the image and its build: the hash MCUboot carries (what MCUmgr
reports), the header's version, the board, and the MCUmgr transports the build has."""
import hashlib
import json
import os
import struct
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "tools", "stage_mcuboot.py")


def signed(version=(1, 1, 0, 0), body=b"app" * 50):
    """A made-up MCUboot image: header (magic, sizes, version), the body, one SHA-256 TLV."""
    hdr = struct.pack("<IIHHII", 0x96F3B83D, 0, 32, 0, len(body), 0)
    hdr += struct.pack("<BBHI", *version) + b"\0" * 4
    digest = hashlib.sha256(hdr + body).digest()
    tlv = struct.pack("<HH", 0x6907, 4 + 4 + 32) + struct.pack("<BxH", 0x10, 32) + digest
    return hdr + body + tlv, digest.hex()


def run(tmp_path, image, config, *extra):
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "python"), TOOL, str(image), "--config",
                        str(config), "--name", "app", "--board-tag", "nucleo-f767zi",
                        "--store", str(tmp_path / "store"), *extra],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def test_an_image_is_staged_by_what_it_carries(tmp_path):
    data, digest = signed((1, 1, 0, 0))
    (tmp_path / "zephyr.signed.bin").write_bytes(data)
    (tmp_path / ".config").write_text('CONFIG_BOARD="nucleo_f767zi"\nCONFIG_MCUMGR_TRANSPORT_SHELL=y\n'
                                      "CONFIG_MCUMGR_TRANSPORT_UDP=y\n# CONFIG_MCUMGR_TRANSPORT_BT is not set\n")
    rc, out = run(tmp_path, tmp_path / "zephyr.signed.bin", tmp_path / ".config")
    assert rc == 0, out
    d = tmp_path / "store" / "zephyr_mcumgr" / "app"
    m = json.loads((d / "manifest.json").read_text())
    assert m == {"family": "zephyr_mcumgr", "format": "mcuboot-bin", "file": "app.signed.bin",
                 "board_tag": "nucleo-f767zi", "target": "nucleo_f767zi", "hash": digest,
                 "version": "1.1.0", "mcumgr": ["serial", "udp"]}
    assert (d / "app.signed.bin").read_bytes() == data


def test_the_build_counter_shows_when_set_and_a_plain_image_is_refused(tmp_path):
    data, _ = signed((0, 4, 46, 45))
    (tmp_path / "a.bin").write_bytes(data)
    (tmp_path / ".config").write_text('CONFIG_BOARD="b"\n')
    rc, out = run(tmp_path, tmp_path / "a.bin", tmp_path / ".config")
    m = json.loads((tmp_path / "store" / "zephyr_mcumgr" / "app" / "manifest.json").read_text())
    assert rc == 0 and m["version"] == "0.4.46+45" and m["mcumgr"] == [], out
    (tmp_path / "plain.bin").write_bytes(b"\0" * 64)
    rc, out = run(tmp_path, tmp_path / "plain.bin", tmp_path / ".config")
    assert rc != 0 and "not an MCUboot image" in out
