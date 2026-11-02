#!/usr/bin/env python3
"""The generated reference docs: lint them, build them, look at them.

    tools/docs.py lint            antsibull-docs' lint of every collection's docs (CI runs it)
    tools/docs.py build           both sites into .build/docs (index.html links them)
    tools/docs.py serve [port]    serve .build/docs on 127.0.0.1 (default 8765)
    tools/docs.py commands [--check]   docs/COMMANDS.md's commands table, from the recipes
    tools/docs.py layout [--check]     LAYOUT.md's tables and the specs, from docs/layout-vars.yml

The commands tables are generated from the recipes (`just --dump`): docs/COMMANDS.md's from
the justfile's own, each plugin's README's from its .just file. A recipe's `[group]` is a
table, its `[doc]` its row, its parameters its synopsis, in the order the file defines them.
`--check` (a unit test) fails when a README is not what the recipes say.

The variables of a layout have one home, docs/layout-vars.yml (name, type, text). `layout`
writes docs/LAYOUT.md's tables from it, and the type and description of each of them in every
role's meta/argument_specs.yml that declares it; `--check` (a unit test) fails when either is
not what the file says.

The tools run from a venv of their own, .build/docs-venv, installed from the lock
docs/tools/requirements.txt (pinned; constrained by the tool's own lock, so ansible-core is the
one the tool runs) and made again when the lock changes. Nothing is published; the sites are
files under .build/.

- The collections' site: antsibull-docs writes the RST of every module, plugin and role of the
  six collections (their DOCUMENTATION, EXAMPLES, RETURN, meta/argument_specs.yml) and Sphinx
  builds it, warnings as errors (docs/tools/sphinx: its configuration, offline).
- The Python site: pdoc over each collection's module_utils, plugin_utils and simulated bench,
  from their docstrings. A collection directory is a namespace package (no __init__.py), so
  pdoc is handed each package itself.
"""

import glob
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import venv

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(REPO, "docs", "tools", "requirements.txt")
SPHINX = os.path.join(REPO, "docs", "tools", "sphinx")
VENV = os.path.join(REPO, ".build", "docs-venv")
OUT = os.path.join(REPO, ".build", "docs")
COLLECTIONS = os.path.join(REPO, "collections")
NAMESPACE = os.path.join(COLLECTIONS, "ansible_collections", "rxrbench")

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rxrbench reference</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem}
a{font-weight:600}li{margin:.6rem 0}</style></head>
<body><h1>rxrbench reference</h1>
<p>Generated from the tree by <code>just docs</code> (.build/docs); not committed.</p>
<ul>
<li><a href="ansible/html/index.html">The collections</a>: every module, plugin and role of the six
collections, from their own documentation (antsibull-docs and Sphinx).</li>
<li><a href="python/index.html">The Python API</a>: module_utils, plugin_utils and the simulated
benches of each collection, from their docstrings (pdoc).</li>
</ul></body></html>
"""


def collections():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(NAMESPACE, "*"))
                  if os.path.exists(os.path.join(p, "galaxy.yml")))


def _bin(name):
    return os.path.join(VENV, "bin", name)


def ensure_venv():
    """The doc tools' venv, as the lock says; made again when the lock changes."""
    with open(LOCK, "rb") as f:
        want = hashlib.sha256(f.read()).hexdigest()
    stamp = os.path.join(VENV, ".lock-sha256")
    if os.path.exists(stamp) and open(stamp).read().strip() == want:
        return
    print(f"docs: installing the doc tools into {os.path.relpath(VENV, REPO)}", flush=True)
    shutil.rmtree(VENV, ignore_errors=True)
    venv.create(VENV, with_pip=True)
    subprocess.run([_bin("python"), "-m", "pip", "install", "-q", "-r", LOCK], check=True)
    with open(stamp, "w") as f:
        f.write(want + "\n")


def _env():
    env = dict(os.environ)
    env["PATH"] = os.path.join(VENV, "bin") + os.pathsep + env.get("PATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.pathsep.join([COLLECTIONS, os.path.join(REPO, ".collections")])
    env["PYTHONPATH"] = COLLECTIONS
    env.pop("VIRTUAL_ENV", None)
    return env


def lint():
    ensure_venv()
    failed = []
    for c in collections():
        r = subprocess.run([_bin("antsibull-docs"), "lint-collection-docs", "--plugin-docs",
                            os.path.join(NAMESPACE, c)], env=_env(), cwd=REPO)
        if r.returncode:
            failed.append(c)
    if failed:
        sys.exit(f"docs: lint failed for {', '.join(failed)}")
    print(f"docs: lint clean ({len(collections())} collections)")


def python_packages():
    """The packages pdoc is handed: each directory of Python a collection's plugins/ (not its
    modules, which antsibull-docs documents) or sim/ holds, unless a regular package above it
    already brings it."""
    dirs = set()
    for f in glob.glob(os.path.join(NAMESPACE, "*", "*", "**", "*.py"), recursive=True):
        rel = os.path.relpath(f, NAMESPACE).split(os.sep)
        if rel[1] not in ("plugins", "sim") or "modules" in rel or "tests" in rel:
            continue
        dirs.add(os.path.dirname(f))

    def brought(d):          # pdoc walks a regular package's subpackages itself
        return all(os.path.exists(os.path.join(p, "__init__.py")) for p in (d, os.path.dirname(d)))

    return sorted(os.path.relpath(d, COLLECTIONS).replace(os.sep, ".")
                  for d in dirs if not brought(d))


def build():
    ensure_venv()
    env = _env()
    shutil.rmtree(OUT, ignore_errors=True)
    rst = os.path.join(OUT, "rst")
    os.makedirs(rst)
    os.chmod(rst, 0o700)          # antsibull-docs wants its destination writable by itself only
    subprocess.run([_bin("antsibull-docs"), "--config-file", os.path.join(SPHINX, "antsibull-docs.cfg"),
                    "collection", "--use-current", "--dest-dir", rst]
                   + [f"rxrbench.{c}" for c in collections()], env=env, cwd=REPO, check=True)
    shutil.copy(os.path.join(SPHINX, "index.rst"), os.path.join(rst, "index.rst"))
    subprocess.run([_bin("sphinx-build"), "-q", "-b", "html", "-c", SPHINX, "-W", "--keep-going",
                    rst, os.path.join(OUT, "ansible", "html")], env=env, cwd=REPO, check=True)
    subprocess.run([_bin("pdoc"), "-o", os.path.join(OUT, "python"), "--docformat", "markdown"]
                   + python_packages(), env=env, cwd=REPO, check=True)
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(INDEX)
    pages = sum(len(fs) for _, _, fs in os.walk(OUT) if fs)
    print(f"docs: built {os.path.relpath(OUT, REPO)}/index.html ({pages} files)")


def serve(port=8765):
    if not os.path.exists(os.path.join(OUT, "index.html")):
        build()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=OUT, **k)  # noqa: E731
    print(f"docs: http://127.0.0.1:{port}/ (ctrl-c stops)")
    http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), handler).serve_forever()


COMMANDS_MD = os.path.join(REPO, "docs", "COMMANDS.md")
BEGIN = "<!-- commands: generated by `just docs-readme` from the recipes' [group] and [doc]; edit those -->"
END = "<!-- commands: end -->"
GROUPS = ["the tool", "bench hosts and devices", "layouts, read-only", "plan and apply",
          "backups and secrets", "simulated benches"]


def _synopsis(name, params):
    out = [name]
    for p in params:
        n, d = p["name"], p["default"]
        if p["kind"] == "star":
            out.append(f"[{n} ...]")
        elif p["kind"] == "plus":
            out.append(f"<{n}> ...")
        elif d is not None:
            out.append(f"[{n}={d}]" if d != "" else f"[{n}]")
        else:
            out.append(f"<{n}>")
    return " ".join(out)


def _defined():
    """Each recipe's place, and the plugin collection whose .just file defines it (None: the
    justfile's own), in the order the justfile imports them."""
    order, owner = {}, {}
    files = [os.path.join(REPO, "justfile")]
    for line in open(files[0]):
        m = re.match(r"^import\??\s+'([^']+)'", line)
        if m:
            files.append(os.path.join(REPO, m.group(1)))
    for f in files:
        if not os.path.exists(f):
            continue
        plugin = None if f == files[0] else "rxrbench." + os.path.basename(os.path.dirname(f))
        for line in open(f):
            m = re.match(r"^([a-z][a-z0-9-]*)[ :]", line)
            if m and not re.match(r"^\S+\s*:=", line) and m.group(1) not in order:
                order[m.group(1)] = len(order)
                owner[m.group(1)] = plugin
    return order, owner


def commands_table(dump, plugin=None):
    """The table of the recipes the justfile defines itself (plugin None), or one plugin's."""
    order, owner = _defined()
    rows = {}
    for name, r in dump["recipes"].items():
        attrs = {k: v for a in r["attributes"] if isinstance(a, dict) for k, v in a.items()}
        if r["private"] or "group" not in attrs or owner.get(name) != plugin:
            continue
        doc = attrs.get("doc") or r["doc"] or ""
        rows.setdefault(attrs["group"], []).append((order.get(name, 1e9), _synopsis(name, r["parameters"]), doc))
    unknown = set(rows) - set(GROUPS)
    if unknown:
        raise SystemExit(f"docs: recipe groups the README does not order: {sorted(unknown)}")
    out = []
    for g in GROUPS:
        if g not in rows:
            continue
        out += ["", f"**{g[:1].upper() + g[1:]}**", "", "| | |", "|---|---|"]
        out += [f"| `just {syn}` | {doc.replace('|', chr(92) + '|')} |" for _, syn, doc in sorted(rows[g])]
    return "\n".join(out).lstrip("\n")


def commands(check=False):
    """docs/COMMANDS.md's table of the justfile's own recipes, and each plugin README's of the
    recipes its .just file brings (the framework's README names no plugin's firmware). With check,
    nothing is written and the answer is whether every table is what the recipes say."""
    dump = json.loads(subprocess.run(["just", "--dump", "--dump-format", "json"], cwd=REPO,
                                     check=True, capture_output=True, text=True).stdout)
    targets = [(COMMANDS_MD, None)] + [
        (os.path.join(NAMESPACE, os.path.basename(os.path.dirname(f)), "README.md"),
         "rxrbench." + os.path.basename(os.path.dirname(f)))
        for f in sorted(glob.glob(os.path.join(NAMESPACE, "*", "*.just")))]
    stale = []
    for path, plugin in targets:
        text = open(path).read()
        if BEGIN not in text or END not in text:
            raise SystemExit(f"docs: {os.path.relpath(path, REPO)} has no {BEGIN!r} ... {END!r} section")
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = f"{head}{BEGIN}\n{commands_table(dump, plugin)}\n{END}{tail}"
        if new == text:
            continue
        stale.append(path)
        if not check:
            with open(path, "w") as f:
                f.write(new)
            print(f"docs: {os.path.relpath(path, REPO)} commands table written")
    return not (check and stale)


LAYOUT_MD = os.path.join(REPO, "docs", "LAYOUT.md")
LAYOUT_VARS = os.path.join(REPO, "docs", "layout-vars.yml")
POINTER = " (Documented with the other layout variables in C(docs/LAYOUT.md).)"


def layout_vars():
    with open(LAYOUT_VARS) as f:
        return yaml.safe_load(f)["tables"]


def _markup(text):
    """A row's Markdown as an argument spec's description: `x` as C(x), a sentence."""
    text = re.sub(r"`([^`]+)`", r"C(\1)", text.strip())
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _layout_table(rows):
    out = ["| Variable | Type | What |", "|---|---|---|"]
    for r in rows:
        name = f"`{r['name']}`" if "name" in r else r["label"]
        out.append(f"| {name} | {r.get('type', '')} | {r['text'].strip()} |")
    return "\n".join(out)


def _spec_block(indent, name, type_, text):
    wrapped = textwrap.wrap(text, 110 - len(indent) - 4, break_on_hyphens=False,
                            break_long_words=False)
    return (f"{indent}{name}: &{name}\n{indent}  type: {type_}\n{indent}  description: >-\n"
            + "".join(f"{indent}    {w}\n" for w in wrapped))


def layout(check=False):
    """LAYOUT.md's tables and the specs' entries of a layout variable, from docs/layout-vars.yml.
    With check, nothing is written and the answer is whether anything would have to change."""
    tables = layout_vars()
    changed = []
    text = open(LAYOUT_MD).read()
    new = text
    for name, rows in tables.items():
        m = re.search(rf"(<!-- layout-vars: {re.escape(name)} -->\n)(.*?)(\n<!-- layout-vars: end -->)",
                      new, re.S)
        if not m:
            raise SystemExit(f"docs: LAYOUT.md has no <!-- layout-vars: {name} --> section")
        new = new[:m.start(2)] + _layout_table(rows) + new[m.end(2):]
    if new != text:
        changed.append(LAYOUT_MD)
        if not check:
            open(LAYOUT_MD, "w").write(new)
    entries = {r["name"]: r for rows in tables.values() for r in rows if "name" in r}
    for spec in sorted(glob.glob(os.path.join(NAMESPACE, "*", "roles", "*", "meta", "argument_specs.yml"))):
        core = os.sep + os.path.join("core", "roles") + os.sep in spec
        text = open(spec).read()
        new = text
        for name, r in entries.items():
            pat = re.compile(rf"^( +){name}: &{name}\n\1  type: \S+\n\1  description: >-\n(?:\1    .*\n)+", re.M)
            m = pat.search(new)
            if not m:
                continue
            desc = _markup(r["text"]) + ("" if core else POINTER)
            new = new[:m.start()] + _spec_block(m.group(1), name, r["type"], desc) + new[m.end():]
        if new != text:
            changed.append(spec)
            if not check:
                open(spec, "w").write(new)
    for f in changed if not check else ():
        print(f"docs: wrote {os.path.relpath(f, REPO)}")
    return not (check and changed)


def main(argv):
    cmd = argv[0] if argv else "build"
    if cmd == "lint":
        lint()
    elif cmd == "build":
        build()
    elif cmd == "serve":
        serve(*argv[1:2])
    elif cmd == "layout":
        if not layout(check="--check" in argv):
            sys.exit("docs: LAYOUT.md or a role spec is not what docs/layout-vars.yml says (just docs-layout)")
    elif cmd == "commands":
        if not commands(check="--check" in argv):
            sys.exit("docs: a commands table is not what the recipes say (just docs-readme)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
