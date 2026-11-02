"""A Zephyr-like shell on a pty, with the behaviours the broker has to survive.

- echoes what is typed, coloured prompt, CRLF line ends;
- a receive ring: a burst longer than `ring` bytes loses its tail, silently;
- an unknown command prints "<cmd>: command not found";
- a command may emit a log line mid-output, which redraws the prompt;
- a command may be slow, or never return; the shell may be dead (no echo at all).
"""
import os
import pty
import threading
import time
import tty

PROMPT = "\x1b[1;32muart:~$ \x1b[m"
LOG = "[00:01:02.003,000] \x1b[0m<inf> meshtastic: RX from 0x0a0b0c0d\x1b[0m"


class FakeShell:
    def __init__(self, commands=None, ring=64, dead=False):
        self.commands = dict(commands or {})
        self.ring, self.dead = ring, dead
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self.seen = []       # every complete line the shell executed
        self.dropped = 0     # bytes lost to the ring
        self.line = ""
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def out(self, text):
        os.write(self.master, text.encode())

    def log_line(self):
        """An asynchronous log line: it interrupts whatever is on screen and redraws the prompt."""
        self.out("\r\n" + LOG + "\r\n" + PROMPT + self.line)

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
            if len(data) > self.ring:
                self.dropped += len(data) - self.ring
                data = data[:self.ring]
            if self.dead:
                continue
            for ch in data.decode(errors="replace"):
                if ch == "\r":
                    self.out("\r\n")
                    line, self.line = self.line, ""
                    if line.strip():
                        self.seen.append(line)
                        self._execute(line.strip())
                    self.out(PROMPT)
                else:
                    self.line += ch
                    self.out(ch)

    def _execute(self, line):
        handler = self.commands.get(line)
        if handler is None:
            self.out(f"{line.split()[0]}: command not found\r\n")
            return
        text = handler(self) if callable(handler) else handler
        if text:
            self.out(text.replace("\n", "\r\n") + "\r\n")

    def close(self):
        self.stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
