import zipfile
from types import SimpleNamespace
import pytest
from romarr.import_intent import preview_import, choose_import
from romarr.library import import_rom
from romarr.platforms import resolve
from romarr.app import ROMarr
from romarr.store import QueueItem, Store


def archive(tmp_path, members):
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_switch_same_extension_roles_require_manual_review(tmp_path):
    path = archive(tmp_path, {"Game.nsp": b"base", "Game update.nsp": b"update", "Game DLC.nsp": b"dlc", "Emulator.exe": b"extra"})
    p = preview_import(path, resolve("switch"), tmp_path / "lib")
    assert p["requires_review"]
    assert len(p["items"]) == 3
    assert all(i["role"] == "unknown" and not i["selected"] for i in p["items"])
    assert p["extras"] == ["Emulator.exe"]
    with pytest.raises(ValueError, match="Classify"):
        choose_import(p, [{"primary": "Game.nsp", "role": "unknown"}])
    with pytest.raises(ValueError, match="exactly one base"):
        choose_import(p, [{"primary": "Game update.nsp", "role": "update"}])
    intent = choose_import(p, [{"primary": "Game.nsp", "role": "base"}, {"primary": "Game DLC.nsp", "role": "dlc"}])
    outcomes = import_rom(path, resolve("switch"), tmp_path / "lib", selected_primaries=[c["primary"] for c in intent["choices"]])
    assert all(o.ok for o in outcomes)
    assert (tmp_path / "lib" / "switch" / "Game.nsp").read_bytes() == b"base"
    assert not (tmp_path / "lib" / "switch" / "Game update.nsp").exists()
    assert path.exists()


def test_companions_cannot_be_separated_and_extras_excluded(tmp_path):
    path = archive(tmp_path, {"Game.cue": b'FILE "track.bin" BINARY\n', "track.bin": b"bytes", "manual.pdf": b"manual", "art.jpg": b"art"})
    p = preview_import(path, resolve("psx"), tmp_path / "lib")
    assert len(p["items"]) == 1
    assert set(p["items"][0]["members"]) == {"Game.cue", "track.bin"}
    assert p["items"][0]["bytes"] == 29
    with pytest.raises(ValueError, match="no longer matches"):
        choose_import(p, [{"primary": "track.bin", "role": "base"}])


def test_multigame_requires_explicit_collection_mode(tmp_path):
    path = archive(tmp_path, {"A.gb": b"a", "B.gb": b"b"})
    p = preview_import(path, resolve("gb"), tmp_path / "lib")
    assert p["requires_review"] and not any(i["selected"] for i in p["items"])
    choices = [{"primary": "A.gb", "role": "base"}, {"primary": "B.gb", "role": "additional-game"}]
    with pytest.raises(ValueError):
        choose_import(p, choices)
    assert choose_import(p, choices, "collection")["mode"] == "collection"


def test_preview_token_changes_when_source_changes(tmp_path):
    path = archive(tmp_path, {"A.gb": b"a"})
    old = preview_import(path, resolve("gb"), tmp_path / "lib")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("B.gb", b"b")
    assert preview_import(path, resolve("gb"), tmp_path / "lib")["token"] != old["token"]


def test_pc_installer_remains_required(tmp_path):
    path = archive(tmp_path, {"setup.exe": b"exe", "setup-1.bin": b"data"})
    p = preview_import(path, resolve("win"), tmp_path / "lib")
    assert "setup.exe" in p["items"][0]["members"]


def test_service_stops_bundle_until_review_then_persists_and_retains_source(tmp_path, monkeypatch):
    path = archive(tmp_path, {"A.gb": b"a" * 32768, "B.gb": b"b" * 32768})
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    item = QueueItem("A", "gb", "bundle", 1, "grabbed", download_id="qbit-tag:job")
    service.queue.append(item)
    removed = []
    client = SimpleNamespace(configured=True, completed=lambda: [{"name": "bundle", "id": "qbit-tag:job", "content_path": str(path)}], remove_download=lambda *a, **k: removed.append(a))
    service.clients = [client]
    cfg = {"path": str(tmp_path / "lib")}
    monkeypatch.setattr(service, "library_for", lambda slug: (cfg, SimpleNamespace(rescan=lambda slug: None)))
    monkeypatch.setattr(service, "_virus_check", lambda *a: None)
    service.store.settings["reconcile_after_import"] = False
    assert service.import_finished()[0]["ok"] is False
    assert item.state == "import-review" and not (tmp_path / "lib").exists()
    p = service.queue_import_plan(0, {})["preview"]
    result = service.queue_import_plan(0, {"confirm": True, "token": p["token"], "choices": [{"primary": "A.gb", "role": "base"}]})
    assert result["ok"]
    loaded = Store(tmp_path / "state.json")
    assert loaded.queue[0].import_intent["choices"] == [{"primary": "A.gb", "role": "base"}]
    assert service.import_finished()[0]["ok"]
    assert item.state == "imported"
    assert path.exists() and not removed
    assert (tmp_path / "lib" / "gb" / "A.gb").exists()
    assert not (tmp_path / "lib" / "gb" / "B.gb").exists()


def test_selected_member_missing_fails_without_publishing(tmp_path):
    path = archive(tmp_path, {"A.gb": b"a"})
    outcomes = import_rom(path, resolve("gb"), tmp_path / "lib", selected_primaries=["missing.gb"])
    assert not outcomes[0].ok and not (tmp_path / "lib").exists()
