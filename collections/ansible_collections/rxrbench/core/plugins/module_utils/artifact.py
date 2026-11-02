"""Firmware artifacts: where an image comes from, and getting exactly that image.

A source says where the bytes live; fetching puts them in a content-addressed cache and returns
a local path. Anything that is not a local path must be pinned by sha256: an image flashed onto
a board is whatever the pin says, not whatever a server returned today.

Source types (`source.type`):

    path     a local file, or a directory when the image names its files (`parts`)
    url      any http(s)/file URL
    github   a release asset: repo, tag, asset
    forgejo  a release asset (base, repo, tag, asset), or a generic package
             (base, owner, package, version, file)

`member` picks one file out of a zip (a release published as one archive of many images). `token_env` names
an environment variable holding an access token; the token is sent, never stored or logged.

Images a family builds from its own source are not fetched here: they are built and identified
by their image key. A CI system that publishes those builds makes them one more source of
this kind, with nothing else changing.
"""
import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile


class ArtifactError(ValueError):
    pass


def default_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "rxrbench", "artifacts")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _need(src, *keys):
    missing = [k for k in keys if not src.get(k)]
    if missing:
        raise ArtifactError(f"{src.get('type')} source needs {', '.join(missing)}")


def locate(src):
    """Where a source's bytes are: ('path', p) or ('url', u). Pure; touches nothing."""
    t = (src or {}).get("type")
    if t == "path":
        _need(src, "path")
        return "path", os.path.expanduser(src["path"])
    if t == "url":
        _need(src, "url")
        return "url", src["url"]
    if t == "github":
        _need(src, "repo", "tag", "asset")
        return "url", (f"https://github.com/{src['repo']}/releases/download/"
                       f"{src['tag']}/{src['asset']}")
    if t == "forgejo":
        _need(src, "base")
        base = src["base"].rstrip("/")
        if src.get("package"):
            _need(src, "owner", "package", "version", "file")
            return "url", (f"{base}/api/packages/{src['owner']}/generic/{src['package']}/"
                           f"{src['version']}/{src['file']}")
        _need(src, "repo", "tag", "asset")
        return "url", f"{base}/{src['repo']}/releases/download/{src['tag']}/{src['asset']}"
    raise ArtifactError(f"unknown source type {t!r} (path, url, github, forgejo)")


def check_source(src, on_disk=False):
    """Problems with a source declaration, without fetching anything. With `on_disk`, a path
    source is also looked for: the path must exist, and a pinned file must hash to its pin (a
    pinned directory is not hashed). A catalog's examples point at builds a reader does not
    have, so the declaration alone is checked unless the caller asks for the disk."""
    try:
        kind, where = locate(src)
    except ArtifactError as e:
        return [str(e)]
    out = []
    pin = src.get("sha256")
    if kind == "url" and not pin:
        out.append("a remote source must be pinned with sha256")
    if pin and (len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin)):
        out.append("sha256 must be 64 lowercase hex characters")
    if on_disk and kind == "path":
        if not os.path.exists(where):
            out.append(f"no such path: {where}")
        elif pin and not out and os.path.isfile(where) and sha256_file(where) != pin:
            out.append(f"{where}: sha256 is {sha256_file(where)}, the pin says {pin}")
    return out


def _download(url, dest, token=None, timeout=120):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)


def fetch(src, cache_dir=None):
    """Return a local path holding exactly the artifact `src` names.

    A path source is returned as it is (and checked against its pin when it has one). A remote
    source is downloaded once into the cache under its sha256; a download that does not match
    the pin is deleted and refused.
    """
    problems = check_source(src)
    if problems:
        raise ArtifactError("; ".join(problems))
    kind, where = locate(src)
    pin = src.get("sha256")
    if kind == "path":
        if not os.path.exists(where):
            raise ArtifactError(f"no such path: {where}")
        if pin and os.path.isfile(where) and sha256_file(where) != pin:
            raise ArtifactError(f"{where}: sha256 is {sha256_file(where)}, the pin says {pin}")
        got = where
    else:
        cache_dir = cache_dir or default_cache_dir()
        slot = os.path.join(cache_dir, pin)
        got = os.path.join(slot, os.path.basename(where.split("?")[0]) or "artifact")
        if not (os.path.exists(got) and sha256_file(got) == pin):
            os.makedirs(slot, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=slot, prefix=".part-")
            os.close(fd)
            try:
                token = os.environ.get(src["token_env"]) if src.get("token_env") else None
                _download(where, tmp, token)
                actual = sha256_file(tmp)
                if actual != pin:
                    raise ArtifactError(f"{where}: sha256 is {actual}, the pin says {pin}")
                os.replace(tmp, got)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    if src.get("member"):
        got = _member(got, src["member"], cache_dir or default_cache_dir())
    return got


def _member(archive, member, cache_dir):
    """One file out of a zip, extracted beside the cache slot of the archive's own hash."""
    if not zipfile.is_zipfile(archive):
        raise ArtifactError(f"{archive}: `member` needs a zip archive")
    out_dir = os.path.join(cache_dir, "members", sha256_file(archive))
    out = os.path.join(out_dir, os.path.basename(member))
    if os.path.exists(out):
        return out
    with zipfile.ZipFile(archive) as z:
        if member not in z.namelist():
            raise ArtifactError(f"{archive}: no member {member!r}")
        os.makedirs(out_dir, exist_ok=True)
        with z.open(member) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f)
    return out


def parts(image, root):
    """The files an image flashes and where, resolved under `root` (a file or a directory).

    esptool images name `parts: [{offset, file}]`; a single-file image (a UF2, a factory bin)
    names none and is itself the one part.
    """
    declared = image.get("parts")
    if not declared:
        if os.path.isdir(root):
            raise ArtifactError("the source is a directory: the image must name its parts")
        return [{"offset": image.get("offset", 0), "path": root}]
    if not os.path.isdir(root):
        raise ArtifactError("the image names parts, so its source must be a directory")
    out = []
    for p in declared:
        path = os.path.join(root, p["file"])
        if not os.path.isfile(path):
            raise ArtifactError(f"missing part {p['file']} under {root}")
        out.append({"offset": int(p["offset"]), "path": path})
    return sorted(out, key=lambda x: x["offset"])
