"""GET/PUT /api/v1/config must never return a credential."""

from __future__ import annotations

import json

from romarr.app import MASKED_ITEM_FIELDS, SECRET_MASK
from romarr.app import mask_item


def test_mask_item_masks_only_declared_fields():
    item = {"id": "1", "name": "IGDB", "client_id": "abc", "token": "tok", "enable": True}
    got = mask_item(item, MASKED_ITEM_FIELDS["metadata_providers"])
    assert got["token"] == SECRET_MASK and got["client_id"] == SECRET_MASK
    assert got["name"] == "IGDB" and got["enable"] is True


def test_empty_secret_stays_empty_not_masked():
    got = mask_item({"token": ""}, ("token",))
    assert got["token"] == ""


def test_notification_urls_are_secrets():
    assert "url" in MASKED_ITEM_FIELDS["connections"]


# ---- the settings endpoint itself --------------------------------------------

def _service(tmp_path):
    from romarr.app import ROMarr
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})


def test_safe_settings_masks_items_and_secret_looking_keys(tmp_path):
    svc = _service(tmp_path)
    svc.store.settings["metadata_providers"] = [
        {"id": "1", "type": "igdb", "client_id": "cid", "token": "tok-SECRET-1"}]
    svc.store.settings["connections"] = [{"id": "2", "type": "discord", "url": "https://hook/x"}]
    svc.store.settings["tracker_passkey"] = "abc"
    out = svc.safe_settings()
    assert out["metadata_providers"][0]["token"] == SECRET_MASK
    assert out["metadata_providers"][0]["type"] == "igdb"
    assert out["connections"][0]["url"] == SECRET_MASK
    assert out["tracker_passkey"] == SECRET_MASK
    assert "tok-SECRET-1" not in json.dumps(out) and "hook/x" not in json.dumps(out)


def test_a_masked_echo_never_overwrites_a_stored_secret(tmp_path):
    svc = _service(tmp_path)
    svc.store.settings["metadata_providers"] = [
        {"id": "1", "type": "igdb", "client_id": "cid", "token": "tok-SECRET-1"}]
    svc.store.settings["tracker_passkey"] = "abc"
    echoed = svc.safe_settings()
    patch = svc.restore_masked_items({
        "metadata_providers": echoed["metadata_providers"],
        "tracker_passkey": echoed["tracker_passkey"]})
    assert patch["metadata_providers"][0]["token"] == "tok-SECRET-1"
    assert patch["metadata_providers"][0]["client_id"] == "cid"
    assert patch["tracker_passkey"] == "abc"


def test_a_new_secret_value_is_kept(tmp_path):
    svc = _service(tmp_path)
    svc.store.settings["metadata_providers"] = [{"id": "1", "token": "old"}]
    patch = svc.restore_masked_items({"metadata_providers": [{"id": "1", "token": "new"}]})
    assert patch["metadata_providers"][0]["token"] == "new"


def test_apprise_plural_urls_are_masked_and_preserved(tmp_path):
    from romarr.app import ROMarr
    svc=ROMarr({"ROMARR_DATA":str(tmp_path/"s.json")})
    row=svc.store.put_item("connections",{"type":"apprise","urls":"discord://user:token@example.invalid"})
    masked=svc.safe_settings()["connections"][0]
    assert masked["urls"]=="********"
    patch=svc.restore_masked_items({"connections":[masked]})
    assert patch["connections"][0]["urls"]==row["urls"]
