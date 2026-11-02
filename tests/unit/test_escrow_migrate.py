"""tools/escrow_migrate.py: an escrow file's identity records moved under secrets.<family>."""
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("escrow_migrate", os.path.join(REPO, "tools", "escrow_migrate.py"))
em = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(em)


def test_an_identity_record_moves_and_reads_back_as_it_was_written():
    old = {"board": "b1", "fam": {"node_id": "0x01", "private_key": "KEY", "public_key_fp": "ab",
                                  "exported": "T"},
           "secrets": {"lin": {"host_key": {"id": "h", "value": "V"}}}}
    new, moved, left = em.migrated(old)
    assert moved == ["fam"] and left == []
    assert new == {"board": "b1", "secrets": {
        "lin": {"host_key": {"id": "h", "value": "V"}},
        "fam": {"identity": {"id": "0x01", "value": "KEY", "fingerprint": "ab", "exported": "T"}}}}
    assert em.migrated(new) == (new, [], []), "a file in the one shape is left as it is"


def test_a_record_that_is_not_an_identity_is_left_where_it_is():
    old = {"fam": {"bridge_secret": "S", "read_at": "T"}}
    new, moved, left = em.migrated(old)
    assert new == old and moved == [] and left == ["fam"]
