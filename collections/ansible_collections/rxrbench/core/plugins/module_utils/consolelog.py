"""Waiting for a line a board prints after its command has returned (a command's `await`).

A command that starts something (a test, a transfer, a tool) returns at once; its result comes
later, on the console, among the log lines. The board's broker writes everything the console
says to its log on the host it runs on (`status()["log"]`), so a command's later lines are read
there: from where the log ended before the command was sent, forward, until a line matches.

The log rotates (<log> becomes <log>.1 when it is full): a reader that finds the live file
shorter than where it started reads the rest of <log>.1 first.
"""
import os
import re
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
#: the broker's end-of-command marker, as the shell complains about it (broker.py)
MARKER = re.compile(r"(?m)^.*mb[0-9a-f]{6}: command not found[^\n]*\n?")
PROMPT = re.compile(r"(?m)^\S*:~\$ ?")
#: the marker's own echo, once its prompt is gone
MARKER_ECHO = re.compile(r"(?m)^mb[0-9a-f]{6}\n?")


def clean(text):
    return ANSI.sub("", text or "").replace("\r", "")


class ConsoleLog:
    def __init__(self, path):
        if not path:
            raise OSError("the broker names no console log")
        self.path = path

    def offset(self):
        """Where the log ends now: what a command sent next prints is after it."""
        st = os.stat(self.path)
        return (st.st_ino, st.st_size)

    def since(self, start):
        """The text written since `start` (an offset())."""
        ino, off = start
        st = os.stat(self.path)
        parts = []
        if st.st_ino != ino or st.st_size < off:
            # rotated since: the rest of what was the live file, then the new one from its start
            try:
                with open(self.path + ".1", "rb") as f:
                    f.seek(off)
                    parts.append(f.read())
            except OSError:
                pass
            off = 0
        with open(self.path, "rb") as f:
            f.seek(off)
            parts.append(f.read())
        return clean(b"".join(parts).decode(errors="replace"))

    def wait(self, start, cmd, pattern, within, pause=0.1):
        """(output, match): the console's lines after `cmd`'s echo, up to and including the first
        line matching `pattern` (a compiled expression; match None when none came in `within`
        seconds). The broker's end markers and the prompts are left out of the output."""
        end = time.monotonic() + within
        while True:
            text = self.since(start)
            at = text.find(cmd)
            body = text[text.find("\n", at) + 1:] if at != -1 and "\n" in text[at:] else (
                "" if at != -1 else text)
            body = MARKER_ECHO.sub("", PROMPT.sub("", MARKER.sub("", body)))
            m = pattern.search(body)
            if m is not None:
                stop = body.find("\n", m.end())
                return body[:stop if stop != -1 else len(body)].strip("\n"), m
            if time.monotonic() >= end:
                return body.strip("\n"), None
            time.sleep(pause)
