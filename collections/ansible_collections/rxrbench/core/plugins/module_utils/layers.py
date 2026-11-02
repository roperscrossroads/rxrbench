"""Layers: what layouts share, declared once.

A layout that reads `layers: [base, ...]` in its devreg.yml inherits from those layers, in
order, later ones winning. A layer is a directory with no boards of its own:

    layers/<name>/
      layer.conf              required: `schema: 1`, a description, the layers it depends on
      group_vars/all.yml      what every board of a layout gets (builds, store, interpreter, keys)
      group_vars/family_<f>.yml
                              what the boards of one firmware family get (its image template,
                              its device tests): nothing for a board of another family, so a
                              layout may mix families and a family's layer stays harmless to
                              the others
      profiles/<p>.yml        a named profile: a whole board state a layout refers to by name
                              (`head1: {host: benchpi1, profile: plain}` in devreg.yml)
      boards/<board>.yml      an append: this layer's changes to one board, over its profile and
                              the layers below, without copying the board's declaration

The model is Yocto's: a layer is a `meta-*` directory, the layout's `layers:` is `bblayers.conf`,
the layout's own group_vars and host_vars are `local.conf` (top priority), a profile is a
recipe, a board file a `.bbappend`, and `layer.conf` carries the compatibility it does there.
What is deliberately left out: BitBake's task graph, its expansion language and its
per-machine override suffixes; Ansible orders the work.

Composition, from the bottom:

  1. the layers' group_vars/all.yml, in `layers:` order, deep-merged (dicts merge key by key,
     lists and scalars are replaced; the operators below are the exceptions); then, for a
     board whose declared firmware.family is F, the layers' group_vars/family_F.yml the same
     way, each top-level key composed over (1) and replacing it whole (the family's group);
  2. a profile: deep-merged across the layers that define it (and the layout's own profiles/),
     then deep-merged over (1), so a profile may state one field of a nested block; a key the
     profile sets replaces the family's whole (the profile's group outranks the family's), so
     the two should not set the same key;
  3. board appends: the layers' boards/<board>.yml in order, then `from: <layout>/<board>`
     (that board's own declaration in another layout), deep-merged over (2);
  4. the layout's own group_vars/all.yml, then host_vars/<board>: these are Ansible's files,
     and Ansible replaces a top-level key as a whole. The engine models that, not deep-merge,
     so `just show` says what Ansible will use.

Where each lands in Ansible, and therefore who beats whom (tests/unit/test_layers.py proves
it): (1) on group `all`, the family's on group `family_F` and (2) on group `profile_P`, the two
children of `all` (Ansible orders siblings by name, so the profile's wins), all below the
layout's group_vars file; (3) as host variables of the board, above the layout's group_vars
file and below its host_vars.

Operators, in layer files only (the layout's own files are Ansible's and would carry them as
literal keys): `key+:` appends to a list or merges into a dict, `key-:` removes list items or
dict keys, `key?:` sets a key only if nothing below set it.

What never comes from a layer: `bench_apply_allowed` and `bench_first_flash`. They are the
operator's go-aheads for one layout, one board; a layer that states them is refused.

A layout that is another layout with a few boards changed says `from: <layout>` at the top of
its devreg.yml (expand()): every board of that layout it does not list itself is added, on the
host declared there, as `from: <layout>/<board>`. The boards it does list are its own
declarations. Its layers, group_vars and go-aheads stay its own, as with the per-board form.

Provenance is kept for every leaf: `just show <layout> <board>` prints each value with the
file that set it.
"""
import copy
import os

import yaml

from . import site


class LayerError(ValueError):
    pass


# The schema this engine reads. A layer.conf with another `schema:` is refused: the layer
# was written for an engine with other rules, and silently reading it is how a value ends up
# on a board for a reason nobody can find.
LAYER_SCHEMA = 1

# The go-aheads: per layout, per board, dated by hand. Never inherited, and a layer
# that states one is refused rather than ignored, so a stray `true` cannot hide in a layer.
NEVER_INHERITED = ("bench_apply_allowed", "bench_first_flash", "bench_app_manager_overwrite",
                   "bench_overwrite")

OPERATORS = ("+", "-", "?")


# ---- where layers are -------------------------------------------------------------------------

def _roots(layout_dir=None):
    """Site roots, the layout's own first: a layout under <root>/layouts/ sees <root>/layers/
    before any other, so an example layout resolves the example layers even when the site has
    a layer of the same name."""
    roots = []
    if layout_dir:
        parent = os.path.dirname(os.path.abspath(layout_dir))
        if os.path.basename(parent) == "layouts":
            roots.append(os.path.dirname(parent))
    for r in (site.site_dir(), site.EXAMPLE):
        if r not in roots:
            roots.append(r)
    return roots


def layer_dirs(layout_dir=None):
    return [os.path.join(r, "layers") for r in _roots(layout_dir)]


def layer_path(name, layout_dir=None):
    """The directory of a layer, or LayerError."""
    if not name or not isinstance(name, str) or "/" in name or name.startswith("."):
        raise LayerError(f"{name!r} is not a layer name")
    for d in layer_dirs(layout_dir):
        p = os.path.join(d, name)
        if os.path.isdir(p):
            if os.path.exists(os.path.join(p, "devreg.yml")):
                raise LayerError(f"{p} has a devreg.yml: a layout, not a layer")
            return p
    raise LayerError(f"no layer {name!r} in " + " or ".join(layer_dirs(layout_dir)))


def is_layer(name):
    """True when `name` is a layer (for the tools that take a layout name, so `just apply base`
    says why it refuses)."""
    try:
        layer_path(name)
        return True
    except LayerError:
        return False


def layout_dir_of(name, layout_dir=None):
    """Another layout's directory, the same roots as layers (for `from: <layout>/<board>`)."""
    for r in _roots(layout_dir):
        p = os.path.join(r, "layouts", name)
        if os.path.isdir(p):
            return p
    raise LayerError(f"no layout {name!r} in " + " or ".join(os.path.join(r, "layouts") for r in _roots(layout_dir)))


def read_layer_conf(d, name):
    """layer.conf, required: {schema: LAYER_SCHEMA, description: str, depends: [names]}."""
    p = os.path.join(d, "layer.conf")
    if not os.path.exists(p):
        raise LayerError(f"layer {name}: no layer.conf (a layer states `schema: {LAYER_SCHEMA}` there)")
    conf = _read_yaml(p)
    if conf.get("schema") != LAYER_SCHEMA:
        raise LayerError(f"layer {name}: layer.conf schema {conf.get('schema')!r}, this engine reads "
                         f"{LAYER_SCHEMA}: not compatible")
    deps = conf.get("depends") or []
    if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
        raise LayerError(f"layer {name}: layer.conf `depends:` must be a list of layer names")
    return {"schema": LAYER_SCHEMA, "description": str(conf.get("description") or ""), "depends": deps}


# ---- reading -----------------------------------------------------------------------------------

def _read_yaml(path):
    with open(path) as fh:
        out = yaml.safe_load(fh) or {}
    if not isinstance(out, dict):
        raise LayerError(f"{path}: not a mapping")
    return out


def _plain_yaml(d, stem):
    """The plain YAML file <d>/<stem>.yml or .yaml, never a *.sops.* one (secrets stay Ansible's)."""
    for ext in (".yml", ".yaml"):
        p = os.path.join(d, stem + ext)
        if os.path.exists(p):
            return p
    return None


def _yaml_dir(d):
    """{stem: vars} for the plain YAML files of a directory, in name order."""
    out = {}
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith((".yml", ".yaml")) and ".sops." not in f:
                out[f.rsplit(".", 1)[0]] = _read_yaml(os.path.join(d, f))
    return out


def _split_op(key):
    """('channels', '+') for 'channels+'; (key, None) for a plain key."""
    if isinstance(key, str) and len(key) > 1 and key[-1] in OPERATORS:
        return key[:-1], key[-1]
    return key, None


def _no_operators(vars_, label):
    """The layout's own files are Ansible's: an operator there would be a literal key."""
    def walk(d, path):
        for k, v in d.items():
            base, op = _split_op(k)
            if op:
                raise LayerError(f"{label}: {'.'.join(path + (k,))}: operators (`{base}{op}:`) work in "
                                 f"layer files only; this file is read by Ansible as it is")
            if isinstance(v, dict):
                walk(v, path + (k,))
    walk(vars_, ())


# ---- the merge ---------------------------------------------------------------------------------

def deep_merge(base, over, prov=None, over_label=None, path=()):
    """`over` merged into a copy of `base`: dicts key by key, anything else replaced; the
    operators `k+` (append / merge), `k-` (remove), `k?` (default) applied against `base`.
    `prov` maps leaf paths (tuples) to the label that set them; it is updated for every leaf
    `over` sets. Returns (merged, prov). Raises LayerError on an operator that cannot apply."""
    prov = {} if prov is None else prov
    if not isinstance(base, dict) or not isinstance(over, dict):
        _stamp(prov, path, over, over_label)
        return copy.deepcopy(over), prov
    out = dict(base)
    for key, v in over.items():
        k, op = _split_op(key)
        here = path + (k,)
        where = f"{over_label}: {'.'.join(map(str, here))}"
        if op == "?":
            if k not in out:
                _stamp(prov, here, v, over_label)
                out[k] = copy.deepcopy(v)
        elif op == "+":
            cur = out.get(k)
            if cur is None:
                _stamp(prov, here, v, over_label)
                out[k] = copy.deepcopy(v)
            elif isinstance(cur, list) and isinstance(v, list):
                out[k] = cur + copy.deepcopy(v)
                prov[here] = f"{prov.get(here, '?')} + {over_label}"
            elif isinstance(cur, dict) and isinstance(v, dict):
                out[k], prov = deep_merge(cur, v, prov, over_label, here)
            else:
                raise LayerError(f"{where}: `+` needs a list on a list or a dict on a dict, not "
                                 f"{type(v).__name__} on {type(cur).__name__}")
        elif op == "-":
            cur = out.get(k)
            if cur is None:
                continue
            if isinstance(cur, list) and isinstance(v, list):
                out[k] = [x for x in cur if x not in v]
                prov[here] = f"{prov.get(here, '?')} - {over_label}"
            elif isinstance(cur, dict) and isinstance(v, (list, dict)):
                drop = list(v) if isinstance(v, list) else list(v.keys())
                out[k] = {kk: vv for kk, vv in cur.items() if kk not in drop}
                for dk in drop:
                    _stamp(prov, here + (dk,), None, None)
                    prov.pop(here + (dk,), None)
            else:
                raise LayerError(f"{where}: `-` needs a list of items on a list or a list of keys on "
                                 f"a dict, not {type(v).__name__} on {type(cur).__name__}")
        elif k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k], prov = deep_merge(out[k], v, prov, over_label, here)
        else:
            _stamp(prov, here, v, over_label)
            out[k] = copy.deepcopy(v)
    return out, prov


def _stamp(prov, path, value, label):
    """Record `label` for every leaf under `path` (and drop stale leaves below a replaced key)."""
    for p in [p for p in prov if p[:len(path)] == path and p != path]:
        del prov[p]
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _stamp(prov, path + (k,), v, label)
    elif label is not None:
        prov[path] = label


def replace_top(base, over, prov, over_label):
    """Ansible's own rule for two sources of the same precedence class: a top-level key in
    `over` replaces the whole key. Modelled here for the layout's own files."""
    out = dict(base)
    for k, v in over.items():
        _stamp(prov, (k,), v, over_label)
        out[k] = copy.deepcopy(v)
    return out, prov


def _refuse_inherited(vars_, label, problems):
    for k in NEVER_INHERITED:
        if k in vars_ or any(k + op in vars_ for op in OPERATORS):
            problems.append(f"{label} states {k}: a go-ahead is never inherited "
                            f"(state it in the layout that applies)")


def _keys(vars_):
    """The top-level keys a file touches, operators stripped."""
    return {_split_op(k)[0] for k in vars_}


# ---- a layout that is another with a few boards changed ---------------------------------------

def expand(layout_dir, config, _seen=None):
    """The board list of a layout with a top-level `from: <layout>` completed: every board of
    that layout this one does not list is added as `{host: <its host there>, from:
    <layout>/<board>}`. Returns a copy of `config` (the same object when there is nothing to
    expand). The source layout is expanded first, so a chain of them resolves; a cycle is
    refused."""
    src = config.get("from")
    if src is None:
        return config
    layout_dir = os.path.abspath(layout_dir)
    layout_name = os.path.basename(layout_dir)
    if not isinstance(src, str) or not src or "/" in src:
        raise LayerError(f"layout {layout_name}: `from:` must name a layout (a board takes "
                         f"`from: <layout>/<board>` in `boards:`), not {src!r}")
    _seen = set() if _seen is None else _seen
    if layout_dir in _seen:
        raise LayerError(f"layout {layout_name}: `from:` refers back to itself (a cycle)")
    src_dir = layout_dir_of(src, layout_dir)
    if os.path.abspath(src_dir) == layout_dir:
        raise LayerError(f"layout {layout_name}: `from: {src}` is itself")
    src_cfg = expand(src_dir, _read_yaml(os.path.join(src_dir, "devreg.yml")), _seen | {layout_dir})
    out = dict(config)
    boards = dict(config.get("boards") or {})
    for board, spec in sorted((src_cfg.get("boards") or {}).items()):
        if board in boards:
            continue
        spec = spec or {}
        boards[board] = ({"host": spec["host"]} if spec.get("host") else {}) | {"from": f"{src}/{board}"}
    out["boards"] = boards
    return out


# ---- the stack of a layout ---------------------------------------------------------------------

def resolve(layout_dir, config, _seen=None):
    """The layer stack of a layout, read and merged.

    config: the parsed devreg.yml. Returns a dict:
      layers:     [{name, dir, conf, group_vars, profiles: {name: vars}, boards: {name: vars}}]
                  in priority order (first is lowest)
      all_vars:   the layers' group_vars merged
      raw_profiles: {name: vars} merged across the layers and the layout's own profiles/
      profiles:   {name: vars} each composed over all_vars, the keys the profile touches (what
                  the plugin sets on the profile's group, complete, so Ansible's top-level
                  replace keeps the merge)
      profile_of: {board: profile name}
      host_vars:  {board: vars} the keys the board's appends and `from:` touch, composed over
                  all_vars and its profile (what the plugin sets on the host)
      prov:       {("all",)+path | ("profile", name)+path | ("board", name)+path: label}
    Raises LayerError listing every problem at once.
    """
    layout_dir = os.path.abspath(layout_dir)
    layout_name = os.path.basename(layout_dir)
    _seen = set() if _seen is None else _seen
    if layout_dir in _seen:
        raise LayerError(f"layout {layout_name}: `from:` refers back to itself (a cycle)")
    _seen = _seen | {layout_dir}
    config = expand(layout_dir, config)
    names = config.get("layers") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise LayerError("devreg.yml: `layers:` must be a list of layer names")
    if len(set(names)) != len(names):
        raise LayerError(f"devreg.yml: a layer is listed twice in `layers:` {names}")
    problems, stack, prov = [], [], {}
    for i, n in enumerate(names):
        try:
            d = layer_path(n, layout_dir)
            conf = read_layer_conf(d, n)
        except LayerError as e:
            problems.append(str(e))
            continue
        for dep in conf["depends"]:
            if dep not in names[:i]:
                problems.append(f"layer {n}: depends on layer {dep!r}, which is not listed before it "
                                f"in `layers:` {names}")
        gv_path = _plain_yaml(os.path.join(d, "group_vars"), "all")
        gv = _read_yaml(gv_path) if gv_path else {}
        _refuse_inherited(gv, f"layer {n}: group_vars/all.yml", problems)
        # group_vars/family_<f>.yml: for the boards of one family only
        fam_vars = {}
        for stem, fv in _yaml_dir(os.path.join(d, "group_vars")).items():
            if stem.startswith("family_") and len(stem) > len("family_"):
                _refuse_inherited(fv, f"layer {n}: group_vars/{stem}.yml", problems)
                fam_vars[stem[len("family_"):]] = fv
            elif stem != "all":
                problems.append(f"layer {n}: group_vars/{stem}.yml: a layer's group_vars are all.yml "
                                f"and family_<name>.yml, nothing else")
        profiles = _yaml_dir(os.path.join(d, "profiles"))
        for pname, pv in profiles.items():
            _refuse_inherited(pv, f"layer {n}: profiles/{pname}.yml", problems)
        boards = _yaml_dir(os.path.join(d, "boards"))
        for bname, bv in boards.items():
            _refuse_inherited(bv, f"layer {n}: boards/{bname}.yml", problems)
        stack.append({"name": n, "dir": d, "conf": conf, "group_vars": gv, "family_vars": fam_vars,
                      "profiles": profiles, "boards": boards})
    # The layout's own profiles/ is the top of the profile stack (local.conf may hold recipes too).
    own_profiles = _yaml_dir(os.path.join(layout_dir, "profiles"))
    for pname, pv in own_profiles.items():
        _refuse_inherited(pv, f"layout {layout_name}: profiles/{pname}.yml", problems)

    def merge(base, over, label, ppath):
        nonlocal prov
        try:
            return deep_merge(base, over, prov, label, ppath)
        except LayerError as e:
            problems.append(str(e))
            return base, prov

    all_vars = {}
    for L in stack:
        all_vars, prov = merge(all_vars, L["group_vars"], f"layer {L['name']}: group_vars/all.yml", ("all",))
    # A family's vars, merged across the layers like `all`, then each key composed over `all`
    # and complete: what the plugin sets on group family_<f>, which Ansible replaces key by key.
    family_raw, family_vars = {}, {}
    for L in stack:
        for fam, fv in L["family_vars"].items():
            family_raw[fam], prov = merge(family_raw.get(fam, {}), fv,
                                          f"layer {L['name']}: group_vars/family_{fam}.yml", ("family", fam))
    for fam, fv in family_raw.items():
        composed, _ = deep_merge(all_vars, fv, {})
        family_vars[fam] = {k: composed[k] for k in _keys(fv) if k in composed}
    raw_profiles = {}
    for L in stack:
        for pname, pv in L["profiles"].items():
            raw_profiles[pname], prov = merge(raw_profiles.get(pname, {}), pv,
                                              f"layer {L['name']}: profiles/{pname}.yml", ("profile", pname))
    for pname, pv in own_profiles.items():
        raw_profiles[pname], prov = merge(raw_profiles.get(pname, {}), pv,
                                          f"layout {layout_name}: profiles/{pname}.yml", ("profile", pname))
    # A profile composed over the shared vars: what the plugin hands Ansible for the profile's
    # group, complete, so Ansible's top-level replace of `all` by the group loses nothing.
    profiles = {}
    for pname, pv in raw_profiles.items():
        composed, _ = deep_merge(all_vars, pv, {})
        profiles[pname] = {k: composed[k] for k in _keys(pv) if k in composed}

    profile_of, host_vars, board_raw = {}, {}, {}
    for board, spec in sorted((config.get("boards") or {}).items()):
        spec = spec or {}
        p = spec.get("profile")
        if p is not None:
            if p not in raw_profiles:
                problems.append(f"{board}: profile {p!r} is in no layer of this layout "
                                f"(layers: {names}) nor in its profiles/")
                p = None
            else:
                profile_of[board] = p
        # The board's inherited state so far: shared vars, then its profile.
        composed, _ = deep_merge(all_vars, raw_profiles.get(p, {}) if p else {}, {})
        touched = set()
        # Appends: each layer's boards/<board>.yml, in order, over the composed state.
        for L in stack:
            bv = L["boards"].get(board)
            if bv is None:
                continue
            touched |= _keys(bv)
            composed, prov = merge(composed, bv, f"layer {L['name']}: boards/{board}.yml", ("board", board))
        # `from: <layout>/<board>`: that board's own declaration there (profile, appends and
        # host_vars; never its layout's group-level vars), over everything so far.
        src = spec.get("from")
        if src is not None and (not isinstance(src, str) or src.count("/") != 1):
            problems.append(f"{board}: from {src!r}: not <layout>/<board>")
            src = None
        if src is not None:
            try:
                src_layout, src_board = src.split("/", 1)
                src_dir = layout_dir_of(src_layout, layout_dir)
                src_cfg = expand(src_dir, _read_yaml(os.path.join(src_dir, "devreg.yml")))
                if src_board not in (src_cfg.get("boards") or {}):
                    raise LayerError(f"{board}: from {src}: {src_board} is not a board of layout {src_layout}")
                decl, dprov = declared(src_dir, src_cfg, src_board, _seen=_seen)
            except LayerError as e:
                problems.append(f"{board}: from {src}: {e}" if not str(e).startswith(board) else str(e))
                continue
            touched |= set(decl)
            composed, prov = merge(composed, decl, f"from {src}", ("board", board))
            for path, label in dprov.items():
                if ("board", board) + path in prov and prov[("board", board) + path] == f"from {src}":
                    prov[("board", board) + path] = f"{label} (from {src})"
        board_raw[board] = composed
        if touched:
            host_vars[board] = {k: composed[k] for k in touched if k in composed}
    if problems:
        raise LayerError("layers refused:\n  " + "\n  ".join(dict.fromkeys(problems)))
    return {"layers": stack, "all_vars": all_vars, "family_vars": family_vars, "profiles": profiles,
            "raw_profiles": raw_profiles, "profile_of": profile_of, "host_vars": host_vars,
            "board_raw": board_raw, "prov": prov, "layout": layout_name}


def _own_files(layout_dir, board):
    """The layout's own plain files for a board, as Ansible reads them: group_vars/all, then
    host_vars/<board>.yml or the files of host_vars/<board>/ in name order."""
    out = []
    p = _plain_yaml(os.path.join(layout_dir, "group_vars"), "all")
    if p:
        out.append(("group_vars/all.yml", _read_yaml(p)))
    p = _plain_yaml(os.path.join(layout_dir, "host_vars"), board)
    if p:
        out.append((f"host_vars/{board}.yml", _read_yaml(p)))
    d = os.path.join(layout_dir, "host_vars", board)
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith((".yml", ".yaml")) and ".sops." not in f:
                out.append((f"host_vars/{board}/{f}", _read_yaml(os.path.join(d, f))))
    layout_name = os.path.basename(os.path.abspath(layout_dir))
    for rel, vars_ in out:
        _no_operators(vars_, f"layout {layout_name}: {rel}")
    return out


def declared(layout_dir, config, board, resolved=None, _seen=None):
    """What `board` itself declares in this layout, without its layout's group-level vars: its
    profile, the appends, its `from:`, then its own host_vars (replacing top-level keys). This
    is what another layout takes with `from: <layout>/<board>`."""
    r = resolved or resolve(layout_dir, config, _seen=_seen)
    prov = {}
    out = {}
    p = r["profile_of"].get(board)
    if p:
        out, prov = deep_merge(out, r["raw_profiles"][p], prov, f"profile {p}")
    hv = r["host_vars"].get(board, {})
    out, prov = deep_merge(out, {k: r["board_raw"][board][k] for k in hv}, prov, "board")
    for path, label in r["prov"].items():
        if path[:2] == ("profile", p) and path[2:] in prov and prov[path[2:]] == f"profile {p}":
            prov[path[2:]] = label
        if path[:2] == ("board", board) and path[2:] in prov and prov[path[2:]] == "board":
            prov[path[2:]] = label
    for rel, vars_ in _own_files(layout_dir, board):
        if rel.startswith("host_vars/"):
            out, prov = replace_top(out, vars_, prov, f"layout {r['layout']}: {rel}")
    return out, prov


def effective(layout_dir, config, board, resolved=None):
    """What Ansible will see for `board` from the layers and the layout's own plain files,
    with provenance per leaf: (vars, {path: label}). Secrets (*.sops.*) and the variables the
    inventory plugin computes (bench_*) are not included: this is the declared state."""
    r = resolved or resolve(layout_dir, config)
    out, prov = _compose(r, layout_dir, board, None)
    # The family's vars reach a board of that family: composed again with them in place, now
    # that the board's declared family is known (it comes from the profile or the board's files).
    fw = out.get("firmware")
    fam = fw.get("family") if isinstance(fw, dict) else None
    if fam in r["family_vars"]:
        out, prov = _compose(r, layout_dir, board, fam)
    return out, prov


def _compose(r, layout_dir, board, fam):
    prov = {path[1:]: label for path, label in r["prov"].items() if path[0] == "all"}
    out = copy.deepcopy(r["all_vars"])
    p = r["profile_of"].get(board)
    if fam:
        # group family_<fam>, a child of `all`: each key replaces `all`'s whole (it was composed
        # over it). A key the profile also sets is the profile's whole, so it is left for that.
        fv = {k: v for k, v in r["family_vars"][fam].items()
              if not (p and k in _keys(r["raw_profiles"][p]))}
        out, prov = replace_top(out, fv, prov, f"family {fam}")
        for path, label in r["prov"].items():
            if path[:2] == ("family", fam) and path[2:] in prov and prov[path[2:]] == f"family {fam}":
                prov[path[2:]] = label
        for path in [q for q, lab in prov.items() if lab == f"family {fam}"]:
            if ("all",) + path in r["prov"]:
                prov[path] = r["prov"][("all",) + path]
    if p:
        # The same composition the plugin hands Ansible (deep merge over `all`); each leaf the
        # profile set is labelled with the layer that set it, the rest of a merged dict stays `all`'s.
        out, prov = deep_merge(out, r["raw_profiles"][p], prov, f"profile {p}")
        for path, label in r["prov"].items():
            if path[:2] == ("profile", p) and path[2:] in prov and prov[path[2:]] == f"profile {p}":
                prov[path[2:]] = label
    own = _own_files(layout_dir, board)
    for rel, vars_ in own:
        if rel == "group_vars/all.yml":
            out, prov = replace_top(out, vars_, prov, f"layout {r['layout']}: {rel}")
    # The board's host variables from the plugin (appends, `from:`) rank above the layout's
    # group_vars file and below its host_vars: Ansible replaces each key whole with the
    # composed value, which already carries the merge.
    hv = r["host_vars"].get(board, {})
    if hv:
        out, prov = replace_top(out, hv, prov, "board")
        for path, label in r["prov"].items():
            if path[:2] == ("board", board) and path[2:] in prov and prov[path[2:]] == "board":
                prov[path[2:]] = label
        # leaves of a composed key that no append touched keep the label they had
        for path in [q for q, lab in prov.items() if lab == "board"]:
            for src in (("profile", p), ("all",)):
                if src[0] == "profile" and not p:
                    continue
                if src + path in r["prov"]:
                    prov[path] = r["prov"][src + path]
                    break
    for rel, vars_ in own:
        if rel.startswith("host_vars/"):
            out, prov = replace_top(out, vars_, prov, f"layout {r['layout']}: {rel}")
    return out, prov


def describe(layout_dir, config):
    """For `just layers`: each layer, lowest first, with its description, keys, profiles and
    board appends; then the layout and what it uses."""
    config = expand(layout_dir, config)
    r = resolve(layout_dir, config)
    lines = []
    for L in r["layers"]:
        keys = ", ".join(sorted(_keys(L["group_vars"]))) or "-"
        fams = "; ".join(f"{fam}: {', '.join(sorted(_keys(fv)))}"
                         for fam, fv in sorted(L["family_vars"].items())) or "-"
        profs = ", ".join(sorted(L["profiles"])) or "-"
        boards = ", ".join(sorted(L["boards"])) or "-"
        deps = f" (depends: {', '.join(L['conf']['depends'])})" if L["conf"]["depends"] else ""
        lines.append(f"{L['name']:<12} {L['dir']}{deps}\n{'':<12}   {L['conf']['description']}\n"
                     f"{'':<12}   keys: {keys}\n{'':<12}   family keys: {fams}\n"
                     f"{'':<12}   profiles: {profs}\n{'':<12}   boards: {boards}")
    used = sorted(set(r["profile_of"].values()))
    boards = []
    if config.get("from"):
        lines.append(f"{'(from)':<12} layout {config['from']}: its boards not listed below come along "
                     f"as declared there")
    for b, spec in sorted((config.get("boards") or {}).items()):
        spec = spec or {}
        bits = [f"profile {spec['profile']}" if spec.get("profile") else None,
                f"from {spec['from']}" if spec.get("from") else None,
                "appended" if b in r["host_vars"] and not spec.get("from") else None]
        bits = [x for x in bits if x]
        boards.append(f"{b} ({', '.join(bits)})" if bits else b)
    lines.append(f"{'(layout)':<12} {os.path.abspath(layout_dir)}\n{'':<12}   profiles used: "
                 f"{', '.join(used) or '-'}\n{'':<12}   boards: {', '.join(boards) or '-'}")
    return "\n".join(lines)
