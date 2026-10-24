"""Canonical import naming is verified, optional, and never renames the source."""
import hashlib
import zipfile
from xml.sax.saxutils import quoteattr

import pytest

from romarr.dat import DatIndex, parse_dat
from romarr.library import import_rom
from romarr.platforms import by_slug


BODY = b"known-rom-data" * 160


def dats_for(roms):
    rows = []
    for name, body in roms:
        rows.append("<rom name=" + quoteattr(name) + " size=" + quoteattr(str(len(body)))
                    + " sha1=" + quoteattr(hashlib.sha1(body).hexdigest()) + "/>")
    xml = ('<datafile><header><name>Naming</name></header>'
           '<game name="Canonical Game (USA)">' + "".join(rows)
           + '</game></datafile>')
    index = DatIndex()
    index.add(parse_dat(xml))
    return index


def test_verified_import_gets_dat_name_and_keeps_source_format_and_bytes(tmp_path):
    source = tmp_path / "mislabeled.smc"
    source.write_bytes(BODY)
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library",
                          dats=dats_for([("Canonical Game (USA).sfc", BODY)]),
                          rename=True)
    assert outcome.ok
    assert outcome.destination.name == "Canonical Game (USA).smc"
    assert outcome.destination.read_bytes() == BODY
    assert source.read_bytes() == BODY


def test_naming_can_be_disabled(tmp_path):
    source = tmp_path / "my preferred name.sfc"
    source.write_bytes(BODY)
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library",
                          dats=dats_for([("Canonical Game (USA).sfc", BODY)]),
                          rename=False)
    assert outcome.ok
    assert outcome.destination.name == source.name
    assert source.exists()


def test_unknown_dump_keeps_its_name(tmp_path):
    source = tmp_path / "Homebrew.sfc"
    source.write_bytes(BODY)
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library", rename=True)
    assert outcome.ok
    assert outcome.destination.name == source.name


@pytest.mark.parametrize("canonical", [
    "../outside.sfc", "/outside.sfc", "dir/outside.sfc",
    "C:" + chr(92) + "outside.sfc", "CON.sfc", "Game?.sfc",
])
def test_unsafe_dat_name_does_not_escape_or_break_filename(tmp_path, canonical):
    source = tmp_path / "safe.sfc"
    source.write_bytes(BODY)
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library",
                          dats=dats_for([(canonical, BODY)]), rename=True)
    assert outcome.ok
    assert outcome.destination.name == "safe.sfc"
    assert outcome.destination.parent == tmp_path / "library" / "snes"
    assert source.read_bytes() == BODY


def test_existing_canonical_destination_is_not_overwritten(tmp_path):
    source = tmp_path / "alias.sfc"
    source.write_bytes(BODY)
    target = tmp_path / "library" / "snes" / "Canonical Game (USA).sfc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep existing")
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library",
                          dats=dats_for([("Canonical Game (USA).sfc", BODY)]), rename=True)
    assert not outcome.ok
    assert target.read_bytes() == b"keep existing"
    assert source.read_bytes() == BODY


def test_archive_member_uses_verified_name_without_changing_archive(tmp_path):
    source = tmp_path / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("folder/alias.sfc", BODY)
    before = source.read_bytes()
    outcome, = import_rom(source, by_slug("snes"), tmp_path / "library",
                          dats=dats_for([("Canonical Game (USA).sfc", BODY)]), rename=True)
    assert outcome.ok
    assert outcome.destination.name == "Canonical Game (USA).sfc"
    assert outcome.destination.read_bytes() == BODY
    assert source.read_bytes() == before


def test_disc_member_names_and_cue_references_are_preserved(tmp_path):
    source = tmp_path / "downloads"
    source.mkdir()
    cue = b'FILE "Alias (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    (source / "Alias.cue").write_bytes(cue)
    (source / "Alias (Track 1).bin").write_bytes(BODY)
    index = dats_for([("Canonical.cue", cue), ("Canonical (Track 1).bin", BODY)])
    outcome, = import_rom(source, by_slug("psx"), tmp_path / "library",
                          dats=index, rename=True)
    assert outcome.ok
    assert outcome.destination.name == "Alias"
    assert (outcome.destination / "Alias.cue").read_bytes() == cue
    assert (outcome.destination / "Alias (Track 1).bin").read_bytes() == BODY
    assert (source / "Alias.cue").read_bytes() == cue


@pytest.mark.parametrize("rename", [True, False])
@pytest.mark.parametrize("mode", ["manual", "download"])
def test_saved_naming_setting_is_honored_by_both_import_paths(tmp_path, rename, mode):
    from romarr.app import ROMarr, QueueItem

    library = tmp_path / "library"
    library.mkdir()
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                       "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(library)})
    service.store.put_item("libraries", {"type": "folder", "name": "Shelf",
                           "enable": True, "path": str(library), "is_default": True})
    service.reload_libraries()
    service.store.settings["rename_on_import"] = rename
    service.dats = dats_for([("Canonical Game (USA).sfc", BODY)])
    source = tmp_path / "alias.sfc"
    source.write_bytes(BODY)
    if mode == "manual":
        result = service.adopt(str(source), "snes")
        assert result["ok"], result
    else:
        job_hash = "a" * 40
        item = QueueItem("Game", "snes", "indexer title", 1, "grabbed",
                         release_id="btih:" + job_hash)
        service.queue.append(item)

        class Client:
            configured = True

            def completed(self):
                return [{"hash": job_hash, "name": "torrent title",
                         "content_path": str(source)}]

        service.clients = [Client()]
        result = service.import_finished()
        assert item.state == "imported", result
    expected = "Canonical Game (USA).sfc" if rename else "alias.sfc"
    assert (library / "snes" / expected).read_bytes() == BODY
    assert source.read_bytes() == BODY
