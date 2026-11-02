"""Layers (module_utils/layers.py): the merge rules, the refusals, and the order Ansible
actually applies, proven against ansible-inventory on a temporary site."""
import json
import os
import subprocess

import pytest
import yaml

from ansible_collections.rxrbench.core.plugins.module_utils import layers

REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh)


def _layer(site, name, group_vars=None, profiles=None, boards=None, depends=(), schema=1, family_vars=None):
    d = site / "layers" / name
    _write(str(d / "layer.conf"), {"schema": schema, "description": f"test layer {name}",
                                   "depends": list(depends)})
    if group_vars is not None:
        _write(str(d / "group_vars/all.yml"), group_vars)
    for fam, fv in (family_vars or {}).items():
        _write(str(d / f"group_vars/family_{fam}.yml"), fv)
    for pname, pv in (profiles or {}).items():
        _write(str(d / f"profiles/{pname}.yml"), pv)
    for bname, bv in (boards or {}).items():
        _write(str(d / f"boards/{bname}.yml"), bv)
    return d


# ---- the merge rules ----------------------------------------------------------------------

def test_dicts_merge_lists_and_scalars_replace():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1, "c": {"deep": {"k": "v"}}}
    over = {"a": {"y": [3], "z": 2}, "b": 2, "c": "flat"}
    out, prov = layers.deep_merge(base, over, {}, "over")
    assert out == {"a": {"x": 1, "y": [3], "z": 2}, "b": 2, "c": "flat"}
    assert prov == {("a", "y"): "over", ("a", "z"): "over", ("b",): "over", ("c",): "over"}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1, "c": {"deep": {"k": "v"}}}, "base untouched"


def test_provenance_follows_each_leaf():
    out, prov = layers.deep_merge({}, {"m": {"a": 1, "b": 2}}, {}, "low")
    out, prov = layers.deep_merge(out, {"m": {"b": 3}}, prov, "high")
    assert out == {"m": {"a": 1, "b": 3}}
    assert prov == {("m", "a"): "low", ("m", "b"): "high"}


def test_a_replaced_dict_drops_its_old_leaves_from_provenance():
    out, prov = layers.deep_merge({}, {"m": {"a": 1}}, {}, "low")
    out, prov = layers.deep_merge(out, {"m": "gone"}, prov, "high")
    assert out == {"m": "gone"} and prov == {("m",): "high"}


def test_ansible_replaces_a_top_level_key_whole():
    out, prov = layers.deep_merge({}, {"m": {"a": 1, "b": 2}}, {}, "layer")
    out, prov = layers.replace_top(out, {"m": {"b": 3}}, prov, "layout")
    assert out == {"m": {"b": 3}}, "the layout's own file replaces the key, as Ansible does"
    assert prov == {("m", "b"): "layout"}


# ---- a temporary site: two layers and a layout ------------------------------------------------

@pytest.fixture
def tsite(tmp_path, monkeypatch):
    site = tmp_path / "site"
    monkeypatch.setenv("RXRBENCH_SITE", str(site))
    os.makedirs(site, exist_ok=True)
    (site / "rf_rules.yml").write_text("region: US\nmax_tx_power: 2\n")   # a quiet bench in the US
    _layer(site, "base",
           group_vars={"builds": {"fw": {"version": "1", "ref": "main"}}, "store": "/s", "interp": "/py"},
           profiles={"xiao": {"firmware": {"family": "zephyrtastic", "class": 1},
                              "meshtastic": {"config": {"lora": {"region": "US", "tx_power": 2}}}}})
    _layer(site, "extra",
           group_vars={"builds": {"fw": {"version": "2"}}, "tests": ["a"]},
           profiles={"xiao": {"meshtastic": {"config": {"lora": {"tx_power": 5}}}}})
    lay = site / "layouts/lay"
    _write(str(lay / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base", "extra"],
            "boards": {"b1": {"profile": "xiao"}, "b2": {}, "b3": {"profile": "xiao"}}})
    _write(str(lay / "group_vars/all.yml"), {"store": "/own", "bench_apply_allowed": True})
    _write(str(lay / "host_vars/b1.yml"), {"meshtastic": {"owner": "b1"}})
    return site, lay


def _cfg(lay):
    return yaml.safe_load(open(os.path.join(lay, "devreg.yml")))


def test_layers_compose_in_order(tsite):
    site, lay = tsite
    r = layers.resolve(str(lay), _cfg(lay))
    assert [L["name"] for L in r["layers"]] == ["base", "extra"]
    assert r["all_vars"] == {"builds": {"fw": {"version": "2", "ref": "main"}}, "store": "/s",
                             "interp": "/py", "tests": ["a"]}
    assert r["raw_profiles"]["xiao"]["meshtastic"]["config"]["lora"] == {"region": "US", "tx_power": 5}
    assert r["profile_of"] == {"b1": "xiao", "b3": "xiao"}
    assert r["prov"][("all", "builds", "fw", "version")] == "layer extra: group_vars/all.yml"
    assert r["prov"][("all", "builds", "fw", "ref")] == "layer base: group_vars/all.yml"


def test_effective_state_of_a_board(tsite):
    site, lay = tsite
    v, prov = layers.effective(str(lay), _cfg(lay), "b1")
    assert v["store"] == "/own" and prov[("store",)] == "layout lay: group_vars/all.yml"
    assert v["builds"] == {"fw": {"version": "2", "ref": "main"}}
    assert v["firmware"] == {"family": "zephyrtastic", "class": 1}
    assert prov[("firmware", "class")] == "layer base: profiles/xiao.yml"
    # The layout's host_vars replaces the top-level key whole: the profile's lora config is gone
    # for this board, exactly as Ansible will see it.
    assert v["meshtastic"] == {"owner": "b1"}
    assert prov[("meshtastic", "owner")] == "layout lay: host_vars/b1.yml"
    v2, _ = layers.effective(str(lay), _cfg(lay), "b2")
    assert "firmware" not in v2 and v2["store"] == "/own"
    # b3: the profile alone, merged across both layers, each leaf labelled with its layer
    v3, prov3 = layers.effective(str(lay), _cfg(lay), "b3")
    assert v3["meshtastic"]["config"]["lora"] == {"region": "US", "tx_power": 5}
    assert prov3[("meshtastic", "config", "lora", "tx_power")] == "layer extra: profiles/xiao.yml"
    assert prov3[("meshtastic", "config", "lora", "region")] == "layer base: profiles/xiao.yml"


def test_a_go_ahead_in_a_layer_is_refused(tsite):
    site, lay = tsite
    _write(str(site / "layers/extra/group_vars/all.yml"), {"bench_apply_allowed": True})
    with pytest.raises(layers.LayerError, match="bench_apply_allowed.*never inherited"):
        layers.resolve(str(lay), _cfg(lay))
    _write(str(site / "layers/extra/group_vars/all.yml"), {})
    _write(str(site / "layers/extra/profiles/xiao.yml"), {"bench_first_flash": True})
    with pytest.raises(layers.LayerError, match="bench_first_flash.*never inherited"):
        layers.resolve(str(lay), _cfg(lay))


def test_unknown_layer_and_unknown_profile_are_refused_together(tsite):
    site, lay = tsite
    cfg = _cfg(lay)
    cfg["layers"] = ["base", "nope"]
    cfg["boards"]["b2"] = {"profile": "heltec"}
    with pytest.raises(layers.LayerError) as e:
        layers.resolve(str(lay), cfg)
    assert "no layer 'nope'" in str(e.value) and "profile 'heltec'" in str(e.value)


def test_a_layout_is_not_a_layer(tsite):
    site, lay = tsite
    _write(str(site / "layers/lay2/devreg.yml"), {"plugin": "x", "boards": {}})
    with pytest.raises(layers.LayerError, match="a layout, not a layer"):
        layers.layer_path("lay2")
    assert layers.is_layer("base") and not layers.is_layer("lay")


def test_where_refuses_a_layer_as_a_layout(tsite):
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "python"), os.path.join(REPO, "tools", "where.py"),
                        "base"], capture_output=True, text=True)
    assert r.returncode != 0 and "a layer, not a layout" in r.stderr


# ---- the order Ansible applies, proven --------------------------------------------------------

@pytest.fixture
def ansible_site(tmp_path, monkeypatch):
    """The simulated bench's registry and catalog, a layer, and a layout of sim1 + sim2 that
    inherits it and overrides one key in each of its own files."""
    site = tmp_path / "site"
    monkeypatch.setenv("RXRBENCH_SITE", str(site))
    os.makedirs(site, exist_ok=True)
    (site / "rf_rules.yml").write_text("region: US\nmax_tx_power: 2\n")   # a quiet bench in the US
    _layer(site, "base",
           group_vars={"shared": {"a": 1, "b": 2}, "shared2": {"a": 1, "b": 2}, "only_layer": "L",
                       "both": "layer", "templated": "{{ only_layer }}-{{ shared.c }}"},
           profiles={"sim": {"firmware": {"family": "zephyrtastic", "class": 0},
                             "meshtastic": {"config": {"lora": {"region": "US", "modem_preset": "SHORT_FAST",
                                                                "tx_power": 2, "tx_enabled": True}}},
                             "shared": {"b": 20, "c": 30}, "shared2": {"b": 20, "c": 30}}})
    lay = site / "layouts/t"
    _write(str(lay / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base"],
            "registry": os.path.join(REPO, "collections/ansible_collections/rxrbench/mesh/sim", "registry.yaml"),
            "catalog_dir": os.path.join(REPO, "collections/ansible_collections/rxrbench/mesh/sim", "catalog"),
            "boards": {"sim1": {"host": "simhost", "profile": "sim"}, "sim2": {"host": "simhost"}}})
    _write(str(lay / "group_vars/all.yml"), {"both": "layout", "bench_apply_allowed": False,
                                              "ansible_python_interpreter": "/usr/bin/python3"})
    # a key the profile also sets, restated here: Ansible replaces it whole (a layout that did
    # this to `meshtastic` would lose the profile's lora config, and the RF rules would refuse it)
    _write(str(lay / "host_vars/sim1.yml"), {"shared2": {"z": 1}})
    _write(str(lay / "host_vars/sim2.yml"),
           {"firmware": {"family": "zephyrtastic", "class": 0},
            "meshtastic": {"config": {"lora": {"region": "US", "modem_preset": "SHORT_FAST",
                                               "tx_power": 2, "tx_enabled": True}}}})
    return site, lay



@pytest.fixture
def widget_site(tmp_path, monkeypatch):
    """The same layer and layout on a site of the fixture plugin's widget family (the plugin
    under <site>/collections, loaded by <site>/plugins.yml): the layer mechanics with core
    alone, no bundled plugin's bench."""
    site = tmp_path / "site"
    monkeypatch.setenv("RXRBENCH_SITE", str(site))
    os.makedirs(site, exist_ok=True)
    os.symlink(os.path.join(REPO, "tests", "fixtures", "collections"), site / "collections")
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", os.pathsep.join(
        [os.environ.get("RXRBENCH_COLLECTIONS") or os.path.join(REPO, "collections"), str(site / "collections")]))
    (site / "plugins.yml").write_text("plugins: [fixture.widgets]\n")
    (site / "rf_rules.yml").write_text("region: US\nmax_tx_power: 2\n")
    _write(str(site / "registry.yaml"), {"version": 1, "hosts": {"work": {"local": True}}, "devices": {
        b: {"kind": "serial", "policy": {"board_tag": "board-w"},
            "match": {"usb_serial": f"W{i}"}, "reach": {"usb": {"host": "work"}}}
        for i, b in enumerate(("sim1", "sim2"))}})
    _write(str(site / "catalog/boards.yml"), {"boards": {"board-w": {"mcu": "esp32s3"}}})
    _write(str(site / "catalog/firmware.yml"), {"images": {"w1": {
        "family": "widget", "board_tags": ["board-w"], "flasher": "esptool", "rf": "none",
        "source": {"type": "path", "path": "/nowhere"}}}})
    widget = {"firmware": {"family": "widget", "image": "w1"}}
    _layer(site, "base",
           group_vars={"shared": {"a": 1, "b": 2}, "shared2": {"a": 1, "b": 2}, "only_layer": "L",
                       "both": "layer", "templated": "{{ only_layer }}-{{ shared.c }}"},
           profiles={"sim": {**widget, "shared": {"b": 20, "c": 30}, "shared2": {"b": 20, "c": 30}}})
    lay = site / "layouts/t"
    _write(str(lay / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base"], "registry": "../../registry.yaml",
            "catalog_dir": "../../catalog",
            "boards": {"sim1": {"host": "work", "profile": "sim"}, "sim2": {"host": "work"}}})
    _write(str(lay / "group_vars/all.yml"), {"both": "layout", "bench_apply_allowed": False,
                                              "ansible_python_interpreter": "/usr/bin/python3"})
    _write(str(lay / "host_vars/sim1.yml"), {"shared2": {"z": 1}})
    _write(str(lay / "host_vars/sim2.yml"), widget)
    return site, lay

def _inventory(lay, board):
    env = dict(os.environ, RXRBENCH_LOCATION_DIR="/nonexistent")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-inventory"), "-i", str(lay),
                        "--host", board], capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_ansible_applies_the_order_the_engine_models(widget_site):
    site, lay = widget_site
    h1 = _inventory(lay, "sim1")
    # layer < layout group_vars: the layout's own file wins on a shared key
    assert h1["both"] == "layout"
    # a layer's key reaches the host when nothing above restates it
    assert h1["only_layer"] == "L"
    # layer all < profile: the profile's group (a child of all) wins, composed over the layer's
    # value so the merge survives Ansible's top-level replace
    assert h1["shared"] == {"a": 1, "b": 20, "c": 30}
    # profile < layout host_vars: the file replaces the key whole
    assert h1["shared2"] == {"z": 1}
    assert h1["firmware"] == {"family": "widget", "image": "w1"}
    assert h1["bench_layers"] == ["base"]
    # a board with no profile sees only the layer's shared vars
    h2 = _inventory(lay, "sim2")
    assert h2["shared"] == {"a": 1, "b": 2} and h2["shared2"] == {"a": 1, "b": 2}
    # and the engine's model is what Ansible did, key for key
    for board, got in (("sim1", h1), ("sim2", h2)):
        want, _ = layers.effective(str(lay), _cfg(lay), board)
        assert {k: got.get(k) for k in want} == want, board


def test_ansible_refuses_a_layout_whose_layer_states_a_go_ahead(ansible_site):
    site, lay = ansible_site
    _write(str(site / "layers/base/group_vars/all.yml"), {"bench_apply_allowed": True})
    env = dict(os.environ, RXRBENCH_LOCATION_DIR="/nonexistent")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible-inventory"), "-i", str(lay),
                        "--host", "sim1"], capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode != 0 and "never inherited" in r.stderr


# ---- the layout check sees a profile's state -------------------------------------------------

@pytest.mark.plugins
def test_a_profiled_board_is_grouped_and_checked_like_a_declared_one(ansible_site, monkeypatch):
    from ansible_collections.rxrbench.core.plugins.module_utils import layout
    site, lay = ansible_site
    monkeypatch.setenv("RXRBENCH_LOCATION_DIR", "/nonexistent")
    r = layout.load_layout(str(lay), _cfg(lay))
    # sim1's class and family come from its profile, sim2's from its host_vars: same groups
    for b in ("sim1", "sim2"):
        assert {"class_0", "family_zephyrtastic"} <= set(r["boards"][b]["groups"]), b
        assert r["boards"][b]["vars"]["bench_family"] == "zephyrtastic"
    # and the RF rules read the profile too: a profile above the power limit refuses the layout
    _write(str(site / "layers/base/profiles/sim.yml"),
           {"firmware": {"family": "zephyrtastic", "class": 0},
            "meshtastic": {"config": {"lora": {"region": "US", "modem_preset": "LONG_FAST",
                                               "tx_power": 14, "tx_enabled": True}}}})
    with pytest.raises(layout.LayoutError, match="14 dBm is above the ceiling"):
        layout.load_layout(str(lay), _cfg(lay))


def test_a_layers_template_renders_like_a_group_vars_file(widget_site):
    """A string an inventory plugin sets is inert to `{{ }}` on ansible-core 2.19+ unless the
    plugin marks it trusted; a layer's files are the site's own YAML and must render."""
    site, lay = widget_site
    env = dict(os.environ, RXRBENCH_LOCATION_DIR="/nonexistent")
    r = subprocess.run([os.path.join(REPO, "venv", "bin", "ansible"), "-i", str(lay), "sim1", "-m",
                        "ansible.builtin.debug", "-a", "var=templated"],
                       capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode == 0, r.stderr
    assert '"templated": "L-30"' in r.stdout, r.stdout


# ---- operators ---------------------------------------------------------------------------------

def test_plus_appends_to_a_list_and_merges_into_a_dict():
    out, prov = layers.deep_merge({"tests": [1], "m": {"a": 1}}, {"tests+": [2], "m+": {"b": 2}}, {}, "L")
    assert out == {"tests": [1, 2], "m": {"a": 1, "b": 2}}
    assert prov[("tests",)].endswith("+ L") and prov[("m", "b")] == "L"
    out, _ = layers.deep_merge({}, {"tests+": [2]}, {}, "L")
    assert out == {"tests": [2]}, "appending to nothing sets"


def test_minus_removes_list_items_and_dict_keys():
    base = {"tests": [{"name": "a"}, {"name": "b"}], "m": {"a": 1, "b": 2}}
    out, _ = layers.deep_merge(base, {"tests-": [{"name": "a"}], "m-": ["a"]}, {}, "L")
    assert out == {"tests": [{"name": "b"}], "m": {"b": 2}}
    out, _ = layers.deep_merge({}, {"gone-": [1]}, {}, "L")
    assert out == {}, "removing from nothing is nothing"


def test_question_sets_only_when_unset():
    out, prov = layers.deep_merge({"a": 1}, {"a?": 2, "b?": 3}, {}, "L")
    assert out == {"a": 1, "b": 3} and prov == {("b",): "L"}


def test_an_operator_on_the_wrong_shape_is_refused():
    with pytest.raises(layers.LayerError, match="`\\+` needs"):
        layers.deep_merge({"a": "x"}, {"a+": [1]}, {}, "L")
    with pytest.raises(layers.LayerError, match="`-` needs"):
        layers.deep_merge({"a": [1]}, {"a-": "x"}, {}, "L")


def test_operators_in_the_layouts_own_files_are_refused(tsite):
    site, lay = tsite
    _write(str(lay / "host_vars/b2.yml"), {"meshtastic": {"channels+": [1]}})
    with pytest.raises(layers.LayerError, match="layer files only"):
        layers.effective(str(lay), _cfg(lay), "b2")


# ---- board appends ------------------------------------------------------------------------------

def test_a_layers_board_file_appends_to_the_profile(tsite):
    site, lay = tsite
    _layer(site, "rigs", boards={"b1": {"meshtastic": {"config": {"device": {"role": "ROUTER"}},
                                                        "channels+": [{"index": 1, "name": "x"}]},
                                        "zep": {"cluster": "c1"}}})
    cfg = _cfg(lay)
    cfg["layers"].append("rigs")
    _write(str(lay / "host_vars/b1.yml"), {"owner": "b1"})   # no longer replaces `meshtastic`
    r = layers.resolve(str(lay), cfg)
    assert set(r["host_vars"]["b1"]) == {"meshtastic", "zep"}, "only the keys the append touches"
    v, prov = layers.effective(str(lay), cfg, "b1")
    assert v["meshtastic"]["config"]["lora"] == {"region": "US", "tx_power": 5}, "the profile's, kept"
    assert v["meshtastic"]["config"]["device"] == {"role": "ROUTER"}
    assert v["meshtastic"]["channels"] == [{"index": 1, "name": "x"}]
    assert prov[("meshtastic", "config", "device", "role")] == "layer rigs: boards/b1.yml"
    assert prov[("meshtastic", "config", "lora", "region")] == "layer base: profiles/xiao.yml"
    assert prov[("zep", "cluster")] == "layer rigs: boards/b1.yml"
    assert "b2" not in r["host_vars"]


# ---- layer.conf ---------------------------------------------------------------------------------

def test_a_layer_without_layer_conf_is_refused(tsite):
    site, lay = tsite
    os.remove(str(site / "layers/extra/layer.conf"))
    with pytest.raises(layers.LayerError, match="no layer.conf"):
        layers.resolve(str(lay), _cfg(lay))


def test_an_incompatible_schema_is_refused(tsite):
    site, lay = tsite
    _layer(site, "extra", schema=2)
    with pytest.raises(layers.LayerError, match="schema 2.*not compatible"):
        layers.resolve(str(lay), _cfg(lay))


def test_a_dependency_must_be_listed_first(tsite):
    site, lay = tsite
    _layer(site, "extra", depends=["base"])
    cfg = _cfg(lay)
    layers.resolve(str(lay), cfg)                      # base, extra: fine
    cfg["layers"] = ["extra", "base"]
    with pytest.raises(layers.LayerError, match="depends on layer 'base', which is not listed before"):
        layers.resolve(str(lay), cfg)
    cfg["layers"] = ["base", "base"]
    with pytest.raises(layers.LayerError, match="listed twice"):
        layers.resolve(str(lay), cfg)


# ---- from ---------------------------------------------------------------------------------------

@pytest.fixture
def two_layouts(tsite):
    """`lay` from the tsite fixture plus `other`, which takes b1 from lay and changes one thing."""
    site, lay = tsite
    other = site / "layouts/other"
    _write(str(other / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base"],
            "boards": {"b1": {"from": "lay/b1"}, "b9": {"from": "lay/b1"}}})
    _write(str(other / "group_vars/all.yml"), {"store": "/other"})
    _write(str(other / "host_vars/b9.yml"), {"firmware": {"family": "zephyrtastic", "class": 9}})
    return site, lay, other


def test_from_takes_the_boards_own_declaration_not_its_layouts_group_vars(two_layouts):
    site, lay, other = two_layouts
    v, prov = layers.effective(str(other), _cfg(other), "b1")
    # b1 in lay: profile xiao (both layers) + host_vars {meshtastic: {owner: b1}} (replaces whole)
    assert v["firmware"] == {"family": "zephyrtastic", "class": 1}
    assert v["meshtastic"] == {"owner": "b1"}
    assert prov[("meshtastic", "owner")] == "layout lay: host_vars/b1.yml (from lay/b1)"
    assert prov[("firmware", "class")] == "layer base: profiles/xiao.yml (from lay/b1)"
    # lay's group vars do not come along: other's own store, other's layer stack
    assert v["store"] == "/other" and v["builds"] == {"fw": {"version": "1", "ref": "main"}}
    # "the same except X": other's host_vars win over the from
    v9, _ = layers.effective(str(other), _cfg(other), "b9")
    assert v9["firmware"]["class"] == 9 and v9["meshtastic"] == {"owner": "b1"}


def test_from_refusals(two_layouts):
    site, lay, other = two_layouts
    cfg = _cfg(other)
    cfg["boards"]["b1"] = {"from": "lay/nope"}
    with pytest.raises(layers.LayerError, match="nope is not a board of layout lay"):
        layers.resolve(str(other), cfg)
    cfg["boards"]["b1"] = {"from": "nolayout/b1"}
    with pytest.raises(layers.LayerError, match="no layout 'nolayout'"):
        layers.resolve(str(other), cfg)
    cfg["boards"]["b1"] = {"from": "b1"}
    with pytest.raises(layers.LayerError, match="not <layout>/<board>"):
        layers.resolve(str(other), cfg)
    # a cycle: lay takes b1 from other, which takes it from lay
    lcfg = _cfg(lay)
    lcfg["boards"]["b1"] = {"from": "other/b1"}
    _write(str(lay / "devreg.yml"), lcfg)
    with pytest.raises(layers.LayerError, match="cycle"):
        layers.resolve(str(other), _cfg(other))


def test_a_familys_vars_reach_only_its_boards(tsite):
    """group_vars/family_<f>.yml in a layer: a board whose declared family is f gets it, composed
    over `all` and merged across layers; a board of another family does not; a key the profile
    also sets is the profile's whole."""
    site, lay = tsite
    _write(str(site / "layers/base/group_vars/family_zephyrtastic.yml"),
           {"image": "{{ store }}/class-{{ firmware.class }}", "tests": ["version"],
            "builds": {"fw": {"sign": "key"}}})
    _write(str(site / "layers/extra/group_vars/family_zephyrtastic.yml"), {"tests": ["version", "build"]})
    _write(str(site / "layers/extra/group_vars/family_stock.yml"), {"tests": []})
    _write(str(lay / "host_vars/b2.yml"), {"firmware": {"family": "stock", "image": "factory"}})
    cfg = _cfg(lay)
    v1, prov = layers.effective(str(lay), cfg, "b1")       # profile xiao: family zephyrtastic
    assert v1["image"] == "{{ store }}/class-{{ firmware.class }}"
    assert v1["tests"] == ["version", "build"]              # merged across the layers, extra wins
    assert v1["builds"] == {"fw": {"version": "2", "ref": "main", "sign": "key"}}   # composed over all
    assert prov[("image",)] == "layer base: group_vars/family_zephyrtastic.yml"
    assert prov[("tests",)] == "layer extra: group_vars/family_zephyrtastic.yml"
    assert prov[("builds", "fw", "ref")] == "layer base: group_vars/all.yml"
    assert prov[("builds", "fw", "sign")] == "layer base: group_vars/family_zephyrtastic.yml"
    v2, _ = layers.effective(str(lay), cfg, "b2")          # declared stock in its host_vars
    assert "image" not in v2 and v2["tests"] == []
    v3, _ = layers.effective(str(lay), cfg, "b3")
    assert v3["tests"] == ["version", "build"]
    # the profile's group outranks the family's: a key both set is the profile's, whole
    _write(str(site / "layers/base/profiles/xiao.yml"),
           {"firmware": {"family": "zephyrtastic", "class": 1}, "tests": ["mine"],
            "meshtastic": {"config": {"lora": {"region": "US", "tx_power": 2}}}})
    v1, prov = layers.effective(str(lay), cfg, "b1")
    assert v1["tests"] == ["mine"] and prov[("tests",)] == "layer base: profiles/xiao.yml"
    assert "family keys: zephyrtastic: builds, image, tests" in layers.describe(str(lay), cfg)
    # a layer's group_vars holds all.yml and family_<f>.yml, nothing else; no go-ahead there either
    _write(str(site / "layers/extra/group_vars/boards.yml"), {"x": 1})
    with pytest.raises(layers.LayerError, match="group_vars/boards.yml: a layer's group_vars are"):
        layers.resolve(str(lay), cfg)
    os.remove(site / "layers/extra/group_vars/boards.yml")
    _write(str(site / "layers/extra/group_vars/family_stock.yml"), {"bench_apply_allowed": True})
    with pytest.raises(layers.LayerError, match="family_stock.yml states bench_apply_allowed"):
        layers.resolve(str(lay), cfg)


def test_ansible_places_a_familys_vars_on_its_group(widget_site):
    site, lay = widget_site
    _write(str(site / "layers/base/group_vars/family_widget.yml"),
           {"fam_only": "Z", "shared": {"d": 4}, "both": "family"})
    h1, h2 = _inventory(lay, "sim1"), _inventory(lay, "sim2")
    for board, got in (("sim1", h1), ("sim2", h2)):
        assert got["fam_only"] == "Z", board
        assert got["both"] == "layout", board                     # the layout's file still wins
        want, _ = layers.effective(str(lay), _cfg(lay), board)
        assert {k: got.get(k) for k in want} == want, board
    # sim1: the profile's `shared` (a child group named later) replaces the family's whole;
    # sim2 has no profile and sees the family's, composed over all
    assert h1["shared"] == {"a": 1, "b": 20, "c": 30}
    assert h2["shared"] == {"a": 1, "b": 2, "d": 4}


def test_a_variant_takes_the_rest_of_a_layouts_boards(two_layouts):
    """`from: lay` at the top: lay's boards this layout does not list come along as declared
    there, on lay's host for them; the listed ones are this layout's own."""
    site, lay, other = two_layouts
    lcfg = _cfg(lay)
    lcfg["boards"]["b2"] = {"host": "pi1"}
    _write(str(lay / "devreg.yml"), lcfg)
    var = site / "layouts/var"
    _write(str(var / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base"], "from": "lay",
            "boards": {"b3": {"profile": "xiao"}}})
    _write(str(var / "group_vars/all.yml"), {"store": "/var", "bench_apply_allowed": True})
    _write(str(var / "host_vars/b3.yml"), {"meshtastic": {"owner": "var"}})
    cfg = layers.expand(str(var), _cfg(var))
    assert cfg["boards"] == {"b1": {"from": "lay/b1"}, "b2": {"host": "pi1", "from": "lay/b2"},
                             "b3": {"profile": "xiao"}}
    v1, prov = layers.effective(str(var), _cfg(var), "b1")
    assert v1["meshtastic"] == {"owner": "b1"}          # lay's host_vars for b1, through the from
    assert v1["store"] == "/var"                        # the variant's own group vars
    assert prov[("meshtastic", "owner")] == "layout lay: host_vars/b1.yml (from lay/b1)"
    v3, _ = layers.effective(str(var), _cfg(var), "b3")
    assert v3["meshtastic"] == {"owner": "var"}         # listed: the variant's own declaration
    assert "(from)" in layers.describe(str(var), _cfg(var))
    # a chain: a variant of the variant takes everything, including what var changed
    var2 = site / "layouts/var2"
    _write(str(var2 / "devreg.yml"), {"plugin": "rxrbench.core.devreg", "layers": ["base"],
                                      "from": "var", "boards": {}})
    assert set(layers.expand(str(var2), _cfg(var2))["boards"]) == {"b1", "b2", "b3"}
    v3b, _ = layers.effective(str(var2), _cfg(var2), "b3")
    assert v3b["meshtastic"] == {"owner": "var"}
    # refusals: a board path, an unknown layout, itself, a cycle
    for bad, msg in (("lay/b1", "must name a layout"), ("nolayout", "no layout 'nolayout'"),
                     ("var", "is itself")):
        with pytest.raises(layers.LayerError, match=msg):
            layers.expand(str(var), {**_cfg(var), "from": bad})
    _write(str(lay / "devreg.yml"), {**lcfg, "from": "var"})
    with pytest.raises(layers.LayerError, match="cycle"):
        layers.expand(str(var), _cfg(var))


def test_a_layouts_sibling_layers_come_first(tmp_path, monkeypatch):
    """An example layout under <root>/layouts resolves <root>/layers before the site's, so a
    site layer of the same name does not shadow the example's."""
    site = tmp_path / "site"
    monkeypatch.setenv("RXRBENCH_SITE", str(site))
    os.makedirs(site, exist_ok=True)
    (site / "rf_rules.yml").write_text("region: US\nmax_tx_power: 2\n")   # a quiet bench in the US
    _layer(site, "base", group_vars={"who": "site"})
    ex = tmp_path / "example"
    _layer(ex, "base", group_vars={"who": "example"})
    lay = ex / "layouts/l"
    _write(str(lay / "devreg.yml"), {"plugin": "x", "layers": ["base"], "boards": {"b": {}}})
    assert layers.resolve(str(lay), _cfg(lay))["all_vars"] == {"who": "example"}
    lay2 = site / "layouts/l"
    _write(str(lay2 / "devreg.yml"), {"plugin": "x", "layers": ["base"], "boards": {"b": {}}})
    assert layers.resolve(str(lay2), _cfg(lay2))["all_vars"] == {"who": "site"}


# ---- the order Ansible applies to appends and from --------------------------------------------

def test_ansible_places_appends_above_group_vars_and_below_host_vars(widget_site):
    site, lay = widget_site
    _layer(site, "rigs", boards={"sim1": {"both": "append", "shared2": {"q": 9}, "fresh": "A"}})
    cfg = _cfg(lay)
    cfg["layers"].append("rigs")
    _write(str(lay / "devreg.yml"), cfg)
    h1 = _inventory(lay, "sim1")
    assert h1["both"] == "append", "an append beats the layout's group_vars file"
    assert h1["shared2"] == {"z": 1}, "the layout's host_vars beats an append"
    assert h1["fresh"] == "A"
    want, _ = layers.effective(str(lay), cfg, "sim1")
    assert {k: h1.get(k) for k in want} == want
    # from: a second layout takes sim2 from this one
    other = site / "layouts/u"
    _write(str(other / "devreg.yml"),
           {"plugin": "rxrbench.core.devreg", "layers": ["base"], "registry": "../../registry.yaml",
            "catalog_dir": "../../catalog", "boards": {"sim2": {"host": "work", "from": "t/sim2"}}})
    _write(str(other / "group_vars/all.yml"), {"bench_apply_allowed": False,
                                                "ansible_python_interpreter": "/usr/bin/python3"})
    h2 = _inventory(other, "sim2")
    assert h2["firmware"] == {"family": "widget", "image": "w1"}
    assert "both" not in h2 or h2["both"] == "layer", "t's group_vars did not come along"
    want, _ = layers.effective(str(other), _cfg(other), "sim2")
    assert {k: h2.get(k) for k in want} == want


@pytest.mark.plugins
def test_a_plain_profile_cannot_also_declare_a_role(ansible_site, monkeypatch):
    """`intents: none` beside a brain, head, listener or cluster is a contradiction the layout
    refuses: a plain node remembers no role, and an apply would clear then set, every time."""
    from ansible_collections.rxrbench.core.plugins.module_utils import layout
    site, lay = ansible_site
    monkeypatch.setenv("RXRBENCH_LOCATION_DIR", "/nonexistent")
    _write(str(site / "layers/base/profiles/sim.yml"),
           {"firmware": {"family": "zephyrtastic", "class": 0},
            "zephyrtastic": {"intents": "none", "listener": {"presets": ["MEDIUM_FAST"]}},
            "meshtastic": {"config": {"lora": {"region": "US", "modem_preset": "SHORT_FAST",
                                               "tx_power": 2, "tx_enabled": True}}}})
    with pytest.raises(layout.LayoutError, match="intents: none and a role \\(listener\\)"):
        layout.load_layout(str(lay), _cfg(lay))
