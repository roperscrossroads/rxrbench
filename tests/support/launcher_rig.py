"""A fake Launcher console behind a real broker, for tests that drive the launcher plugin's
modules as a play would: the module opens the broker's socket itself."""
import importlib.util
import os
import threading
import time

from ansible_collections.rxrbench.core.plugins.module_utils import broker_client
from tests.support.fake_launcher import FakeLauncher

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO, "collections", "ansible_collections", "rxrbench", "launcher")
TOOL = os.path.join(LAUNCHER, "host", "bin", "launcher_serial.py")


def load_broker():
    spec = importlib.util.spec_from_file_location("broker_for_launcher_modules", os.path.join(
        REPO, "collections", "ansible_collections", "rxrbench", "core", "host", "broker", "broker.py"))
    mb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mb)
    mb.load_dialects(os.path.join(LAUNCHER, "host", "dialects.d"))
    return mb


class Rig:
    """`make(**FakeLauncher args)` -> (node, the broker's socket path); `close()` stops all."""

    def __init__(self, tmp_path, board="cyd"):
        self.mb = load_broker()
        self.tmp, self.board, self.made = tmp_path, board, []

    def make(self, **kw):
        node = FakeLauncher(**kw)
        c = self.mb.Console(self.board, self.mb.path_opener(node.path), str(self.tmp / f"{self.board}.log"))
        broker = self.mb.Broker(c, str(self.tmp / "run" / f"{self.board}.sock"))
        t = threading.Thread(target=broker.run, daemon=True)
        t.start()
        self.made.append((node, broker, t))
        end = time.time() + 3
        while not c.is_open() and time.time() < end:
            time.sleep(0.01)
        return node, broker.sock_path

    def close(self):
        for node, broker, t in self.made:
            broker.shutdown()
            t.join(timeout=2)
            node.close()


def resets_power_on(monkeypatch, node, window=2.0):
    """A pty has no modem lines: the broker's `reset` becomes the fake's power-on, with a boot
    window of `window` seconds, for every Board a module under test opens."""
    real = broker_client.Board.request

    def request(self, **req):
        if req.get("op") == "reset":
            node.window = window
            node.power_on()
            return {"ok": True, "port": node.path}
        return real(self, **req)
    monkeypatch.setattr(broker_client.Board, "request", request)
