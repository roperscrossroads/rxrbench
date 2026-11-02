"""The nuttx plugin's tools/nuttx_sim.py: the kconfig-tweak a host without kconfig-frontends is given for NuttX's
configure (the host setup sets the host kind with it), and the tool's own .config tweak."""
import importlib.util
import os
import stat
import subprocess
import sys

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
SPEC = importlib.util.spec_from_file_location("nuttx_sim", os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "nuttx", "tools", "nuttx_sim.py"))
nuttx_sim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nuttx_sim)


def test_the_provided_kconfig_tweak_enables_disables_and_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(nuttx_sim.shutil, "which", lambda _name: None)
    d = nuttx_sim.kconfig_tweak_dir()
    tool = os.path.join(d, "kconfig-tweak")
    assert os.stat(tool).st_mode & stat.S_IXUSR
    cfg = tmp_path / ".config"
    cfg.write_text("CONFIG_HOST_WINDOWS=y\n# CONFIG_HOST_LINUX is not set\nCONFIG_OTHER=\"x\"\n")
    for args in (("--disable", "CONFIG_HOST_WINDOWS"), ("--enable", "CONFIG_HOST_LINUX"),
                 ("--enable", "CONFIG_HOST_ARM64")):
        subprocess.run([sys.executable, tool, "--file", str(cfg), *args], check=True)
    assert cfg.read_text() == ("# CONFIG_HOST_WINDOWS is not set\nCONFIG_HOST_LINUX=y\n"
                               "CONFIG_OTHER=\"x\"\nCONFIG_HOST_ARM64=y\n")


def test_a_host_with_kconfig_frontends_keeps_its_own(monkeypatch):
    monkeypatch.setattr(nuttx_sim.shutil, "which", lambda _name: "/usr/bin/kconfig-tweak")
    assert nuttx_sim.kconfig_tweak_dir() is None


def test_the_tools_own_tweak_sets_strings_and_unsets(tmp_path):
    cfg = tmp_path / ".config"
    cfg.write_text("CONFIG_LINE_MAX=80\nCONFIG_SIM_UART_DMA=y\n")
    nuttx_sim.tweak(str(cfg), "CONFIG_LINE_MAX=512")
    nuttx_sim.tweak(str(cfg), "CONFIG_SIM_UART_DMA=n")
    nuttx_sim.tweak(str(cfg), 'CONFIG_NSH_PROMPT_STRING="nsh-b> "')
    assert cfg.read_text() == ('CONFIG_LINE_MAX=512\n# CONFIG_SIM_UART_DMA is not set\n'
                               'CONFIG_NSH_PROMPT_STRING="nsh-b> "\n')
