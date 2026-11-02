"""What a Linux device says it is: its os-release, kernel, hostname and uptime, in one ssh
command, with nothing on the device but a shell."""

COMMAND = ("cat /etc/os-release; echo ---; hostname; uname -r; uname -m; "
           "cut -d. -f1 /proc/uptime")


def parse(text):
    """The facts in the output of COMMAND."""
    head, sep, tail = text.partition("\n---\n")
    if not sep:
        raise ValueError("the device's answer has no os-release part")
    osr = {}
    for line in head.splitlines():
        k, eq, v = line.partition("=")
        if eq and k.strip():
            osr[k.strip().lower()] = v.strip().strip('"').strip("'")
    rest = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    if len(rest) < 4 or not rest[3].isdigit():
        raise ValueError(f"cannot read hostname, kernel, machine and uptime from {rest!r}")
    return {"os": osr, "hostname": rest[0], "kernel": rest[1], "machine": rest[2],
            "uptime_s": int(rest[3])}


def read(target):
    return parse(target.check(COMMAND, timeout=20))


def differs(osr, declared):
    """{field: [device, declared]} for each declared os-release field the device does not match."""
    return {k.lower(): [osr.get(k.lower()), str(v)] for k, v in (declared or {}).items()
            if osr.get(k.lower()) != str(v)}
