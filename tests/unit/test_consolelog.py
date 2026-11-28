"""A command's later lines read from its board's console log (module_utils/consolelog.py): after
the command's echo, without the broker's end markers and the prompts, across a rotation."""
import os
import re
import threading
import time

from ansible_collections.rxrbench.core.plugins.module_utils import consolelog

PROMPT = "\x1b[1;32muart:~$ \x1b[m"


def test_the_line_after_the_command_is_found_and_what_came_before_it_is_its_output(tmp_path):
    path = tmp_path / "b.log"
    path.write_bytes(b"old: tool sh: rc 9, 1 ms\r\n")
    log = consolelog.ConsoleLog(str(path))
    start = log.offset()

    def board():
        with open(path, "ab") as f:
            f.write(f"{PROMPT}tool run sh\r\n{PROMPT}mb1a2b3c\r\nmb1a2b3c: command not found\r\n".encode())
            f.flush()
            time.sleep(0.3)
            f.write(b"sh: 3 threads\r\n[00:00:01.000,000] <inf> x: y\r\ntool sh: rc 0, 21 ms\r\nafter\r\n")

    t = threading.Thread(target=board)
    t.start()
    out, m = log.wait(start, "tool run sh", re.compile(r"^tool sh: rc (?P<rc>\d+)", re.M), 5)
    t.join()
    assert m is not None and m.group("rc") == "0"
    assert out == "sh: 3 threads\n[00:00:01.000,000] <inf> x: y\ntool sh: rc 0, 21 ms", out


def test_no_line_in_time_is_none_with_what_was_said(tmp_path):
    path = tmp_path / "b.log"
    path.write_bytes(b"")
    log = consolelog.ConsoleLog(str(path))
    start = log.offset()
    path.write_bytes(b"uart:~$ tool run sh\r\nsh: started\r\n")
    out, m = log.wait(start, "tool run sh", re.compile("^tool sh: rc"), 0.3)
    assert m is None and out == "sh: started"


def test_a_log_that_rotated_meanwhile_is_read_on_from_its_old_file(tmp_path):
    path = tmp_path / "b.log"
    path.write_bytes(b"x" * 100)
    log = consolelog.ConsoleLog(str(path))
    start = log.offset()
    with open(path, "ab") as f:
        f.write(b"uart:~$ tool run sh\r\nsh: half\r\n")
    os.replace(path, str(path) + ".1")
    path.write_bytes(b"tool sh: rc 0, 2 ms\r\n")
    out, m = log.wait(start, "tool run sh", re.compile(r"^tool sh: rc \d+", re.M), 1)
    assert m is not None and out == "sh: half\ntool sh: rc 0, 2 ms", out
