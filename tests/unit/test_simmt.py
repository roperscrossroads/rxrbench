"""The stock Meshtastic sim's lifecycle (mesh/sim/stock/simmt.py): `down` stops nodes and keeps
their config, and `down --erase` (what the sim test's teardown runs) leaves nothing behind."""
from ansible_collections.rxrbench.mesh.sim.stock import simmt


def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(simmt, "docker", lambda *a, **k: seen.append(a))
    monkeypatch.setattr(simmt, "nodes", lambda: {"mt1": 4403, "mt2": 4404})
    return seen


def test_down_stops_every_node_and_keeps_its_config(monkeypatch):
    seen = calls(monkeypatch)
    simmt.down()
    assert seen == [("rm", "-f", "rxrbench-sim-mt1"), ("rm", "-f", "rxrbench-sim-mt2")]


def test_down_erase_of_one_node_removes_its_container_and_volume_alone(monkeypatch):
    seen = calls(monkeypatch)
    simmt.down(["mt1"], erase=True)
    assert seen == [("rm", "-f", "rxrbench-sim-mt1"), ("volume", "rm", "-f", "rxrbench-sim-mt1")]


def test_the_command_line_takes_names_and_erase(monkeypatch):
    seen = calls(monkeypatch)
    monkeypatch.setattr("sys.argv", ["simmt.py", "down", "mt2", "--erase"])
    simmt.main()
    assert seen == [("rm", "-f", "rxrbench-sim-mt2"), ("volume", "rm", "-f", "rxrbench-sim-mt2")]
