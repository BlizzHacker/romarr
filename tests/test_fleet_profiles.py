"""Tests for multi-device fleet and standalone gaming launcher profiles."""

from romarr import hub


def test_fleet_profiles_contains_handhelds_and_retro_setups():
    res = hub.fleet_profiles()
    assert "profiles" in res
    profiles = {p["id"]: p for p in res["profiles"]}

    # Verify existing profiles remain intact
    assert "es-de" in profiles
    assert "retropie-arcadepunks" in profiles
    assert "argosy" in profiles
    assert "grout" in profiles
    assert "deckrommsync" in profiles

    # Verify newly added SteamOS, Retro Punks Arcade / Batocera, and Adreno profiles
    assert "steamos-deck" in profiles
    assert "rpi-batocera-arcadepunks" in profiles
    assert "adreno-android-handhelds" in profiles

    steamos = profiles["steamos-deck"]
    assert "SteamOS" in steamos["name"]
    assert steamos["gamelist_format"] == "esde"
    assert steamos["igir_token"] == "{es}"

    adreno = profiles["adreno-android-handhelds"]
    assert "Adreno" in adreno["name"] or "Snapdragon" in adreno["name"]
    assert adreno["igir_token"] == "{romm}"

    rpi_arcade = profiles["rpi-batocera-arcadepunks"]
    assert "Arcade Punks" in rpi_arcade["name"]
    assert rpi_arcade["igir_token"] == "{batocera}"


def test_build_fleet_sync_plan_for_steamos_and_adreno():
    plan_steamos = hub.build_fleet_sync_plan({
        "device_id": "steamos-deck",
        "source_path": "/home/deck/Emulation/roms",
    })
    assert plan_steamos["ok"] is True
    assert plan_steamos["device"]["id"] == "steamos-deck"
    assert "igir" in plan_steamos["igir_command"]

    plan_adreno = hub.build_fleet_sync_plan({
        "device_id": "adreno-android-handhelds",
        "prefer_regions": ["USA", "WORLD"],
    })
    assert plan_adreno["ok"] is True
    assert plan_adreno["device"]["id"] == "adreno-android-handhelds"


def test_fleet_profiles_split_files_vs_app_and_never_delete_by_default():
    res = hub.fleet_profiles()
    assert res["kinds"] == {
        "files": "Copy games onto the device's ROM folder",
        "app": "The device runs an app that connects to your library server",
    }
    by_id = {p["id"]: p for p in res["profiles"]}
    assert {p["kind"] for p in res["profiles"]} == {"files", "app"}
    for pid in ("es-de", "retropie-arcadepunks", "rpi-batocera-arcadepunks", "steamos-deck", "adreno-android-handhelds"):
        assert by_id[pid]["kind"] == "files"
        assert "{platform}" not in by_id[pid]["default_path"]
    for pid in ("argosy", "grout", "deckrommsync", "retroarch-webdav", "console-cfw-feeds"):
        assert by_id[pid]["kind"] == "app"
        assert by_id[pid]["default_path"] == ""

    # Default local plan is non-destructive and strips legacy {platform} suffix
    local_plan = hub.build_fleet_sync_plan({
        "device_id": "retropie-arcadepunks",
        "transport": "local",
        "target_path": "/home/pi/RetroPie/roms/{platform}",
    })
    assert local_plan["ok"] is True
    assert local_plan["kind"] == "files"
    assert local_plan["mirror"] is False
    assert local_plan["warnings"] == []
    assert local_plan["target_path"] == "/home/pi/RetroPie/roms"
    assert local_plan["output_path"] == "/home/pi/RetroPie/roms/{es}"
    assert "clean" not in local_plan["igir_command"].split()
    assert local_plan["preview_command"].startswith("igir test report ")
    assert "copy" not in local_plan["preview_command"].split()

    # Default SSH plan does not pass --delete-after
    ssh_plan = hub.build_fleet_sync_plan({
        "device_id": "rpi-batocera-arcadepunks",
        "transport": "ssh",
    })
    assert "--delete" not in ssh_plan["network_sync_command"]
    assert "root@batocera.local:/userdata/roms/" in ssh_plan["network_sync_command"]

    # Default rclone plan uses rclone copy, not rclone sync
    rclone_plan = hub.build_fleet_sync_plan({
        "device_id": "es-de",
        "transport": "rclone",
        "target_path": "remote:roms",
    })
    assert rclone_plan["network_sync_command"].startswith("rclone copy ")

    # Explicit mirror=True enables clean / --delete-after / sync and warns
    mirror_local = hub.build_fleet_sync_plan({
        "device_id": "es-de",
        "transport": "local",
        "mirror": True,
    })
    assert mirror_local["mirror"] is True
    assert "clean" in mirror_local["igir_command"].split()
    assert mirror_local["warnings"]

    mirror_ssh = hub.build_fleet_sync_plan({
        "device_id": "retropie-arcadepunks",
        "transport": "ssh",
        "mirror": True,
    })
    assert "--delete-after" in mirror_ssh["network_sync_command"]
    assert mirror_ssh["warnings"]


def test_app_profiles_return_setup_steps_not_filesystem_commands():
    plan = hub.build_fleet_sync_plan({
        "device_id": "argosy",
        "library_url": "https://romm.example.lan/",
    })
    assert plan["ok"] is True
    assert plan["kind"] == "app"
    assert plan["igir_command"] == ""
    assert plan["network_sync_command"] == ""
    assert any("https://romm.example.lan" in step for step in plan["setup"])

    bad = hub.build_fleet_sync_plan({"device_id": "nonexistent-device"})
    assert bad["ok"] is False


def test_library_shell_ui_has_no_hardcoded_host_hacks():
    from romarr.ui import page
    html = page()
    assert "romm.moveweight.com" not in html
    assert "192.168.0.94" not in html


def test_library_and_devices_page_is_named_plainly_and_agents_have_their_own_page():
    """AI agents are not a device or a library app: they get their own page,
    and the page that remains says what it is."""
    from romarr.ui import JS, page
    html = page()
    assert "Library &amp; Devices" in html or "Library & Devices" in html
    assert 'data-page="agents"' in html
    assert "RENDER.agents=" in JS
    portal = JS[JS.index("RENDER.portal="):JS.index("RENDER.agents=")]
    assert "data-ptab=\"mcp\"" not in portal
    assert "/api/v1/mcp" not in portal
    assert "go('agents')" in portal          # old saved tab still lands somewhere
    for jargon in ("Library Shell", "Device Fleet", "Load in Shell", "npx igir@latest"):
        assert jargon not in JS, jargon


def test_device_plan_page_offers_copy_and_shows_errors():
    from romarr.ui import JS
    portal = JS[JS.index("RENDER.portal="):JS.index("RENDER.agents=")]
    assert "data-copycmd" in portal
    assert "Could not build the plan" in portal


def test_home_relative_device_folders_still_expand_in_the_shell():
    """`~/ROMs` inside single quotes is a folder named "~", not home."""
    import subprocess
    local = hub.build_fleet_sync_plan({"device_id": "es-de", "source_path": "/roms"})
    output_arg = local["igir_command"].split("--output ")[1].split(" ")[0]
    assert output_arg == "~/'ROMs/{es}'"
    expanded = subprocess.run(["bash", "-c", "printf %s " + output_arg],
                              capture_output=True, text=True, check=True).stdout
    assert expanded.startswith("/") and expanded.endswith("/ROMs/{es}")

    remote = hub.build_fleet_sync_plan({"device_id": "steamos-deck", "transport": "ssh"})
    assert "deck@steamdeck.local:Emulation/roms/" in remote["network_sync_command"]
    assert "~" not in remote["network_sync_command"]


def test_device_plan_clears_output_and_ignores_stale_reply_when_mirror_or_inputs_change():
    import shutil
    import subprocess
    import pytest
    from romarr.ui import JS

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    portal_src = JS[JS.index("RENDER.portal="):JS.index("RENDER.agents=")]
    script = r'''
const assert = require('node:assert/strict');
let pageVersion = 1;
let activePage = 'portal';
const window = { _portalTab: 'fleet' };
const location = { origin: 'http://127.0.0.1:7878' };
const RENDER = {};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const toast = () => {};
const go = () => {};

function makeEl(id, tagName, attrs = {}) {
  return {
    id, tagName, type: attrs.type || '', value: attrs.value || '',
    checked: Boolean(attrs.checked), innerHTML: '', dataset: {},
    onchange: null, oninput: null, onclick: null,
  };
}
let els = {};
const pageEl = {
  _html: '',
  get innerHTML() { return this._html; },
  set innerHTML(v) {
    this._html = v;
    els = {
      '#page': pageEl,
      '#fl-dev': makeEl('fl-dev', 'SELECT', { value: 'es-de' }),
      '#fl-trans': makeEl('fl-trans', 'SELECT', { value: 'local' }),
      '#fl-path': makeEl('fl-path', 'INPUT', { value: '~/ROMs' }),
      '#fl-host': makeEl('fl-host', 'INPUT', { value: 'user@device.local' }),
      '#fl-src': makeEl('fl-src', 'INPUT', { value: '/roms' }),
      '#fl-reg': makeEl('fl-reg', 'INPUT', { value: 'USA,WORLD,EUR,JPN' }),
      '#fl-1g1r': makeEl('fl-1g1r', 'INPUT', { type: 'checkbox', checked: true }),
      '#fl-mirror': makeEl('fl-mirror', 'INPUT', { type: 'checkbox', checked: false }),
      '#fl-plan': makeEl('fl-plan', 'BUTTON'),
      '#fl-out': makeEl('fl-out', 'DIV'),
    };
  },
};
const $ = (sel) => sel === '#page' ? pageEl : (els[sel] || null);
const document = { querySelectorAll: () => [] };
const j = async (url) => {
  if (url === '/api/v1/library') return { items: [] };
  if (url === '/api/v1/hub/fleet') return {
    profiles: [{ id: 'es-de', name: 'ES-DE', kind: 'files', category: 'Frontend',
                 protocol: 'Igir', igir_token: '{es}', default_path: '~/ROMs',
                 default_ssh_host: 'user@device.local', description: 'ES-DE' }],
    kinds: {}, igir: { available: true },
  };
  return { library_path: '/roms' };
};

let pendingResolvers = [];
const fetch = (url, opts) => new Promise((resolve) => {
  pendingResolvers.push({ body: JSON.parse(opts.body), resolve });
});

''' + portal_src + r'''
(async () => {
  await RENDER.portal();
  // Flush initial automatic runPlan fetch.
  assert.equal(pendingResolvers.length, 1);
  pendingResolvers.shift().resolve({
    json: async () => ({
      ok: true, kind: 'files', output_path: '~/ROMs/{es}', warnings: [],
      steps: [{ title: 'Sync', command: 'igir copy --input /roms --output ~/\'ROMs/{es}\'' }],
    }),
  });
  await new Promise(r => setImmediate(r));
  assert(els['#fl-out'].innerHTML.includes('igir copy'));

  // Scenario 1: Unchecking Mirror while a mirror=True response is still in flight drops it.
  els['#fl-mirror'].checked = true;
  els['#fl-mirror'].onchange();
  assert.equal(els['#fl-out'].innerHTML, '');
  els['#fl-plan'].onclick();
  assert.equal(pendingResolvers.length, 1);
  assert.equal(pendingResolvers[0].body.mirror, true);

  els['#fl-mirror'].checked = false;
  els['#fl-mirror'].onchange();
  assert.equal(els['#fl-out'].innerHTML, '');

  pendingResolvers.shift().resolve({
    json: async () => ({
      ok: true, kind: 'files', output_path: '~/ROMs/{es}',
      warnings: ['Mirror mode deletes extra files'],
      steps: [{ title: 'Mirror', command: 'igir copy clean --input /roms' }],
    }),
  });
  await new Promise(r => setImmediate(r));
  assert.equal(els['#fl-out'].innerHTML, '');
  assert(!els['#fl-out'].innerHTML.includes('igir copy clean'));

  // Scenario 2: Rebuilding renders safe plan, then toggling Mirror or editing path clears it.
  els['#fl-plan'].onclick();
  assert.equal(pendingResolvers[0].body.mirror, false);
  pendingResolvers.shift().resolve({
    json: async () => ({
      ok: true, kind: 'files', output_path: '~/ROMs/{es}', warnings: [],
      steps: [{ title: 'Mirror', command: 'igir copy clean --input /roms' }],
    }),
  });
  await new Promise(r => setImmediate(r));
  assert(els['#fl-out'].innerHTML.includes('igir copy clean'));

  els['#fl-mirror'].onchange();
  assert.equal(els['#fl-out'].innerHTML, '');

  els['#fl-plan'].onclick();
  pendingResolvers.shift().resolve({
    json: async () => ({
      ok: true, kind: 'files', output_path: '~/Custom/{es}', warnings: [],
      steps: [{ title: 'Copy', command: 'igir copy --output ~/\'Custom/{es}\'' }],
    }),
  });
  await new Promise(r => setImmediate(r));
  assert(els['#fl-out'].innerHTML.includes('Custom'));

  els['#fl-path'].value = '~/Other';
  els['#fl-path'].oninput();
  assert.equal(els['#fl-out'].innerHTML, '');
})().catch((err) => { console.error(err); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
