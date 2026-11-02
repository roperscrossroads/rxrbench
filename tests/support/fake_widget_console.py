"""A console on a pty in the fixture plugin's line dialect (fixture.widgets, `widget-line`): the
smallest board a line dialect describes, for core's broker tests.

- each typed character is echoed, except after `passwd `: a secret never reaches a log;
- a line ends at CR; the answer is one line, "= <text>", or a bare state line
  "~S{json}*crc16" for `widget state`; a blank line gets no answer, an unknown one
  "= Unknown command";
- `widget wipe` reboots without an answer (here: counted).

`echo_secrets=True` is a firmware that does not keep the contract: it echoes the secret like
any other text and repeats it in its answer.
"""
import binascii
import json
import os
import pty
import threading
import time
import tty

SECRET_WORDS = ("passwd ",)


class FakeWidgetConsole:
    def __init__(self, echo_secrets=False):
        self.echo_secrets = echo_secrets
        self.st = {"name": "widget1", "password": ""}
        self.wipes = 0
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.seen = []
        self.line = ""
        self.echo = True
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def out(self, text):
        os.write(self.master, text.encode())

    def _run(self):
        os.set_blocking(self.master, False)
        while not self.stop:
            try:
                data = os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                return
            for ch in data.decode(errors="replace"):
                if ch == "\r":
                    self.out("\n")
                    line, self.line, self.echo = self.line.strip(), "", True
                    if not line:
                        continue
                    self.seen.append(line)
                    reply = self._execute(line)
                    if reply is not None:
                        self.out(reply + "\r\n")
                elif ch != "\n":
                    self.line += ch
                    if self.echo:
                        self.out(ch)
                    if self.line in SECRET_WORDS and not self.echo_secrets:
                        self.echo = False

    def state(self):
        j = json.dumps({"v": 1, "board": "native_sim", "image": "1.0.0", "class": 3,
                        "name": self.st["name"]}, separators=(",", ":"))
        return f"~S{j}*{binascii.crc_hqx(j.encode(), 0):04x}"

    def _execute(self, line):
        if line == "get name":
            return f"= {self.st['name']}"
        if line == "widget state":
            return self.state()
        if line.startswith("passwd "):
            self.st["password"] = line.split(" ", 1)[1]
            return "= OK" + (f" passwd is {self.st['password']}" if self.echo_secrets else "")
        if line == "widget wipe":
            self.wipes += 1
            return None
        return "= Unknown command"

    def close(self):
        self.stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
