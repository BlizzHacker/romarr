"""Responsive navigation contract and executable JavaScript regressions (#132)."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from romarr import ui


def run_sidebar(checks):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    fixture = Path(__file__).with_name('sidebar_dom.js').read_text()
    start = ui.JS.find('/* ---------------- sidebar ---------------- */')
    assert start >= 0, 'responsive sidebar controller is missing'
    end = ui.JS.index('/* ---------------- pages ---------------- */', start)
    script = 'const NAV=' + json.dumps(ui.NAV) + ';\n' + fixture + ui.JS[start:end] + checks
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_category_selection_and_hash_navigation_follow_active_page():
    run_sidebar(r'''
sidebarBindNav();
go('logs');
assert.equal(rendered,'logs');
assert.equal($('.navgroup[open]').dataset.group,'system');
assert.equal(document.querySelectorAll('.navgroup[open]').length,1);
assert.equal($('.nav[aria-current="page"]').dataset.page,'logs');
const system=$('.navhead[data-group="system"]');
system.fire('click');
assert.equal(rendered,'status');
system.fire('click');
assert.equal(rendered,'status');
assert.equal($('.navgroup[open]').dataset.group,'system');
go('ecosystem');
assert.equal($('.navgroup[open]').dataset.group,'hub');
location.hash='#getstarted';
window.events.hashchange[0]();
assert.equal(rendered,'getstarted');
assert.equal($('.navgroup[open]').dataset.group,'settings');
assert.equal(document.querySelectorAll('.navgroup[open]').length,1);
''')

def test_mobile_drawer_is_modal_and_categories_keep_it_open():
    html = ui.page()
    assert 'id="sidebar-menu"' in html, 'mobile menu control is missing'
    assert 'id="sidebar-close"' in html
    assert 'id="sidebar-scrim" hidden' in html
    assert 'aria-controls="rail"' in html
    assert '@media(max-width:700px)' in ui.CSS
    assert '--sidebar-visual-height' in ui.CSS
    run_sidebar(r'''
sidebarBindNav(); go('library'); menu.focus();
menu.fire('click');
assert(rail.classList.contains('drawer-open'));
assert.equal(main.inert,true);
assert.equal(scrim.hidden,false);
assert.equal(menu.getAttribute('aria-expanded'),'true');
assert(rail.contains(document.activeElement));
$('.navhead[data-group="settings"]').fire('click');
assert.equal(rendered,'general');
assert(rail.classList.contains('drawer-open'));
$('.nav[data-page="profiles"]').fire('click');
assert(!rail.classList.contains('drawer-open'));
assert.equal(main.inert,false);
assert.equal(document.activeElement,menu);
for(const method of ['escape','backdrop','close','resize']){
  menu.fire('click');
  assert(rail.classList.contains('drawer-open'));
  if(method==='escape') document.fire('keydown',{key:'Escape'});
  if(method==='backdrop') scrim.fire('click');
  if(method==='close') $('#sidebar-close').fire('click');
  if(method==='resize'){mobile=false;window.events.resize[0]();}
  assert(!rail.classList.contains('drawer-open'),method);
  assert.equal(main.inert,false);
  assert.equal(scrim.hidden,true);
}
mobile=true;menu.fire('click');
const first=$('#sidebar-close'),last=$('#appearance-toggle');
first.focus();
assert(document.fire('keydown',{key:'Tab',shiftKey:true}).prevented);
assert.equal(document.activeElement,last);
last.focus();
assert(document.fire('keydown',{key:'Tab',shiftKey:false}).prevented);
assert.equal(document.activeElement,first);
assert.equal(rail.style['--sidebar-visual-height'],'600px');
''')

def test_appearance_is_native_collapsible_with_css_only_presets():
    html = ui._theme_control_html()
    nodes = Elements(html).nodes
    details = [a for t, a in nodes if t == 'details']
    assert len(details) == 1, 'Appearance must be a native details accordion'
    assert details[0]['id'] == 'appearance-details'
    assert 'open' not in details[0]
    assert '<span>Appearance</span>' in html
    modes = [a for t, a in nodes if t == 'button' and 'data-theme-set' in a]
    assert [a['data-theme-set'] for a in modes] == ['dark', 'light', 'sepia']
    assert '>Warm</button>' in html
    accents = [a for t, a in nodes if t == 'button' and 'data-accent' in a]
    assert [a['data-accent'] for a in accents] == [v for v, _ in ui.ACCENTS]
    radios = [a for t, a in nodes if t == 'input' and a.get('type') == 'radio']
    assert len(radios) == 9
    for radio in radios:
        assert f":has(#{radio['id']}:checked)" in ui.CSS
    assert 'id="sidebar-menu-native"' in ui.page()
    assert '#sidebar-native-toggle:checked ~ #rail' in ui.CSS
    assert '#appearance-toggle' in ui.CSS
    assert 'position:sticky' in ui.CSS
    assert 'romarr.theme' in ui.THEME_BOOT and 'romarr.accent' in ui.THEME_BOOT

def test_appearance_controls_report_selection_and_preview_custom_accent():
    theme = ui.JS[ui.JS.index('function paintTheme'):ui.JS.index('/* ---------------- boot')]
    run_sidebar(theme + r'''
const light=make('button',{'data-theme-set':'light'},appearance);
const warm=make('button',{'data-theme-set':'sepia'},appearance);
const amber=make('button',{'data-accent':'#f2a33c'},appearance);
const teal=make('button',{'data-accent':'#2dd4bf'},appearance);
const custom=make('label',{class:'sw pick'},appearance);
const picker=make('input',{id:'accent-pick'},custom);
let mode='sepia',accent='#2DD4BF';
const ROMTHEME={mode:()=>mode,accent:()=>accent,set(m,a){
  mode=m||mode;accent=a||accent;document.documentElement.fire('themechange');
}};
bindTheme();
assert.equal(warm.getAttribute('aria-pressed'),'true');
assert.equal(light.getAttribute('aria-pressed'),'false');
assert.equal(teal.getAttribute('aria-pressed'),'true');
assert.equal(amber.getAttribute('aria-pressed'),'false');
assert(!custom.classList.contains('on'));
light.onclick();assert.equal(mode,'light');
assert.equal(light.getAttribute('aria-pressed'),'true');
picker.value='#123456';picker.oninput();
assert.equal(accent,'#123456');
assert(custom.classList.contains('on'));
assert.equal(teal.getAttribute('aria-pressed'),'false');
amber.onclick();assert.equal(accent,'#f2a33c');
assert(!custom.classList.contains('on'));
''')

def test_unknown_or_inherited_route_falls_back_to_selected_library():
    run_sidebar(r'''
sidebarBindNav();
for(const page of ['does-not-exist','toString','constructor','__proto__','<img src=x>']){
  go('logs');go(page);
  assert.equal(rendered,'library');
  assert.equal(location.hash,'library');
  assert.equal(heading.textContent,'Games');
  assert.equal($('.navgroup[open]').dataset.group,'library');
  assert.equal($('.nav[aria-current="page"]').dataset.page,'library');
}
''')

def test_mobile_drawer_preserves_modified_links_and_locks_background_scroll():
    run_sidebar(r'''
sidebarBindNav();go('library');menu.fire('click');
assert(document.documentElement.classList.contains('sidebar-drawer-open'));
const link=$('.nav[data-page="discover"]');
for(const key of ['ctrlKey','metaKey','shiftKey','altKey']){
  assert(!link.fire('click',{[key]:true}).prevented);
  assert.equal(rendered,'library');
  assert(rail.classList.contains('drawer-open'));
}
assert(link.fire('click').prevented);
assert.equal(rendered,'discover');
assert(!document.documentElement.classList.contains('sidebar-drawer-open'));
''')

def test_live_counts_update_parent_and_page_without_changing_selection():
    counts = ui.JS[ui.JS.index('async function refreshCounts'):ui.JS.index('(async()=>{', ui.JS.index('async function refreshCounts'))]
    run_sidebar(counts + r'''
let response={games:12345,missing:2,queued:7,games_on_disk:12000,games_catalogued:345};
const j=async()=>response;
(async()=>{
  sidebarBindNav();go('status');
  await refreshCounts();
  const queueBadges=document.querySelectorAll('[data-ct="queued"]');
  assert.equal(queueBadges.length,2);
  assert(queueBadges.every(b=>b.textContent===7));
  assert.equal($('.navgroup[open]').dataset.group,'system');
  response={games:null,missing:null,queued:0};await refreshCounts();
  assert(queueBadges.every(b=>b.textContent===''));
  assert.equal($('[data-ct="games"]').textContent,'—');
  assert.equal($('[data-ct="missing"]').style.display,'none');
  response={games:99,missing:3,queued:11};await refreshCounts();
  assert(queueBadges.every(b=>b.textContent===11 && b.style.display===''));
  assert.equal($('[data-ct="missing"]').style.display,'');
  assert.equal($('.nav[aria-current="page"]').dataset.page,'status');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def test_base_theme_rules_survive_an_unsupported_has_selector():
    # :is() has a forgiving selector list. A browser that understands the
    # existing theme but not :has() must retain the :root branch instead of
    # dropping the entire ordinary comma-separated rule as invalid.
    for base, fallback in [
        (':root', 'dark'),
        (':root[data-theme="light"]', 'light'),
        (':root[data-theme="sepia"]', 'sepia'),
    ]:
        assert f':is({base},\nhtml:not(.sidebar-ready):has(#fallback-mode-{fallback}:checked))' in ui.CSS


def test_native_navigation_preserves_routes_in_requested_order():
    expected = {
        'Library': 'library add discover search',
        'Wanted': 'missing lists calendar',
        'Activity': 'queue history blocklist',
        'Hub': 'hub portal peers ecosystem',
        'Settings': 'general media profiles indexers clients libraries connections metadata setup getstarted',
        'System': 'status stats update platforms collections manualimport tasks logs',
    }
    nodes = Elements(ui._nav_html()).nodes
    groups = [a for t, a in nodes if t == 'details']
    assert len(groups) == 6, 'categories must be native expandable details'
    assert [(g, [p for p, _, _ in items]) for g, items in ui.NAV] == [
        (g, pages.split()) for g, pages in expected.items()]
    assert [g['data-group'] for g in groups] == [g.lower() for g in expected]
    assert [g['data-group'] for g in groups if 'open' in g] == ['library']
    assert all(g['name'] == 'romarr-groups' for g in groups)
    links = [a for t, a in nodes if t == 'a']
    assert len(links) == len({a['data-page'] for a in links}) == 32
    assert all(a['href'] == '#' + a['data-page'] for a in links)
    assert [a['data-ct'] for _, a in nodes if 'data-ct' in a] == [
        'games', 'missing', 'queued', 'queued']
    heads = [a for t, a in nodes if t == 'summary']
    assert [h['data-first-page'] for h in heads] == [p.split()[0] for p in expected.values()]

def test_metadata_paints_before_lookup_and_ignores_superseded_responses():
    metadata = ui.JS[ui.JS.index("RENDER.metadata=async()=>{"):ui.JS.index("// --- Calendar")]
    run_sidebar(metadata + r"""
const content=make('div',{id:'page'},main);
let input,output,identify;
Object.defineProperty(content,'innerHTML',{set(html){
  for(const child of this.children) child.isConnected=false;
  this.children=[];
  if(html.includes('id="mfn"')){
    input=make('input',{id:'mfn'},this);input.value=html.match(/id="mfn" value="([^"]*)"/)[1];
    output=make('div',{id:'mout'},this);output.isConnected=true;
    identify=make('button',{id:'mgo'},this);
    make('button',{id:'mp-add'},this);
  }
}});
let pending=[],setupRequests=0;
const esc=value=>String(value);
const fetch=url=>{
  assert.equal(url,'/api/v1/metadataprovider');
  ++setupRequests;
  return Promise.resolve({ok:true,json:async()=>({items:[],revision:'fixture'})});
};
const j=(url,options)=>{
  if(!url.includes('/lookup?')){++setupRequests;return Promise.resolve({});}
  return new Promise(resolve=>pending.push({url,resolve,signal:options.signal}));
};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  go('metadata');
  location.hash='#metadata';
  window.events.hashchange[0]();
  await tick();
  assert.equal(setupRequests,2,'hashchange must not render Metadata twice');
  assert.equal(output.textContent,'Looking up...');
  assert.equal(pending.length,1);
  assert(pending[0].url.includes('Chrono.Trigger'));
  input.value='Manual.smc';input.oninput();
  assert(pending[0].signal.aborted);
  const manual=identify.onclick();
  pending[1].resolve({found:true,matched_by:'filename',title:'Manual result'});
  await manual;
  pending[0].resolve({matched_by:'filename',title:'Old example'});
  await tick();
  assert(output.innerHTML.includes('Manual result'));
  input.value='Pending.smc';
  const abandoned=identify.onclick();
  go('history');
  assert(pending[2].signal.aborted);
  assert.equal(rendered,'history');
  pending[2].resolve({matched_by:'filename',title:'Late result'});
  await abandoned;
  assert(!output.innerHTML.includes('Late result'));
  location.hash='#metadata';
  window.events.hashchange[0]();
  location.hash='#metadata';
  await tick();
  assert.equal(output.textContent,'Looking up...');
  assert.equal(setupRequests,4);
  assert.equal(input.value,'Pending.smc');
  assert(pending[3].url.includes('Pending.smc'));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
