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


def test_review_pending_reserves_automatic_grabs(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.queue.append(QueueItem("A", "gb", "bundle", 1, "import-review"))
    assert service._reserve_automatic_grab("A", "gb") is None


def test_bad_archive_does_not_abort_next_completed_import(tmp_path, monkeypatch):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"broken archive")
    good = tmp_path / "Good.gb"
    good.write_bytes(b"x" * 32768)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.queue.extend([QueueItem("Bad", "gb", "Bad", 1, "grabbed"), QueueItem("Good", "gb", "Good", 1, "grabbed")])
    service.clients = [SimpleNamespace(configured=True, completed=lambda: [{"name": "Bad", "content_path": str(bad)}, {"name": "Good", "content_path": str(good)}])]
    cfg = {"path": str(tmp_path / "lib")}
    monkeypatch.setattr(service, "library_for", lambda slug: (cfg, SimpleNamespace(rescan=lambda slug: None)))
    monkeypatch.setattr(service, "_virus_check", lambda *a: None)
    service.store.settings["reconcile_after_import"] = False
    results = service.import_finished()
    assert [r["ok"] for r in results] == [False, True]
    assert service.queue[0].state == "import-failed"
    assert "preview" in service.queue[0].detail
    assert bad.exists()


def test_http_preview_is_read_only_and_confirm_survives_restart(tmp_path, monkeypatch):
    """Verify HTTP queue preview is read-only and saved import intent survives restarts."""
    from romarr.app import make_handler
    from http.server import ThreadingHTTPServer
    import threading
    import requests
    path = archive(tmp_path, {"A.gb": b"a" * 32768, "B.gb": b"b" * 32768})
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "testkey"})
    service.queue.append(QueueItem("A", "gb", "bundle", 1, "grabbed"))
    service.clients = [SimpleNamespace(configured=True, completed=lambda: [{"name": "bundle", "content_path": str(path)}])]
    cfg = {"path": str(tmp_path / "lib")}
    monkeypatch.setattr(service, "library_for", lambda slug: (cfg, SimpleNamespace(rescan=lambda slug: None)))
    service.store.save()
    original = (tmp_path / "state.json").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/v1/queue/import-plan"
    headers = {"X-Api-Key": "testkey"}
    try:
        assert requests.get(base.rsplit("/", 1)[0], headers=headers).json()["items"][0]["can_preview_import"]
        response = requests.get(base, params={"index": 0}, headers=headers)
        assert response.status_code == 200
        plan = response.json()["preview"]
        assert not (tmp_path / "lib").exists()
        assert (tmp_path / "state.json").read_bytes() == original
        body = {"index": 0, "confirm": True, "token": "stale", "choices": [{"primary": "A.gb", "role": "base"}]}
        assert "error" in requests.post(base, json=body, headers=headers).json()
        assert (tmp_path / "state.json").read_bytes() == original
        # A queue index can shift while the preview is open. Another row can
        # refer to the same completed bytes, so source metadata alone is unsafe.
        first = service.queue[0]
        service.queue[0] = QueueItem("Different requested game", "gb", "bundle", 1, "grabbed")
        body["token"] = plan["token"]
        assert "error" in requests.post(base, json=body, headers=headers).json()
        assert not service.queue[0].import_intent
        assert (tmp_path / "state.json").read_bytes() == original
        service.queue[0] = first
        body["token"] = plan["token"]
        assert requests.post(base, json=body, headers=headers).json()["ok"]
        loaded = Store(tmp_path / "state.json")
        assert loaded.queue[0].import_intent["choices"] == body["choices"]
        service.queue[0].state = "import-failed"
        assert service.queue_action(0, "retry")["import_retry"]
        assert len(service.queue) == 1 and service.queue[0].import_intent["choices"] == body["choices"]
    finally:
        server.shutdown()
        server.server_close()


# --- NUP containers: the role comes from the header, not the name ----------

def _nup(tmp_path, name, packages):
    """Write a NUP container to `tmp_path/name` and return its path."""
    from tests.nup_builder import build_nup

    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_nup(packages))
    return path


def test_a_loose_nup_base_is_read_from_its_header(tmp_path):
    from tests.nup_builder import KIND_UPDATE, Pkg

    path = _nup(tmp_path, "Game.nup", [Pkg(0x0100ABCD12340000), Pkg(0x0100ABCD12340000, KIND_UPDATE, 0x10000)])
    p = preview_import(path, resolve("switch"), tmp_path / "lib")
    item = p["items"][0]
    assert (item["role"], item["confidence"]) == ("base", "header")
    assert "base+update" in item["evidence"] and "0100ABCD12340000" in item["evidence"]
    assert item["selected"] and not p["requires_review"]


@pytest.mark.parametrize(("kind", "role"), [(1, "update"), (2, "dlc")])
def test_a_loose_nup_update_or_dlc_gets_its_role(tmp_path, kind, role):
    from tests.nup_builder import FLAG_STANDALONE_UPDATE, Pkg

    flags = FLAG_STANDALONE_UPDATE if role == "update" else 0
    path = _nup(tmp_path, "Game.nup", [Pkg(0x0100ABCD12341001, kind, 0x10000, flags)])
    item = preview_import(path, resolve("switch"), tmp_path / "lib")["items"][0]
    assert (item["role"], item["confidence"]) == (role, "header")


@pytest.mark.parametrize("blob", [b"", b"NUP\x01" + b"\x00" * 300], ids=["empty", "truncated"])
def test_an_unreadable_nup_requires_review(tmp_path, blob):
    path = tmp_path / "Game.nup"
    path.write_bytes(blob)
    p = preview_import(path, resolve("switch"), tmp_path / "lib")
    assert p["items"][0]["role"] == "unknown"
    assert not p["items"][0]["selected"]
    assert p["requires_review"]


def test_a_system_only_nup_requires_review(tmp_path):
    from tests.nup_builder import KIND_SYSTEM, Pkg

    path = _nup(tmp_path, "Firmware.nup", [Pkg(0x0100000000000809, KIND_SYSTEM)])
    p = preview_import(path, resolve("switch"), tmp_path / "lib")
    assert p["items"][0]["role"] == "unknown" and p["requires_review"]


def test_a_nup_folder_is_classified_for_review(tmp_path):
    """Several sets still go to review, but each arrives correctly labelled."""
    from tests.nup_builder import FLAG_STANDALONE_UPDATE, KIND_ADDON, KIND_UPDATE, Pkg

    folder = tmp_path / "Game"
    _nup(folder, "Game.nup", [Pkg(0x0100ABCD12340000)])
    _nup(folder, "Game update.nup", [Pkg(0x0100ABCD12340000, KIND_UPDATE, 0x30000, FLAG_STANDALONE_UPDATE)])
    _nup(folder, "Game DLC.nup", [Pkg(0x0100ABCD12341001, KIND_ADDON)])
    p = preview_import(folder, resolve("switch"), tmp_path / "lib")
    assert p["requires_review"]
    assert {i["primary"]: i["role"] for i in p["items"]} == {
        "Game.nup": "base", "Game update.nup": "update", "Game DLC.nup": "dlc"}
    choose_import(p, [{"primary": i["primary"], "role": i["role"]} for i in p["items"]])


def test_a_nup_inside_an_archive_is_not_opened(tmp_path):
    from tests.nup_builder import Pkg, build_nup

    path = archive(tmp_path, {"Game.nup": build_nup([Pkg(0x0100ABCD12340000)])})
    item = preview_import(path, resolve("switch"), tmp_path / "lib")["items"][0]
    assert (item["role"], item["confidence"]) == ("base", "format-only")
