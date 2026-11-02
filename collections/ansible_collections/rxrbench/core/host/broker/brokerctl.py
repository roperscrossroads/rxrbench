#!/usr/bin/env python3
"""Talk to a board's serial broker from a shell, on the bench host that runs it.

    brokerctl.py exec   <board> <cmd> [<cmd> ...] [--deadline S]
    brokerctl.py status <board>
    brokerctl.py log    <board>                     the board's console log path
    brokerctl.py mark   <board> <text...>           write "### rxrbench <utc> <board>: <text>"
                                                    into the board's log
    brokerctl.py release   <board> [--ttl S]        close the port for S s (default 120, max 900)
                                                    and print it: for a tool that needs the port
    brokerctl.py reacquire <board> [--wait S]       reopen it now; retries for up to S s (default
                                                    30), since the board may be rebooting

A tool that needs the port itself (esptool, a flash read, a debugger) releases it, uses the
printed path, and reacquires it. The release has a time limit: if the tool dies, the broker
takes the port back by itself, so a console is never left unlogged for good.

`exec` prints one JSON object per command, in order, as the broker answered it:
{"cmd", "ok", "output", "error", "elapsed"}. A command that timed out has ok false, error
"timeout" and the partial output: a caller must not read it as a complete short answer.
The output is the console's text between the command's echo and the end marker, log lines
included; filtering them is the caller's choice. Exit status: 0 when every command answered,
1 when any did not, 2 when the broker could not be reached.

The socket is $XDG_RUNTIME_DIR/rxrbench/<board>.sock (or --socket-dir). Clients and the
broker take turns: one command at a time per board, whoever asks.
"""
import argparse
import json
import os
import socket
import sys
import time


def socket_path(board, socket_dir=None):
    base = socket_dir or os.path.join(os.environ.get("XDG_RUNTIME_DIR") or
                                      f"/run/user/{os.getuid()}", "rxrbench")
    return os.path.join(base, f"{board}.sock")


def request(path, req, timeout):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("the broker closed the connection")
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        s.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--socket-dir")
    sub = ap.add_subparsers(dest="op", required=True)
    e = sub.add_parser("exec")
    e.add_argument("board")
    e.add_argument("cmds", nargs="+")
    e.add_argument("--deadline", type=float, default=15.0)
    e.add_argument("--dialect", default="zephyr-shell",
                   help="the console's dialect: zephyr-shell, or one the broker has rules for")
    for name in ("status", "log"):
        sub.add_parser(name).add_argument("board")
    m_ = sub.add_parser("mark")
    m_.add_argument("board")
    m_.add_argument("text", nargs="+")
    r_ = sub.add_parser("release")
    r_.add_argument("board")
    r_.add_argument("--ttl", type=float, default=120.0)
    q_ = sub.add_parser("reacquire")
    q_.add_argument("board")
    q_.add_argument("--wait", type=float, default=30.0)
    a = ap.parse_args(argv)
    path = socket_path(a.board, a.socket_dir)
    if not os.path.exists(path):
        print(json.dumps({"ok": False, "error": f"no broker for {a.board} ({path})"}))
        return 2
    try:
        if a.op == "mark":
            r = request(path, {"op": "mark", "text": " ".join(a.text)}, 10)
            if not r.get("ok"):
                print(json.dumps(r), file=sys.stderr)
            return 0 if r.get("ok") else 1
        if a.op == "release":
            r = request(path, {"op": "release", "ttl": a.ttl}, 10)
            if not r.get("ok"):
                print(json.dumps(r), file=sys.stderr)
                return 1
            print(r.get("port") or "")
            return 0
        if a.op == "reacquire":
            end = time.monotonic() + a.wait
            while True:
                r = request(path, {"op": "reacquire"}, 30)
                if r.get("ok") or time.monotonic() >= end:
                    print(json.dumps(r))
                    return 0 if r.get("ok") else 1
                time.sleep(1.0)
        if a.op in ("status", "log"):
            r = request(path, {"op": "status"}, 10)
            print(r.get("log", "") if a.op == "log" else json.dumps(r))
            return 0 if r.get("ok") else 1
        rc = 0
        for cmd in a.cmds:
            req = {"op": "exec", "cmd": cmd, "deadline": a.deadline}
            if a.dialect != "zephyr-shell":
                req["dialect"] = a.dialect
            r = request(path, req, a.deadline + 30)
            print(json.dumps({"cmd": cmd, "ok": bool(r.get("ok")), "output": r.get("output", ""),
                              "error": r.get("error"), "elapsed": r.get("elapsed")}), flush=True)
            rc |= 0 if r.get("ok") else 1
        return rc
    except (OSError, ValueError, ConnectionError) as ex:
        print(json.dumps({"ok": False, "error": f"broker {a.board}: {ex}"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
