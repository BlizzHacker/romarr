"""Collections must be usable without reading the source.

A set queued with no platform requested every title for nowhere and still
read 100% done; a queued set never advanced unless someone pressed Run next
for every slice. These pin the fixes.
"""

from __future__ import annotations

from romarr.app import ROMarr
from romarr.collections import Batch, platform_for_dat
from romarr.dat import Dat, Game, Rom


def test_a_dat_name_suggests_its_platform():
    assert platform_for_dat("Nintendo - Super Nintendo Entertainment System (20240101-000000)") == "snes"
    assert platform_for_dat("Nintendo - Game Boy Advance") == "gba"
    assert platform_for_dat("Sega - Master System - Mark III") == "sms"
    assert platform_for_dat("Sony - PlayStation") == "psx"


def test_the_vendor_alone_never_picks_a_platform():
    # "Nintendo" is an NES alias; an unknown Nintendo system must not become the NES.
    assert platform_for_dat("Nintendo - Something Nobody Ships") == ""
    assert platform_for_dat("") == ""


def test_the_operators_catalog_mapping_wins():
    assert platform_for_dat("My Odd DAT", {"my odd dat": "snes"}) == "snes"


def _service(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    service.scheduler.stop()
    library = tmp_path / "library"
    library.mkdir()
    service.game_libraries = [({"name": "L", "type": "folder", "path": str(library)}, object())]
    games = {n: Game(name=n, roms=(Rom(name=n + ".sfc", crc=f"{i:08x}"),))
             for i, n in enumerate(["Alpha (USA)", "Beta Quest (USA)"])}
    service.dats.dats.append(Dat(name="Nintendo - Super Nintendo Entertainment System",
                                 version="1", games=games))
    return service


def test_starting_a_set_without_a_platform_uses_the_dats_own(tmp_path):
    service = _service(tmp_path)
    result = service.collection_start("Nintendo - Super Nintendo Entertainment System", "")
    assert result.get("ok"), result
    assert result["platform"] == "snes"
    assert result["queued"] == 2


def test_starting_a_set_for_an_unknown_system_is_refused(tmp_path):
    service = _service(tmp_path)
    service.dats.dats[0].name = "Homebrew - Unknown Thing"
    result = service.collection_start("Homebrew - Unknown Thing", "")
    assert "pick the platform" in result["error"]


def test_a_failed_request_counts_as_failed_not_done(tmp_path):
    service = _service(tmp_path)
    service.request = lambda name, platform: {"ok": False, "error": "no usable release among 0 result(s)"}
    batch = Batch(id="b1", platform="snes", dat="x", queue=["Alpha (USA)"])
    service.store.put_item("batches", batch.to_dict())
    progress = service.collection_step("b1")
    assert progress["done"] == 0 and progress["failed"] == 1
    status = service.collection_status()
    row = status["batches"][0]
    assert row["failures"] == [{"title": "Alpha (USA)",
                                "reason": "no usable release among 0 result(s)"
                                " (kept on Wanted; the Wanted search retries it)"}]
    assert status["dat_platforms"]["Nintendo - Super Nintendo Entertainment System"] == "snes"
    assert status["interval_minutes"] == 10


def test_the_scheduled_tick_advances_queued_sets_and_skips_paused_ones(tmp_path):
    service = _service(tmp_path)
    asked = []
    service.request = lambda name, platform: asked.append(name) or {"ok": True}
    service.store.put_item("batches", Batch(id="go", platform="snes", queue=["A", "B"],
                                            per_pass=1).to_dict())
    service.store.put_item("batches", Batch(id="wait", platform="snes", status="paused",
                                            queue=["C"]).to_dict())
    assert service._collection_batches_active()
    result = service.collection_tick()
    assert asked == ["A"]
    assert result["stepped"] == 1 and result["requested"] == 1
    service.collection_tick()
    assert asked == ["A", "B"]
    assert not service._collection_batches_active()
    assert service.collection_tick()["stepped"] == 0


def test_an_old_set_saved_without_a_platform_heals_from_its_dat(tmp_path):
    """The reported bug: Run next moved the bar and nothing reached Wanted."""
    service = _service(tmp_path)
    asked = []
    service.request = lambda name, platform: asked.append((name, platform)) or {"ok": True}
    service.store.put_item("batches", Batch(
        id="old", platform="", dat="Nintendo - Super Nintendo Entertainment System",
        queue=["Alpha (USA)"]).to_dict())
    service.collection_step("old")
    assert asked == [("Alpha (USA)", "snes")]


def test_a_set_with_no_knowable_platform_fails_each_title_with_a_reason(tmp_path):
    service = _service(tmp_path)
    service.request = lambda name, platform: {"ok": True}
    service.store.put_item("batches", Batch(
        id="lost", platform="", dat="Homebrew - Unknown Thing", queue=["X"]).to_dict())
    progress = service.collection_step("lost")
    assert progress["done"] == 0 and progress["failed"] == 1
    reason = service.collection_status()["batches"][0]["failures"][0]["reason"]
    assert "no platform" in reason


def test_a_title_with_no_release_says_it_stays_on_wanted(tmp_path):
    service = _service(tmp_path)
    service.request = lambda name, platform: {
        "ok": False, "error": "no usable release among 3 result(s)"}
    service.store.put_item("batches", Batch(id="b", platform="snes", queue=["A"]).to_dict())
    service.collection_step("b")
    reason = service.collection_status()["batches"][0]["failures"][0]["reason"]
    assert "kept on Wanted" in reason


def _render_harness(status, checks):
    import shutil
    import subprocess

    import pytest

    from romarr.ui import JS
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Collections renderer harness")
    start = JS.index("RENDER.collections=async()=>{")
    end = JS.index("\nRENDER.manualimport=", start)
    harness = """
const assert=require('node:assert/strict');
const RENDER={}, nodes={}, SETTINGS={}, pending=[];
const PLATFORMS=[{slug:'nes',name:'NES'},{slug:'snes',name:'SNES'}];
const store={};
const localStorage={getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}};
let activePage='collections',pageVersion=1;
const $=id=>nodes[id]||(nodes[id]={id,value:'',innerHTML:'',events:{},style:{},
  querySelectorAll:()=>[],addEventListener(name,fn){this.events[name]=fn;}});
const enhancePlatformSelect=()=>{}, fmtBytes=String, confirm=()=>true, toast=()=>{};
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const STATUS=""" + __import__("json").dumps(status) + """;
const j=(url,options)=>{
  if(url==='/api/v1/collection')return Promise.resolve(STATUS);
  if(url==='/api/v1/collection/source'||url==='/api/v1/collection/source?all=1')return Promise.resolve({
    torrents:[{hash:'h',client_id:'one',client_name:'qb',name:'No-Intro pack',state:'paused'}],jobs:[]});
  if(options) return new Promise(resolve=>pending.push({url,options,resolve}));
  throw Error(url);
};
"""
    result = subprocess.run([node, "-e", harness + JS[start:end] + checks],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_the_page_explains_itself_and_the_panel_can_be_hidden():
    _render_harness({"dats": ["Nintendo - Super Nintendo Entertainment System"],
                     "dat_platforms": {"Nintendo - Super Nintendo Entertainment System": "snes"},
                     "batches": [], "interval_minutes": 10}, """
(async()=>{
  await RENDER.collections();
  const page=$('#page').innerHTML;
  assert.match(page,/How Collections works/);
  assert.match(page,/docs\\/COLLECTIONS\\.md/);
  assert.match(page,/<option value="snes" selected>/, 'the DAT platform is preselected');
  await $('#chelp-hide').onclick();
  assert.equal(localStorage.getItem('romarr.collections.help.hidden'),'1');
  assert.equal($('#chelp').style.display,'none');
  await $('#chelp-show').onclick();
  assert.equal(localStorage.getItem('romarr.collections.help.hidden'),null);
  await new Promise(setImmediate);
  assert.match($('#cbatch').innerHTML,/No set is being acquired yet/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_the_torrent_start_button_uses_the_dats_platform_when_none_is_picked():
    """The reported bug: Start selective acquisition never became clickable."""
    _render_harness({"dats": ["Nintendo - Super Nintendo Entertainment System"],
                     "dat_platforms": {"Nintendo - Super Nintendo Entertainment System": "snes"},
                     "batches": []}, """
(async()=>{
  await RENDER.collections();
  $('#cdat').value='Nintendo - Super Nintendo Entertainment System';
  $('#cplat').value='';
  $('#csrc-cap').value='100';
  $('#csrc-torrent').selectedOptions=[{value:'h',dataset:{client:'one'}}];
  const preview=$('#csrc-plan').onclick();
  const call=pending.shift();
  assert.equal(JSON.parse(call.options.body).platform,'snes');
  call.resolve({matched_titles:1,batches:[{groups:[{title:'A',files:[{}],size:1}]}],total_bytes:1,reserve_bytes:1});
  await preview;
  assert.doesNotMatch($('#csrc-out').innerHTML,/id="csrc-start" disabled/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_with_no_dat_the_page_says_where_to_add_one():
    _render_harness({"dats": [], "batches": []}, """
(async()=>{
  await RENDER.collections();
  const page=$('#page').innerHTML;
  assert.match(page,/No DAT loaded yet/);
  assert.match(page,/Settings → Media Management/);
  assert.match(page,/COLLECTIONS\\.md/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_the_guide_exists_and_is_linked():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    guide = (root / "docs" / "COLLECTIONS.md").read_text(encoding="utf-8")
    for heading in ("Your first set", "Every option explained", "Worked examples",
                    "Statuses and errors", "FAQ"):
        assert heading in guide
    assert "docs/COLLECTIONS.md" in (root / "README.md").read_text(encoding="utf-8")
    assert "COLLECTIONS.md" in (root / "docs" / "FEATURES.md").read_text(encoding="utf-8")


def test_a_named_dat_that_is_not_loaded_is_refused_not_swapped(tmp_path):
    """Review: an unknown DAT name fell back to the first loaded DAT while the
    platform came from the requested name -- SNES titles requested as GBA."""
    service = _service(tmp_path)
    result = service.collection_start("Nintendo - Game Boy Advance", "")
    assert "is not loaded" in result["error"]
    source = service.collection_source_start("one", "h", "Nintendo - Game Boy Advance", "", 1 << 30)
    assert "is not loaded" in source["error"]


def test_a_title_already_in_flight_counts_as_requested_not_failed(tmp_path):
    service = _service(tmp_path)
    asked = []
    service.request = lambda name, platform: asked.append(name) or {"ok": True}
    service._reserve_automatic_grab = lambda game, platform: None  # in flight
    service.store.put_item("batches", Batch(id="b", platform="snes", queue=["A"]).to_dict())
    progress = service.collection_step("b")
    assert asked == [], "an in-flight title must not be handed off again"
    assert progress["done"] == 1 and progress["failed"] == 0


def test_switching_to_a_dat_without_a_known_platform_clears_the_old_one():
    _render_harness({"dats": ["Nintendo - Super Nintendo Entertainment System", "Odd DAT"],
                     "dat_platforms": {"Nintendo - Super Nintendo Entertainment System": "snes",
                                       "Odd DAT": ""},
                     "batches": []}, """
(async()=>{
  await RENDER.collections();
  $('#cplat').value='snes';
  $('#cdat').value='Odd DAT';
  $('#cdat').events.change();
  assert.equal($('#cplat').value,'');
  $('#cdat').value='Nintendo - Super Nintendo Entertainment System';
  $('#cdat').events.change();
  assert.equal($('#cplat').value,'snes');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_the_atari_8_bit_family_dat_maps_to_atari_8_bit():
    assert platform_for_dat("Atari - 8-bit Family") == "atari8bit"


def test_non_game_umd_dats_get_no_default_platform():
    assert platform_for_dat("Sony - PlayStation Portable (UMD Video)") == ""
    assert platform_for_dat("Sony - PlayStation Portable (UMD Music)") == ""
    assert platform_for_dat("Sony - PlayStation Portable (PSN)") == "psp"
    # The operator's own mapping still wins.
    assert platform_for_dat("Sony - PlayStation Portable (UMD Video)",
                            {"Sony - PlayStation Portable (UMD Video)": "psp"}) == "psp"


def test_every_live_style_dat_name_maps_where_romarr_has_the_platform():
    """Names as No-Intro and Redump publish them, against the platform table."""
    expected = {
        "Atari - 2600": "atari2600", "Atari - 8-bit Family": "atari8bit",
        "Bandai - WonderSwan Color": "wonderswan-color",
        "Commodore - Amiga": "amiga", "GCE - Vectrex": "vectrex",
        "Magnavox - Odyssey2": "odyssey-2-slash-videopac-g7000",
        "NEC - PC Engine - TurboGrafx 16": "turbografx16--1",
        "NEC - PC Engine CD - TurboGrafx-CD": "turbografx-16-slash-pc-engine-cd",
        "Nintendo - Family Computer Disk System": "fds",
        "Nintendo - New Nintendo 3DS (Digital)": "3ds",
        "Sega - Mega-CD - Sega CD": "segacd",
        "The 3DO Company - 3DO": "3do",
        # Systems ROMarr has no platform for stay blank rather than guessing.
        "Nintendo - Pokemon Mini": "", "Sega - Naomi": "", "NEC - PC-98": "",
    }
    assert {name: platform_for_dat(name) for name in expected} == expected
