"""Every page in the nav has a renderer, and every renderer is reachable.

The counterpart to `test_wired.py`, one layer up. A nav entry with no
`RENDER.<page>` produces a menu item that clicks to a blank screen -- the UI
version of a module nobody calls, and just as invisible to a passing test
suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from romarr.ui import JS, NAV, page


SOURCE = page()


def nav_pages() -> list[str]:
    return [slug for _, items in NAV for slug, _, _ in items]


@pytest.mark.parametrize("slug", nav_pages())
def test_every_nav_entry_has_a_renderer(slug):
    assert f"RENDER.{slug}=" in SOURCE, (
        f"'{slug}' is in the nav but has no RENDER.{slug} -- it clicks to a "
        "blank screen")


@pytest.mark.parametrize("slug", nav_pages())
def test_every_nav_entry_has_a_page_title(slug):
    titles = re.search(r"const titles=\{(.*?)\};", SOURCE, re.S)
    assert titles, "the title map moved"
    assert f"{slug}:" in titles.group(1), f"'{slug}' has no page title"


def test_every_renderer_is_in_the_nav():
    """A renderer nobody can navigate to is dead code with a stylesheet."""
    rendered = set(re.findall(r"RENDER\.([a-z]+)=", SOURCE))
    assert rendered - set(nav_pages()) == {"game"}
    assert "function gameHref(" in SOURCE
    assert "go(gameHref(" in SOURCE


def test_the_pages_added_for_the_new_backends_exist():
    """Blocklist, connections, metadata, calendar and manual import all had
    working endpoints and no way to reach them."""
    for slug in ("blocklist", "connections", "metadata", "calendar",
                 "manualimport"):
        assert f"RENDER.{slug}=" in SOURCE, slug


def test_the_catalogue_grid_does_not_collide_with_the_library_grid():
    """`.grid` is the library's 150px poster grid and is defined later in the
    same stylesheet, so it won on equal specificity and rendered the plugin
    catalogue as seven cramped columns. Caught by looking at a screenshot --
    the markup was correct and every test passed.
    """
    assert ".pgrid{" in SOURCE
    assert 'class="pgrid"' in SOURCE


def test_collections_rejects_stale_source_previews_and_escapes_restore_errors():
    """A source preview only applies to the current visit and input snapshot."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Collections renderer harness")
    start = JS.index("RENDER.collections=async()=>{")
    end = JS.index("\nRENDER.manualimport=", start)
    harness = """
const assert=require('node:assert/strict');
const RENDER={}, nodes={}, SETTINGS={}, PLATFORMS=[], pending=[];
let activePage='collections',pageVersion=1,initial;
const $=id=>nodes[id]||(nodes[id]={
  value:'',innerHTML:'',events:{},
  querySelectorAll:()=>[],addEventListener(name,fn){this.events[name]=fn;}
});
$('#cplat').value='snes';
$('#cdat').value='Demo';
$('#csrc-cap').value='100';
const first={value:'hash',dataset:{client:'one'}};
$('#csrc-torrent').selectedOptions=[first];
const enhancePlatformSelect=()=>{}, fmtBytes=String, confirm=()=>true;
const esc=s=>String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;');
const j=(url,options)=>{
  if(url==='/api/v1/collection')return initial||Promise.resolve({dats:['Demo']});
  if(url==='/api/v1/collection/source'||url==='/api/v1/collection/source?all=1')return Promise.resolve({
    torrents:[{hash:'hash',client_id:'one'}],
    jobs:[{status:'restore-needed',errors:['<restore error>']}]
  });
  if(options) return new Promise(resolve=>pending.push({url,resolve}));
  throw Error(url);
};
"""
    checks = """
(async()=>{
  await RENDER.collections();
  await new Promise(setImmediate);
  assert.match($('#csrc-jobs').innerHTML,/&lt;restore error&gt;/);
  assert.doesNotMatch($('#csrc-jobs').innerHTML,/<restore error>/);
  const button=$('#csrc-plan'), out=$('#csrc-out');
  const plan={matched_titles:1,batches:[]};
  const stale=button.onclick();
  $('#csrc-cap').value='99';
  $('#csrc-cap').events.input();
  pending.shift().resolve(plan);
  await stale;
  assert.match(out.innerHTML,/Selection changed/);

  const old=button.onclick(), latest=button.onclick();
  const olderResponse=pending.shift();
  pending.shift().resolve(plan);
  await latest;
  assert.match(out.innerHTML,/1 missing title/);
  olderResponse.resolve({...plan,matched_titles:2});
  await old;
  assert.match(out.innerHTML,/1 missing title/);

  $('#csrc-torrent').selectedOptions=[{...first,dataset:{client:'two'}}];
  await $('#csrc-start').onclick();
  assert.equal(pending.length,0);
  assert.match(out.innerHTML,/Selection changed/);

  let finish;
  initial=new Promise(resolve=>finish=resolve);
  const leaving=RENDER.collections();
  pageVersion++;
  $('#page').innerHTML='Next visit';
  finish({});
  await leaving;
  assert.equal($('#page').innerHTML,'Next visit');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", harness + JS[start:end] + checks],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
