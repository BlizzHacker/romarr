from dataclasses import replace
import pytest
from romarr.selection import Release, judge, collection_content_kind
from romarr.platforms import resolve
from romarr.profiles import ReleaseProfile


def release(path, title="Fixture Game (2012)(Publisher)(US).zip"):
    return Release(title, 0, 1, (1000,), "magnet:?xt=urn:btih:" + "a"*40,
                   "torrent", indexer="hub:minerva-archive", torrent_file_path=path,
                   file_selection_required=True)


@pytest.mark.parametrize("path,kind", [
    ("./TOSEC-PIX/Nintendo/Wii U/Manuals/Games/game.zip", "manual"),
    ("./TOSEC-PIX/Nintendo/Wii U/Commercials/Games/game.zip", "commercial"),
    (r".\tOsEc-PiX\Nintendo\Wii U\mAnUaLs\Games\game.zip", "manual"),
    ("// TOSEC_PIX /Nintendo/Wii U/Screenshots/Game.zip", "associated-media"),
    ("TOSEC/Nintendo/Wii U/Manuals/Games/game.zip", "manual"),
    ("TOSEC/Nintendo/Wii U/Commercials/Games/game.zip", "commercial"),
])
def test_known_non_game_hierarchies_are_refused(path, kind):
    item = release(path)
    assert collection_content_kind(path) == kind
    result = judge(item, "Fixture Game", resolve("wiiu"))
    assert not result.accepted
    assert "non-game collection content" in result.verdict
    assert not any("ROM extension" in reason for _, reason in result.reasons)


@pytest.mark.parametrize("path,title,wanted", [
    ("./TOSEC/Nintendo/Wii U/Games/Fixture Game.zip", "Fixture Game (US).zip", "Fixture Game"),
    ("./TOSEC/Nintendo/Wii U/Games/Manual Samuel.zip", "Manual Samuel (US).zip", "Manual Samuel"),
    ("./TOSEC/Nintendo/Wii U/Games/Commercial Break.zip", "Commercial Break (US).zip", "Commercial Break"),
    ("./TOSEC/Nintendo/Wii U/Games/Manuals.zip", "Manuals (US).zip", "Manuals"),
])
def test_game_zip_unknown_size_and_title_words_stay_eligible(path, title, wanted):
    item = release(path, title)
    assert collection_content_kind(path) == ""
    result = judge(item, wanted, resolve("wiiu"))
    assert result.accepted, result.why()
    assert item.size == 0


def test_explicit_adapter_content_kind_cannot_score_as_game():
    item = replace(release(""), content_kind="manual")
    assert "non-game" in judge(item, "Fixture Game", resolve("wiiu")).verdict


def test_profile_exclusions_use_path_but_required_terms_still_use_title():
    item = release("TOSEC/Nintendo/Wii U/UnwantedBranch/Game.zip")
    profile = ReleaseProfile.from_settings({"excluded_terms": ["UnwantedBranch"]}, resolve("wiiu"))
    assert "excluded by release profile" in judge(item, "Fixture Game", resolve("wiiu"), profile=profile).verdict
    required = ReleaseProfile.from_settings({"required_terms": ["Wii U"]}, resolve("wiiu"))
    assert "missing a required term" in judge(item, "Fixture Game", resolve("wiiu"), profile=required).verdict


def test_hub_adapter_carries_refusal_and_search_disables_grab(tmp_path, monkeypatch):
    from romarr.app import ROMarr
    from romarr import hub
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    row = {"plugin": "minerva-archive", "title": "Fixture Game (US).zip",
           "magnet": "magnet:?xt=urn:btih:"+"a"*40,
           "extra": {"path": "TOSEC-PIX/Nintendo/Wii U/Manuals/Games/Fixture Game.zip"}}
    monkeypatch.setattr(service, "_hub_enabled", lambda: True)
    monkeypatch.setattr(hub, "search", lambda *a, **k: ([row], ""))
    monkeypatch.setattr(hub, "resolve_torrents", lambda rows: rows)
    items = service._hub_releases("Fixture Game", resolve("wiiu"))
    assert len(items) == 1 and items[0].content_kind == "manual"
    monkeypatch.setattr(service, "_search_releases", lambda *args: items)
    result = service.candidates("Fixture Game", "wiiu")["items"][0]
    assert not result["accepted"] and not result["grabbable"]
    assert "non-game" in " ".join(result["reasons"])
    assert not service.grab(items[0], "Fixture Game", "wiiu", manual=True)["ok"]
    assert not service.queue
