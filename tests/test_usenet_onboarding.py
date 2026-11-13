"""Usenet onboarding choices, search guidance and escaped library progress."""
import shutil
import subprocess

import pytest

from romarr import ui
from romarr.downloaders import CLIENT_TYPES
from romarr.indexers import INDEXER_TYPES


def run_js(code):
    """Run browser-independent interaction checks without starting a service."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for onboarding interaction checks")
    result = subprocess.run([node, "-e", code], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_setup_choices_open_existing_protocol_editors_and_ignore_late_saves():
    """Each shortcut opens the correct existing editor, and navigation stays put."""
    assert {"builtin_nzb", "mdm_torrent", "mdm_http", "hub"} <= CLIENT_TYPES.keys()
    assert "newznab" in INDEXER_TYPES
    script = ui.JS[ui.JS.index("const SETUP="):ui.JS.index("RENDER.getstarted=")]
    harness = r"""
const assert=require('assert'),RENDER={},nodes={},opened=[],calls=[];
let pageVersion=1,activePage='setup';
const state={libraries:[],download_clients:[],indexers:[],dat_path:'',dat_loaded:0,hub_available:true,mode:'download'};
const $=selector=>nodes[selector]||(nodes[selector]={innerHTML:'',value:'',className:'',disabled:false});
const document={querySelectorAll:()=>[]};
const j=async url=>{calls.push(url);return state;};
const esc=x=>String(x??''),fmt=x=>String(x),toast=()=>{},go=()=>{};
const editItem=(kind,item,afterSave)=>opened.push({kind,item,afterSave});
const addItem=(kind,afterSave)=>opened.push({kind,afterSave});
"""
    checks = r"""
(async()=>{
 SETUP.initialized=true;SETUP.mode='download';SETUP.step=2;
 await RENDER.setup();
 for(const [id,type] of [['setup-usenet','builtin_nzb'],['setup-torrent','mdm_torrent'],['setup-direct','mdm_http'],['setup-hub-client','hub']]){
   assert($('#page').innerHTML.includes('id="'+id+'"'));
   $('#'+id).onclick();
   assert.equal(opened.at(-1).kind,'downloadclient');
   assert.equal(opened.at(-1).item.type,type);
 }
 assert($('#page').innerHTML.includes('NNTP server and login'));
 $('#setup-client').onclick();assert.equal(opened.at(-1).kind,'downloadclient');
 SETUP.step=3;await RENDER.setup();
 assert($('#page').innerHTML.includes('Usenet without a torrent indexer'));
 $('#setup-newznab').onclick();assert.equal(opened.at(-1).kind,'indexer');
 assert.equal(opened.at(-1).item.type,'newznab');
 const saved=opened.at(-1).afterSave,count=calls.length;
 activePage='library';pageVersion++;saved();
 await new Promise(setImmediate);
 assert.equal(calls.length,count,'a late editor save must not reopen Setup');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    run_js(harness + script + checks)


def test_no_results_explains_source_checks_before_download_failures_and_guards_searches():
    """Empty results offer usable source checks; older searches cannot overwrite newer ones."""
    script = ui.JS[ui.JS.index("RENDER.search=async()=>{"):ui.JS.index("RENDER.missing=")]
    harness = r"""
const assert=require('assert'),RENDER={},nodes={},PLATFORMS=[],pending=[];
let pageVersion=1,activePage='search';
const $=selector=>nodes[selector]||(nodes[selector]={innerHTML:'',value:'',disabled:false});
const document={querySelectorAll:()=>[]};
const enhancePlatformSelect=()=>{},toast=()=>{},refreshCounts=()=>{};
const esc=x=>String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const j=url=>new Promise(resolve=>pending.push({url,resolve}));
"""
    checks = r"""
(async()=>{
 await RENDER.search();$('#s-name').value='Example Game';$('#s-plat').value='snes';
 const older=$('#s-go').onclick(),newer=$('#s-go').onclick();
 pending[1].resolve({items:[],found:0,accepted:0});await newer;
 const guidance=$('#s-out').innerHTML;
 assert(guidance.includes('No releases returned'));
 assert(guidance.includes('No download was started'));
 assert(guidance.includes('Newznab indexer'));
 for(const page of ['indexers','queue','history','hub'])assert(guidance.includes('href="#'+page+'"'));
 pending[0].resolve({error:'older result'});await older;
 assert.equal($('#s-out').innerHTML,guidance);
 const leaving=$('#s-go').onclick();
 activePage='library';pageVersion++;$('#s-out').innerHTML='New page';
 pending[2].resolve({error:'late response'});await leaving;
 assert.equal($('#s-out').innerHTML,'New page');
 assert(pending.every(item=>item.url.startsWith('/api/v1/release?')));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    run_js(harness + script + checks)


def test_indexers_explains_newznab_provider_and_client():
    """The actual Indexers renderer keeps the separate roles visible."""
    start = ui.JS.index("RENDER.indexers=async()=>{")
    script = ui.JS[start:ui.JS.index("RENDER.general=", start)]
    run_js(r"""
const assert=require('assert'),RENDER={},nodes={};
let pageVersion=1,activePage='indexers';
const $=selector=>nodes[selector]||(nodes[selector]={innerHTML:''});
const document={querySelectorAll:()=>[]};
const j=async()=>({items:[],proxied:[]}),esc=x=>String(x??''),addItem=()=>{},editItem=()=>{};
""" + script + r"""
(async()=>{
 await RENDER.indexers();
 const html=$('#page').innerHTML;
 assert(html.includes('Newznab for Usenet'));
 assert(html.includes('NNTP server and login'));
 assert(html.includes('download client'));
 assert(html.includes('Usenet without a torrent indexer'));
 assert(html.includes('connectivity, not game availability'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_library_progress_keeps_partial_scans_visible_and_escapes_server_messages():
    """A NAS error cannot become HTML, and partial shelves carry useful progress."""
    start = ui.JS.index("function libraryScanHelp(d){")
    script = ui.JS[start:ui.JS.index("RENDER.library=", start)]
    run_js(r"""
const assert=require('assert');
const num=x=>String(x);
const esc=x=>String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
""" + script + r"""
const html=libraryScanHelp({partial:true,scan_message:'<script>unsafe</script>',
 scan:{running:true,library:'<NAS>',stage:'reading',read:42,elapsed_seconds:12}});
assert(html.includes('42 games read'));assert(html.includes('12 s elapsed'));
assert(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));assert(!html.includes('<script>'));
assert(html.includes('&lt;NAS&gt;'));assert(html.includes('NAS-LIBRARIES.md'));
assert(html.includes('games read so far'));
assert.equal(libraryScanHelp({scan:{stage:'complete',running:false}}),'');
assert(libraryScanHelp({scan:{stage:'error'},scan_message:'Mount unavailable'}).includes('Mount unavailable'));
""")

def test_stopped_partial_library_scans_do_not_claim_to_be_running():
    """The rendered shelf distinguishes active reads from failed or capped reads."""
    start = ui.JS.index("function libraryScanHelp(d){")
    script = ui.JS[start:ui.JS.index("function bindLibraryChips", start)]
    run_js(r"""
const assert=require('assert'),RENDER={},SOURCE_LABEL={};
const LIB={platform:'',genre:'',region:'',decade:'',origin:'',source:'',sort:'',offset:0,limit:120};
const page={innerHTML:'',querySelector:()=>null};
const $=selector=>selector==='#page'?page:{value:''};
const esc=x=>String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=x=>String(x),bindLibraryChips=()=>{};
let response;
const j=async()=>response;
""" + script + r"""
(async()=>{
 const base={loading:false,partial:true,items:[],total:0,grand_total:7,cached_total:7,
   totals:{on_disk:7,catalogued:0,total:7,counted_from:'cached rows'},facets:{},platforms:[]};
 for(const [stage,message] of [['error','Network mount unavailable'],['complete','Game limit reached']]){
   response={...base,scan_message:message,scan:{running:false,stage,library:'NAS',read:7,elapsed_seconds:4}};
   await RENDER.library();
   assert(page.innerHTML.includes(message));
   assert(page.innerHTML.includes('scan incomplete'));
   assert(page.innerHTML.includes('7 games read'));
   assert(!/read is still running|still reading|still loading/.test(page.innerHTML));
   assert(!page.innerHTML.includes('Library scan complete'));
 }
 response={...base,scan_message:'Reading records',scan:{running:true,stage:'reading',read:7,elapsed_seconds:4}};
 await RENDER.library();
 assert(page.innerHTML.includes('still loading'));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
