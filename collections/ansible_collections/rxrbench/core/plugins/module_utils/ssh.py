"""Run commands on a device over ssh, from the host that can reach it.

A device image may have BusyBox and no Python, so nothing of the tool runs on the device: a
module runs on the access host and drives ssh itself. That also survives the device's own
reboot, which a module running on the device could not.

The timeout is applied on the far side, never only locally: a local kill ends the ssh client
and leaves the remote command running. The local limit is a backstop a little beyond it.
The far side may have no `timeout` command (this BusyBox does not), so the limit is plain
shell: the command runs in its own process group beside a watchdog that kills the group.
"""
import os
import shlex
import subprocess


class SshError(Exception):
    pass


class Target:
    """A device reached through an `ssh` route (module_utils/routes.py)."""

    def __init__(self, route, connect_timeout=8):
        self.host = route.get("ip") or route.get("addr")
        if not self.host:
            raise SshError("the ssh route has no ip")
        self.user = route.get("user") or "root"
        self.port = int(route.get("port") or 22)
        self.jump = route.get("jump")
        # A route may name the key that opens the device and the file its host key is
        # pinned in; without them ssh's own configuration decides both.
        self.identity = os.path.expanduser(route["identity"]) if route.get("identity") else None
        self.known_hosts = (os.path.expanduser(route["known_hosts"])
                            if route.get("known_hosts") else None)
        self.connect_timeout = connect_timeout

    def _opts(self, port_flag):
        o = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
             port_flag, str(self.port)]
        if self.jump:
            o += ["-J", self.jump]
        if self.identity:
            o += ["-i", self.identity, "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            o += ["-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "StrictHostKeyChecking=yes"]
        return o

    def run(self, command, timeout=30):
        """(rc, output). rc 255 is ssh itself failing (unreachable, refused, dropped)."""
        # setsid: the command gets a process group of its own, so the watchdog can kill
        # everything it started, not only its shell (whose children would otherwise live
        # on and hold the connection open).
        remote = (f"setsid sh -c {shlex.quote(command)} & p=$!; "
                  f"(sleep {int(timeout)}; kill -- -$p 2>/dev/null) >/dev/null 2>&1 & w=$!; "
                  f"wait $p; rc=$?; kill $w 2>/dev/null; exit $rc")
        argv = ["ssh", "-n", *self._opts("-p"), f"{self.user}@{self.host}", remote]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15,
                               check=False)
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return 124, f"no answer within {timeout + 15} s"

    def check(self, command, timeout=30):
        """The command's output; raises SshError unless it exits 0."""
        rc, out = self.run(command, timeout)
        if rc != 0:
            raise SshError(f"`{command}` on {self.host}: rc {rc}: {out.strip()[-300:]}")
        return out

    def put(self, local, remote, timeout=600):
        argv = ["scp", "-q", *self._opts("-P"), local, f"{self.user}@{self.host}:{remote}"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise SshError(f"copying to {self.host} did not finish in {timeout} s") from e
        if r.returncode != 0:
            raise SshError(f"copying to {self.host}: {(r.stdout + r.stderr).strip()[-300:]}")

    def write(self, remote, text, mode="600", timeout=30):
        """Write a small text file on the device, whole: sent on the connection's standard
        input (never an argument, never a file on this host), written beside its place with
        its mode, then moved over it. Needs nothing on the device but a shell."""
        q = shlex.quote(remote)
        command = f"umask 077; cat > {q}.new && chmod {mode} {q}.new && mv {q}.new {q}"
        argv = ["ssh", *self._opts("-p"), f"{self.user}@{self.host}", command]
        try:
            r = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=timeout,
                               check=False)
        except subprocess.TimeoutExpired as e:
            raise SshError(f"writing {remote} on {self.host} did not finish in {timeout} s") from e
        if r.returncode != 0:
            raise SshError(f"writing {remote} on {self.host}: rc {r.returncode}: "
                           f"{(r.stdout + r.stderr).strip()[-300:]}")

    def up(self):
        return self.run("true", timeout=8)[0] == 0


class Console:
    """A device's shell over ssh, with the one method a board's console has (`exec`), so
    code that asks a device a question does not care which carries it. ssh itself failing
    (unreachable, refused, dropped, no answer) raises SshError; a command's own exit status
    does not, as on a console."""

    def __init__(self, target):
        self.t = target

    def exec(self, cmd, deadline=20.0, **_kw):
        rc, out = self.t.run(cmd, timeout=max(1, int(deadline)))
        if rc in (255, 124):
            raise SshError(f"`{cmd}` on {self.t.host}: {out.strip()[-200:] or 'no answer'}")
        return out
