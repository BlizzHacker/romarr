"""#57: the indexer Test button said "Rejected the API key" after Save.

The key was never wrong. The edit form shows `********` for a saved
credential; on Save/Test the placeholder travelled in the body, and the
route merged secrets with the DOWNLOADERS merger -- whose CLIENT_TYPES
spec has no entry for "prowlarr", so it merged nothing and stored eight
asterisks as the real API key. Prowlarr then answered 401 to a key of
`********`, which surfaced as "Rejected the API key".

Two layers here: the merger itself (unit), and the real HTTP routes --
POST /api/v1/indexer (save) and /api/v1/indexer/test -- driven the way a
browser drives them.
"""

import io
import json

import pytest

from romarr.app import ROMarr, make_handler
from romarr.indexers import merge_indexer_secrets, redact_indexer


# -- the merger -----------------------------------------------------------------

def test_a_masked_form_keeps_the_stored_key():
    old = {"id": "1", "type": "prowlarr", "url": "http://p:9696",
           "api_key": "real-key"}
    new = {"id": "1", "type": "prowlarr", "url": "http://p:9696",
           "api_key": "********"}
    assert merge_indexer_secrets(new, old)["api_key"] == "real-key"


def test_a_blank_secret_field_also_means_unchanged():
    old = {"id": "1", "type": "prowlarr", "url": "u", "api_key": "real"}
    assert merge_indexer_secrets({"type": "prowlarr", "api_key": ""},
                                 old)["api_key"] == "real"


def test_a_typed_key_replaces_the_stored_one():
    old = {"id": "1", "type": "prowlarr", "api_key": "old-key"}
    assert merge_indexer_secrets({"type": "prowlarr", "api_key": "new-key"},
                                 old)["api_key"] == "new-key"


def test_no_stored_row_means_nothing_to_merge():
    new = {"type": "prowlarr", "api_key": "typed"}
    assert merge_indexer_secrets(new, None) is new


def test_passkey_is_an_indexer_secret_too():
    # torrentrss carries `passkey`, a field the downloaders merger never knew.
    old = {"id": "2", "type": "torrentrss", "url": "u", "passkey": "pk"}
    assert merge_indexer_secrets({"type": "torrentrss", "passkey": "********"},
                                 old)["passkey"] == "pk"
    assert redact_indexer(old)["passkey"] == "********"


def test_the_downloaders_merger_really_was_the_wrong_one():
    # The regression this whole file exists for: the downloaders merge,
    # given a prowlarr body, silently merges NOTHING (no CLIENT_TYPES spec),
    # so the placeholder survives into the store. If this ever starts
    # merging indexer secrets, the split below is no longer load-bearing.
    from romarr.downloaders import merge_secrets
    old = {"id": "1", "type": "prowlarr", "api_key": "real-key"}
    out = merge_secrets({"type": "prowlarr", "api_key": "********"}, old)
    assert out["api_key"] == "********"


# -- the routes, driven as a browser drives them ---------------------------------

class FakeHandler:
    """Enough of BaseHTTPRequestHandler for the real _post/_get bodies."""

    def __init__(self, service, path, body=None, headers=None):
        self.service = service
        self.path = path
        payload = json.dumps(body or {}).encode()
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(payload)),
                        **(headers or {})}
        self.status = None

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def log_message(self, *a):
        pass

    # the real handler's response surface, minimal mirror of _json/_send
    def _json(self, code, payload):
        self.status = code
        self.wfile.write(json.dumps(payload).encode())

    _send = None  # not needed by these routes


def drive(service, method, path, body=None, headers=None):
    Real = make_handler(service)
    fake = FakeHandler(service, path, body, headers)
    fn = getattr(Real, "_post" if method == "POST" else "_get")
    fn(fake)
    raw = fake.wfile.getvalue()
    return fake.status, (json.loads(raw) if raw else None)


@pytest.fixture()
def svc(tmp_path):
    s = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                    "ROMARR_AUTH": "disabled"})
    return s


def test_save_then_test_uses_the_stored_key_not_the_placeholder(svc,
                                                                 monkeypatch):
    # 1. Save with a real key.
    st, saved = drive(svc, "POST", "/api/v1/indexer", {
        "name": "Prowlarr", "type": "prowlarr",
        "url": "http://prowlarr:9696", "api_key": "the-real-key"})
    assert st == 200
    # ...and the response masks it back.
    assert saved["api_key"] == "********"

    stored = svc.store.get_item("indexers", saved["id"])
    assert stored["api_key"] == "the-real-key", (
        "#57: the placeholder was stored as the credential")

    # 2. Press Test on the saved row: the form sends the placeholder.
    seen = {}

    class FakeProwlarr:
        def __init__(self, cfg):
            seen["api_key"] = cfg.api_key

        def indexers(self):
            return [{"privacy": "private"}, {"privacy": "public"}]

    monkeypatch.setattr("romarr.app.Prowlarr", FakeProwlarr)
    st, out = drive(svc, "POST", "/api/v1/indexer/test", {
        "id": saved["id"], "type": "prowlarr",
        "url": "http://prowlarr:9696", "api_key": "********"})
    assert st == 200
    assert seen["api_key"] == "the-real-key", (
        "Test must probe the stored key, not eight asterisks")
    assert out["ok"] is True
    assert "2 indexer(s)" in out["message"]


def test_a_placeholder_with_nothing_stored_says_so_instead_of_401(svc):
    # No id, no stored row: the merger has nothing to fill in, so the probe
    # would send `********` and report a 401 as "Rejected the API key".
    st, out = drive(svc, "POST", "/api/v1/indexer/test", {
        "type": "prowlarr", "url": "http://prowlarr:9696",
        "api_key": "********"})
    assert st == 200
    assert out["ok"] is False
    assert "placeholder" in out["message"]


def test_indexer_listing_masks_every_indexer_secret(svc):
    drive(svc, "POST", "/api/v1/indexer", {
        "name": "RSS", "type": "torrentrss", "url": "http://rss",
        "passkey": "hunter2"})
    st, out = drive(svc, "GET", "/api/v1/indexer")
    assert st == 200
    item = out["items"][0]
    assert item["passkey"] == "********", (
        "the downloaders redact did not know passkey; it shipped in clear")


def test_editing_a_saved_indexer_keeps_the_key_through_the_same_route(svc):
    # Renaming must not wipe the credential: same put_item path, masked form.
    st, saved = drive(svc, "POST", "/api/v1/indexer", {
        "name": "P", "type": "prowlarr", "url": "http://p:9696",
        "api_key": "k-1"})
    st2, renamed = drive(svc, "POST", "/api/v1/indexer", {
        "id": saved["id"], "name": "P renamed", "type": "prowlarr",
        "url": "http://p:9696", "api_key": "********"})
    assert st2 == 200
    assert svc.store.get_item("indexers", saved["id"])["api_key"] == "k-1"
