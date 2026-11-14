"""Rendered System Update and cross-page state regressions for Part 4.

The Node fixture executes the shipped renderer with a small DOM stub. It does
not claim screenshot, layout, browser-engine, Docker or live-service coverage.
"""
import json
import shutil
import subprocess

import pytest

from romarr import ui


def _node(script: str, payload: dict) -> str:
    """Run first-party UI logic in Node without a network or application server."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; rendered UI regression not executed")
    result = subprocess.run(
        [node, "-e", script, json.dumps(payload)],
        capture_output=True, text=True, timeout=25, check=False,
    )
    assert result.returncode == 0, result.stderr[:1600]
    return result.stdout.strip()


def _helpers() -> str:
    """Select exactly the installed shared presentation helpers."""
    start = ui.JS.index("/* Present only verified backend identity")
    stop = ui.JS.index("RENDER.stats=async()=>", start)
    return ui.JS[start:stop]


_BASE = r"""
const data=JSON.parse(process.argv[1]);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const root={innerHTML:''}, controls={};
const RENDER={};
const j=async()=>data;
const $=selector=>{
  if(selector==='#page')return root;
  if(['#up-check','#up-apply','#up-plugins'].includes(selector)
      &&!root.innerHTML.includes('id="'+selector.slice(1)+'"'))return null;
  return controls[selector]||(controls[selector]={
    innerHTML:'',textContent:'',onclick:null,disabled:false
  });
};
const toast=()=>{},go=()=>{},confirm=()=>false;
"""


def _update(payload: dict) -> dict:
    """Execute the real Update renderer, including its conditional controls."""
    start = ui.JS.index("RENDER.update=async()=>")
    stop = ui.JS.index("RENDER.logs=async()=>", start)
    script = (_BASE + _helpers() + ui.JS[start:stop] + """
RENDER.update().then(()=>console.log(JSON.stringify({
  html:root.innerHTML,
  check_bound:typeof controls['#up-check']?.onclick==='function'
}))).catch(e=>{console.error(String(e));process.exitCode=1;});
""")
    return json.loads(_node(script, payload))


def _stats_banner(stats: dict, update: dict | None) -> str:
    """Render the shared Stats update banner independently of charts/actions."""
    script = (_BASE + _helpers() + """
console.log(JSON.stringify(statsUpdateMessage(data.stats,data.update)));
""")
    return json.loads(_node(script, {"stats": stats, "update": update}))


def _payload(**overrides) -> dict:
    """Supply conservative, version-pinned API response fixtures."""
    result = {
        "running_version": "1.8.1",
        "latest_version": "1.8.2",
        "update_available": False,
        "check_status": {"state": "never_checked"},
        "install": {"kind": "docker", "can_apply": False},
        "settings": {
            "auto_update": True, "auto_update_effective": False,
            "update_check": True, "update_channel": "stable",
        },
        "plugins": {}, "docker_guidance": ["Review host deployment."],
        "update_log": "",
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("state,label", [
    ("current", "no"), ("never_checked", "not checked"),
    ("unknown", "unknown"), ("error", "error"),
])
def test_update_page_renders_each_nonavailable_verdict(state, label):
    """A false Boolean alone must never mean a successful current check."""
    html = _update(_payload(check_status={"state": state}))["html"]
    assert "<b>" + label + "</b><span>Update available</span>" in html
    assert "<span>Last known release</span>" in html or state == "current"


def test_update_page_available_uses_positive_check_not_legacy_boolean():
    """A validated available state drives the tile, not a cached Boolean."""
    html = _update(_payload(check_status={"state": "available"},
                            update_available=False))["html"]
    assert 'color:var(--warn)">yes</span>' in html
    assert "<span>Latest on GitHub</span>" in html


def test_update_page_docker_configured_is_not_effective():
    """A saved preference never authorizes an in-container Docker update."""
    result = _update(_payload(check_status={"state": "available"}))
    html = result["html"]
    assert "configured only; this Docker image must be updated by its host" in html
    assert 'id="up-check"' in html and result["check_bound"]
    assert 'id="up-apply"' not in html


def test_update_page_git_eligible_keeps_manual_action():
    """Keep the original Update now action for capable Git installations."""
    html = _update(_payload(
        check_status={"state": "available"},
        install={"kind": "git", "can_apply": True},
        settings={"auto_update": True, "auto_update_effective": True},
    ))["html"]
    assert "effective for this installation" in html
    assert 'id="up-apply"' in html


def test_update_page_legacy_absence_is_honest():
    """Older APIs with no check/capability/build fields must not claim current."""
    result = _update({"running_version": "0.0.0+unknown",
                      "install": {"kind": "none"}})
    html = result["html"]
    assert "<b>unknown</b><span>Update available</span>" in html
    assert "Build identity: unverified" in html
    assert result["check_bound"]


def test_update_page_escapes_identity_and_preserves_optional_sections():
    """Neither untrusted source identity nor plugin/log data may become markup."""
    html = _update(_payload(
        build={"type": "development", "source_ref": "refs/heads/<img src=x>",
               "source_revision": "f" * 40, "source_repository": "foo/bar"},
        plugins={"<img>": {"source": "<script>", "ref": "main"}},
        update_log="<script>alert(1)</script>",
    ))["html"]
    assert "&lt;img src=x&gt;" in html and "<img src=x>" not in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "ROM Hub plugins" in html and "Last self-update log" in html
    assert 'id="up-plugins"' in html


def test_stats_banner_requires_real_availability_and_effective_capability():
    """Rendered Stats wording must respect Docker's inability to self-apply."""
    stats = {"version": "1.8.1"}
    status = _payload(check_status={"state": "available"},
                      update_available=True)
    banner = _stats_banner(stats, status)
    assert "Update available" in banner and "Docker cannot apply" in banner
    assert "applies on the next daily check" not in banner
    assert _stats_banner(stats, _payload(check_status={"state": "current"})) == ""


def test_stats_banner_explains_disabled_scheduled_checks():
    """An available release does not turn a disabled scheduler back on."""
    stats = {"version": "1.8.1"}
    state = _payload(check_status={"state": "available"},
                     install={"kind": "git", "can_apply": True},
                     settings={"auto_update": True,
                               "auto_update_effective": False,
                               "update_check": False})
    banner = _stats_banner(stats, state)
    assert "Scheduled update checks are disabled" in banner
    assert "applies on the next daily check" not in banner


def test_stats_banner_handles_failed_and_mismatched_status():
    """Do not reuse a stale or mismatched Update API verdict as current."""
    stats = {"version": "1.8.1"}
    assert "Update status: error" in _stats_banner(
        stats, _payload(check_status={"state": "error"}))
    assert "Update status unavailable" in _stats_banner(
        stats, _payload(running_version="1.8.2"))
    assert "Update status unavailable" in _stats_banner(stats, None)


def test_update_ignores_late_api_response_after_navigation():
    """A previous page's fetch must not repaint an unrelated newer page."""
    start = ui.JS.index("RENDER.update=async()=>")
    stop = ui.JS.index("RENDER.logs=async()=>", start)
    setup = _BASE.replace(
        "const j=async()=>data;",
        "let activePage='update',pageVersion=1,resume;"
        "const j=()=>new Promise(resolve=>{resume=resolve;});",
    )
    script = setup + _helpers() + ui.JS[start:stop] + """
const pending=RENDER.update();
root.innerHTML='<div>New page</div>';
activePage='status';pageVersion=2;
resume(data);
pending.then(()=>console.log(JSON.stringify(root.innerHTML)))
  .catch(e=>{console.error(String(e));process.exitCode=1;});
"""
    assert json.loads(_node(script, _payload())) == "<div>New page</div>"


def test_stats_ignores_late_update_response_after_navigation():
    """An extra Stats update-status fetch must respect page generation."""
    start = ui.JS.index("RENDER.stats=async()=>")
    stop = ui.JS.index("RENDER.queue=async()=>", start)
    setup = _BASE.replace(
        "const j=async()=>data;",
        "let activePage='stats',pageVersion=1,resume;"
        "const j=url=>url==='/api/v1/stats'?Promise.resolve(data.stats)"
        ":url==='/api/v1/update'?new Promise(resolve=>{resume=resolve;})"
        ":Promise.reject(new Error('optional audit unavailable'));",
    )
    script = setup + _helpers() + ui.JS[start:stop] + """
const pending=RENDER.stats();
root.innerHTML='<div>New page</div>';
activePage='status';pageVersion=2;
resume(data.update);
pending.then(()=>console.log(JSON.stringify(root.innerHTML)))
  .catch(e=>{console.error(String(e));process.exitCode=1;});
"""
    assert json.loads(_node(script, {
        "stats": {"version": "1.8.1"},
        "update": _payload(),
    })) == "<div>New page</div>"


def test_status_ignores_late_health_response_after_navigation():
    """Health data arriving late must not repaint another page."""
    start = ui.JS.index("RENDER.status=async()=>")
    stop = ui.JS.index("\n};\n", start) + 4
    setup = _BASE.replace(
        "const j=async()=>data;",
        "let activePage='status',pageVersion=1,resume;"
        "const j=()=>new Promise(resolve=>{resume=resolve;});",
    )
    script = setup + _helpers() + ui.JS[start:stop] + """
const pending=RENDER.status();
root.innerHTML='<div>New page</div>';
activePage='update';pageVersion=2;
resume(data);
pending.then(()=>console.log(JSON.stringify(root.innerHTML)))
  .catch(e=>{console.error(String(e));process.exitCode=1;});
"""
    assert json.loads(_node(script, {"version": "1.8.1"})) == "<div>New page</div>"


def test_stats_and_status_use_same_verified_build_presentation():
    """Cross-page UI should share one escaped build-identity presenter."""
    source = ui.JS
    assert "buildIdentityLine(h.build)" in source
    assert "buildIdentityLine(d.build)" in source
    assert "up.running_version===s.version?up.build:null" in source
    for marker in ("RENDER.stats=async()=>", "RENDER.status=async()=>",
                   "RENDER.update=async()=>", "RENDER.queue=async()=>"):
        assert marker in source

@pytest.mark.parametrize("phase", ["health", "formats"])
def test_status_overlapping_refreshes_keep_newest_result(phase):
    """Overlapping Status renders must not let an earlier response win."""
    start = ui.JS.index("RENDER.status=async()=>")
    stop = ui.JS.index("\n};\n", start) + 4
    setup = r"""
const data=JSON.parse(process.argv[1]);
const root={innerHTML:''},controls={},RENDER={};
let activePage='status',pageVersion=1;
const $=selector=>selector==='#page'?root:(controls[selector]??={
  innerHTML:'',textContent:'',style:{},value:'',querySelectorAll:()=>[]
});
const esc=s=>String(s??'');
const buildIdentityLine=()=>'',ggrequestzCard=()=>'';
const playCard=()=>'',moonlightCard=()=>'';
const wireGGRequestz=()=>{},wireMoonlight=()=>{};
const health=[],formats=[];
const j=url=>new Promise(resolve=>{
  if(url==='/api/v1/system/status')health.push(resolve);
  else if(url==='/api/v1/frontend/formats')formats.push(resolve);
  else throw Error('unexpected request '+url);
});
"""
    exercise = r"""
(async()=>{
  const oldRender=RENDER.status();
  let latestRender;
  if(data.phase==='health'){
    latestRender=RENDER.status();
    health[1]({version:'CURRENT-VERSION'});await Promise.resolve();
    health[0]({version:'STALE-VERSION'});await Promise.resolve();
    const ok=root.innerHTML.includes('CURRENT-VERSION')
      &&!root.innerHTML.includes('STALE-VERSION');
    for(const finish of formats)finish({formats:[]});
    await Promise.all([oldRender,latestRender]);
    return ok;
  }
  health[0]({version:'STALE-VERSION'});await Promise.resolve();
  latestRender=RENDER.status();
  health[1]({version:'CURRENT-VERSION'});await Promise.resolve();
  formats[1]({formats:[{name:'current',label:'CURRENT-FORMAT'}]});
  await Promise.resolve();
  formats[0]({formats:[{name:'stale',label:'STALE-FORMAT'}]});
  await Promise.resolve();
  await Promise.all([oldRender,latestRender]);
  return controls['#fe-row'].innerHTML.includes('CURRENT-FORMAT')
    &&!controls['#fe-row'].innerHTML.includes('STALE-FORMAT');
})().then(ok=>console.log(JSON.stringify({ok})))
  .catch(e=>{console.error(String(e));process.exitCode=1;});
"""
    script = setup + ui.JS[start:stop] + exercise
    assert json.loads(_node(script, {"phase": phase}))["ok"]
