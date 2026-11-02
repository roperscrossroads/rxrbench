"""Framework code names no firmware project and no board product without saying why.

Protects core-portability P2 (docs/ARCHITECTURE.md#core-portability): core holds no knowledge that
belongs to one firmware project, board product or update system. A name in executable code (a
string literal, an identifier) is the common sign of such knowledge, so this check flags it; it
is a proxy, and P2 is held by review too. Prose is free (P5): the docs, the READMEs, CLAUDE.md,
comments, docstrings, a module's DOCUMENTATION/EXAMPLES/RETURN and the roles' argument specs
are not scanned.

A name in code that is not knowledge (a vendor's USB product string the code must match, say)
carries its reason inline, on its line or the one above:

    # names: <why this name is here and is not one family's knowledge>
"""
import ast
import io
import os
import re
import subprocess
import tokenize

from tests.support.requirement import breach

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The mesh plugin's projects, and the Linux plugin's update system and health gate: what a
# device runs and how it is updated is a plugin's to know. The roles one firmware gives its
# nodes (a brain, its keyless heads) are that firmware's words.
NAMES = re.compile(r"zephyrtastic|meshtastic|meshcore|zephcore|slotguard|rauc|\bbrain|keyless head",
                   re.I)
# Board products: a plugin brings the boards its firmware runs on, a site names its own. The
# framework knows chips (ESP32, nRF52840, STM32), bootloaders (UF2, MCUboot) and probes.
# Bounded by letters, not by \b: an underscore or a digit is no boundary to a reader.
BOARDS = re.compile(r"(?<![a-z])(?:xiao|t114|wio|e290)(?![a-z])|heltec|sensecap|blue ?pill|rak4631"
                    r"|vision master", re.I)
# Framework code: core, the tools and devreg. Prose (*.md, docs/) is not code.
CODE = ("collections/ansible_collections/rxrbench/core/", "tools/", "devreg/", "justfile",
        "ansible.cfg", "requirements.txt")
# An exception: `names:` then a reason of at least three words, in a comment.
EXCEPTION = re.compile(r"#\s*names:\s*(.*)$")
PROTECTS = ("core-portability P2: core holds no one family's knowledge "
            "(docs/ARCHITECTURE.md#core-portability)")
FIX = ("move the knowledge (format, id, constant, command, behaviour) to the plugin that "
       "brings the family, or to catalog data; core asks through a Family hook "
       "(docs/PLUGINS.md)")
HOW = ("add `# names: <reason>` on the line or the one above, saying why the name is not "
       "one family's knowledge (three words or more)")


DOCS = {"DOCUMENTATION", "EXAMPLES", "RETURN"}


def code_files(repo=REPO):
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=repo, capture_output=True, text=True, check=True)
    return sorted(n for n in out.stdout.split("\n")
                  if n.startswith(CODE) and not n.endswith((".md", ".rst"))
                  and not n.endswith(os.path.join("meta", "argument_specs.yml")))


def _prose_lines(source):
    """Line numbers (1-based) of a Python file's docstrings and documentation strings."""
    out = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            out.update(range(body[0].lineno, body[0].end_lineno + 1))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in DOCS
                                                for t in node.targets):
            out.update(range(node.lineno, node.end_lineno + 1))
    return out


def _strip_comment(name, line):
    if not name.endswith(".py"):
        return re.sub(r"(^|\s)#.*$", "", line)
    try:
        for t in tokenize.generate_tokens(io.StringIO(line + "\n").readline):
            if t.type == tokenize.COMMENT:
                return line[:t.start[1]]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return line


def code_of(name, lines):
    """Each line's code: comments, docstrings and documentation strings blanked."""
    prose = _prose_lines("\n".join(lines)) if name.endswith(".py") else set()
    return ["" if n in prose else _strip_comment(name, line) for n, line in enumerate(lines, 1)]


def _reason(line):
    m = EXCEPTION.search(line)
    return m.group(1).strip() if m else None


def hits(pattern, files, repo=REPO):
    """(unexcused, unreasoned): lines naming without an exception, and exceptions without a
    reason."""
    bare, empty = [], []
    for name in files:
        try:
            with open(os.path.join(repo, name), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        code = code_of(name, lines)
        for i, line in enumerate(lines):
            if not pattern.search(code[i]):
                continue
            why = _reason(line) or (_reason(lines[i - 1]) if i else None)
            where = f"{name}:{i + 1}: {line.strip()[:100]}"
            if why is None:
                bare.append(where)
            elif len(why.split()) < 3:
                empty.append(where)
    return bare, empty


def check(pattern, what, files=None, repo=REPO):
    bare, empty = hits(pattern, code_files(repo) if files is None else files, repo)
    assert not bare, breach([f"{what} named in framework code:"] + bare, PROTECTS, FIX, HOW)
    assert not empty, breach(["a `# names:` exception without its reason:"] + empty,
                             PROTECTS, FIX, HOW)


def test_framework_code_names_no_firmware_project():
    check(NAMES, "a firmware project")


def test_framework_code_names_no_board_product():
    check(BOARDS, "a board product")


def _tree(tmp_path, text):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x.py").write_text(text)
    return ["tools/x.py"]


def test_a_name_in_code_fails_with_the_requirement_and_the_ways_out(tmp_path):
    files = _tree(tmp_path, "FORMAT = 'meshtastic'\n")
    try:
        check(NAMES, "a firmware project", files, str(tmp_path))
    except AssertionError as e:
        text = str(e)
    else:
        raise AssertionError("a name in code passed")
    assert "Protects: core-portability P2" in text and "# names:" in text and "Unsure: ask" in text


def test_a_reasoned_exception_passes_and_an_unreasoned_one_does_not(tmp_path):
    files = _tree(tmp_path, "# names: the vendor's USB product string, read from the board\n"
                            "PRODUCT = 'XIAO-BOOT'\n")
    check(BOARDS, "a board product", files, str(tmp_path))
    (tmp_path / "tools" / "x.py").write_text("PRODUCT = 'XIAO-BOOT'  # names: ok\n")
    try:
        check(BOARDS, "a board product", files, str(tmp_path))
    except AssertionError as e:
        assert "without its reason" in str(e)
    else:
        raise AssertionError("an exception without a reason passed")


PROSE = '''"""The mesh plugin's Meshtastic family says."""
DOCUMENTATION = """
description: a Meshtastic node
"""
x = 1  # a Meshtastic node
'''


def test_comments_and_docstrings_are_prose_and_code_is_not(tmp_path):
    files = _tree(tmp_path, PROSE)
    check(NAMES, "a firmware project", files, str(tmp_path))
    (tmp_path / "tools" / "x.py").write_text("def f():\n    return 'meshtastic'  # its name\n")
    try:
        check(NAMES, "a firmware project", files, str(tmp_path))
    except AssertionError:
        pass
    else:
        raise AssertionError("a name in a string literal passed")


def test_prose_is_not_scanned():
    assert not any(f.endswith(".md") or f.startswith("docs/") for f in code_files())
