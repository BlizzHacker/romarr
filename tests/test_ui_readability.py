"""Readability pass: sizes, dismissible notices, folded help, quiet Prowlarr."""
from romarr import ui
from romarr.app import ROMarr


def test_type_and_controls_are_larger():
    assert "body{font-size:15px" in ui.CSS
    assert ".btn{padding:10px 18px;font-size:14px;min-height:38px" in ui.CSS
    assert ".mini{padding:6px 14px;font-size:13px;min-height:32px" in ui.CSS
    assert "#railfoot{max-height:34vh" in ui.CSS


def test_every_notice_can_be_dismissed_and_brought_back():
    assert "romarr.dismissedNotes" in ui.JS
    assert ".panel-note:not([data-keep]):not([data-note])" in ui.JS
    assert 'id="notes-restore"' in ui._theme_control_html()


def test_long_help_folds_behind_more():
    assert "p.help:not([data-fold])" in ui.JS and "help-more" in ui.CSS


def test_an_unconfigured_prowlarr_is_not_an_error(tmp_path):
    from romarr.app import make_handler
    import json, threading, urllib.request
    from http.server import ThreadingHTTPServer
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/indexer")
        req.add_header("X-Api-Key", "k")
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
    finally:
        httpd.shutdown(); httpd.server_close()
    assert body["error"] is None and body["proxied"] == []


def test_simple_advanced_ui_mode_and_progressive_disclosure():
    html = ui.page()
    assert 'id="ui-mode-toggle"' in html
    assert 'id="cmd-trigger"' in html
    assert 'id="bg-layer"' in html
    assert 'id="bg-preset"' in html
    assert 'id="bg-url"' in html
    assert 'id="bg-fit"' in html
    assert 'id="chat-dock-fab"' in html
    assert 'id="chat-dock"' in html
    assert "romarr.uiMode" in html
    assert "romarr.bg" in html
    assert "details.adv{" in ui.CSS
    assert ".best-match{" in ui.CSS
    assert ".proof-bar{" in ui.CSS
    assert "#chat-dock{" in ui.CSS
    # Progressive disclosure sections across Search, Hub, Setup, General, Media, Profiles, Indexers, Portal, Stats, Update
    for summary in (
        "Recommended Zero-Key Starter Sources",
        "Recommended Best Match",
        "rejected release(s) and why they were refused",
        "Live Readiness Proof",
        "setup-oneclick-hub",
        "Advanced: Power tools, custom repositories &amp; sandbox status",
        "Advanced: Scheduler intervals (The clock)",
        "Advanced: Peering URL, FlareSolverr, custom plugin forges &amp; GitHub token",
        "Advanced: Two-Factor Authentication (TOTP) &amp; Single Sign-On (OIDC)",
        "Advanced: Path mappings, per-platform folder overrides &amp; import rules",
        "Advanced: Switch NSZ/XCI compression &amp; multi-part package folders",
        "Advanced: Antivirus &amp; hash reputation scanners",
        "Advanced: 1G1R fan-translation &amp; language rules",
        "Advanced: Size limits, Usenet retention, video standard &amp; custom scoring rules",
        "Advanced: Hydra Launcher community JSON sources",
        "Advanced library actions",
        "Advanced maintenance: Library audit &amp; Fix existing filenames",
        "Advanced: Release channels &amp; pull request previews",
    ):
        assert summary in html, summary

