"""The meshtastic family's release map: what a release's full install writes beside its merged
image, read from the `.mt.json` the web flasher reads (families/meshtastic.py)."""
import json

from ansible_collections.rxrbench.mesh.plugins.plugin_utils.families import meshtastic

FAMILY = meshtastic.Family()
MT = {"files": [
    {"name": "fw-kit-a-1.0.elf", "md5": "e" * 32},
    {"name": "fw-kit-a-1.0.bin", "md5": "a" * 32, "part_name": "app0"},
    {"name": "fw-kit-a-1.0.factory.bin", "md5": "f" * 32},
    {"name": "littlefs-kit-a-1.0.bin", "md5": "1" * 32, "part_name": "spiffs"},
    {"name": "mt-esp32s3-ota.bin", "md5": "2" * 32, "part_name": "app1"}],
    "part": [{"name": "app0", "subtype": "ota_0"}, {"name": "app1", "subtype": "ota_1"},
             {"name": "spiffs", "subtype": "spiffs"}]}


def _reader(files):
    return lambda name: files.get(name)


def test_the_map_names_the_ota_loader_and_the_file_system_but_not_the_app_the_factory_file_holds():
    got = FAMILY.release_partitions("fw-kit-a-1.0.factory.bin",
                                    _reader({"fw-kit-a-1.0.mt.json": json.dumps(MT).encode()}))
    assert got == [{"partition": "spiffs", "member": "littlefs-kit-a-1.0.bin", "md5": "1" * 32},
                   {"partition": "app1", "member": "mt-esp32s3-ota.bin", "md5": "2" * 32}]


def test_a_table_that_names_the_second_slot_otherwise_is_matched_by_subtype():
    """The SenseCAP Indicator's table calls app1 `flashApp`; the flasher finds it as ota_1."""
    mt = {**MT, "part": [{"name": "app0"}, {"name": "flashApp", "subtype": "ota_1"}, {"name": "spiffs"}]}
    got = FAMILY.release_partitions("fw-kit-a-1.0.factory.bin",
                                    _reader({"fw-kit-a-1.0.mt.json": json.dumps(mt).encode()}))
    assert [r["partition"] for r in got] == ["spiffs", "ota_1"]


def test_members_in_a_folder_of_the_zip_are_found_beside_the_factory_file():
    got = FAMILY.release_partitions("rel/fw-kit-a-1.0.factory.bin",
                                    _reader({"rel/fw-kit-a-1.0.mt.json": json.dumps(MT).encode()}))
    assert {r["member"] for r in got} == {"rel/littlefs-kit-a-1.0.bin", "rel/mt-esp32s3-ota.bin"}


def test_no_map_or_no_factory_file_means_nothing_added():
    assert FAMILY.release_partitions("fw-kit-a-1.0.factory.bin", _reader({})) == []
    assert FAMILY.release_partitions("fw-kit-a-1.0.bin", _reader({"fw-kit-a-1.0.mt.json": b"{}"})) == []
