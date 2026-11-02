"""Filters: a board's escrow file, read and written by secret kind.

Each filter is documented in its sidecar file (escrow_get.yml, escrow_put.yml,
secret_verdict.yml): Ansible reads a sidecar only when the .py holds no DOCUMENTATION."""


def escrow_get(escrow, family, kind):
    return dict((((escrow or {}).get("secrets") or {}).get(family) or {}).get(kind) or {})


def escrow_put(escrow, board, family, kind, record, when):
    out = dict(escrow or {"board": board})
    secrets = {f: dict(k) for f, k in (out.get("secrets") or {}).items()}
    secrets.setdefault(family, {})[kind] = {"id": record.get("id"), "value": record.get("value"),
                                            "fingerprint": record.get("fingerprint"),
                                            "exported": when}
    out["secrets"] = secrets
    return out


def secret_verdict(secret, read, escrowed=None):
    """One secret of one board against what it should be, in words that show no secret.
    `secret` is an entry of bench_secret_kinds, `read` what its plugin's read step found,
    `escrowed` the site's record of one the device made."""
    read, escrowed = read or {}, escrowed or {}
    if secret.get("origin") == "device":
        if not read.get("value"):
            return f"cannot be read ({read.get('msg') or 'no answer'})"
        if not secret.get("escrow"):
            return f"{read.get('id')}, not escrowed by this layout's choice"
        if not escrowed:
            return f"{read.get('id')}, NOT ESCROWED (just keys-export)"
        if escrowed.get("value") == read.get("value"):
            return f"{read.get('id')}, matches the escrow"
        if read.get("id") == escrowed.get("id") and (read.get("fingerprint")
                                                     or escrowed.get("fingerprint")):
            # the same id, another key: only the keys' fingerprints tell them apart
            return (f"{read.get('id')} (key {read.get('fingerprint')}) DIFFERS from the escrow "
                    f"({escrowed.get('id')}, key {escrowed.get('fingerprint')})")
        return f"{read.get('id')} DIFFERS from the escrow ({escrowed.get('id')})"
    if not read.get("declared"):
        return "not declared"
    if read.get("msg"):
        return f"cannot be read ({read['msg']})"
    if read.get("matches"):
        return f"{read.get('id')}, as declared"
    return f"{read.get('id')} DIFFERS from what is declared (just plan)"


class FilterModule:
    def filters(self):
        return {"escrow_get": escrow_get, "escrow_put": escrow_put,
                "secret_verdict": secret_verdict}
