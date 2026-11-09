"""Metadata Identify regressions for the embedded browser JavaScript.

The tests use simulated browser requests without calling a live ROMarr API.
"""
import shutil
import subprocess

import pytest
from romarr import ui


def test_metadata_identify_hooks_are_scoped():
    source=ui.JS
    meta=source[source.index('RENDER.metadata=async()=>{'):source.index('// --- Calendar')]
    assert "metadataCachedFilename===input.value.trim()" in meta
    assert "result?.found && result.title && result.matched_by" in meta
    assert "if(result?.error) throw new Error" in meta
    assert "input.onkeydown=e=>" in meta
    assert "if(e.key==='Enter')" in meta
    assert "metadataFilename=input.value" in meta
    assert "if(kind==='metadataprovider') metadataInvalidate();" in source
    assert "metadataInvalidate();" in source
    assert "metadataLookup?.abort();" in source
    assert "location.hash==='#metadata'" in meta
    assert 'Chrono.Trigger.USA.v1.1.smc' in source
    assert 'metadata-table-scroll' not in meta


def test_metadata_identify_cache_and_stale_requests_in_node():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed; browser logic NOT TESTED')
    source=ui.JS
    state=source[source.index('let activePage=null, pageVersion=0, metadataLookup=null;'):
                 source.index('function go(page)')]
    metadata=source[source.index('RENDER.metadata=async()=>{'):
                    source.index('// --- Calendar')]
    script = r'''
const assert=require('node:assert/strict');
const location={hash:'#metadata'};
const RENDER={};
let input=null, output=null, identify=null;
const page={};
let pageHtml='';
Object.defineProperty(page,'innerHTML',{
  get(){return pageHtml;},
  set(html){
    pageHtml=html;
    if(html.includes('id="mfn"')){
      const match=html.match(/<input id="mfn" value="([^"]*)"/);
      assert(match,'expected filename field');
      input={value:match[1],oninput:null,onkeydown:null};
      let inner='',plain='';
      output={isConnected:true};
      Object.defineProperty(output,'innerHTML',{
        get(){return inner;},set(v){inner=v;plain='';}});
      Object.defineProperty(output,'textContent',{
        get(){return plain;},set(v){plain=v;inner='';}});
      identify={onclick:null};
    }
  }
});
const $=id=>({'#page':page,'#mfn':input,'#mout':output,
  '#mgo':identify,'#mp-add':{onclick:null}})[id];
const document={querySelectorAll:()=>[]};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pending=[];
const j=(u,options)=>{
  if(u==='/api/v1/metadataprovider')return Promise.resolve({items:[]});
  if(u==='/api/v1/metadata/schema')return Promise.resolve({providers:[]});
  if(u.startsWith('/api/v1/metadata/lookup?'))
    return new Promise((resolve,reject)=>pending.push({url:u,signal:options.signal,resolve,reject}));
  throw new Error('Unexpected request '+u);
};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const addItem=()=>{};
const editItem=()=>{};
''' + state + r'''
''' + metadata + r'''
(async()=>{
  activePage='metadata';pageVersion=1;
  await RENDER.metadata();
  assert.equal(input.value,'Chrono.Trigger.USA.v1.1.smc');
  assert.equal(pending.length,1);
  assert.equal(output.textContent,'Looking up...');
  pending[0].resolve({matched_by:'filename',found:true,title:'Chrono Trigger'});
  await tick();
  assert(output.innerHTML.includes('Chrono Trigger'));
  assert.equal(metadataCachedFilename,'Chrono.Trigger.USA.v1.1.smc');

  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,1,'cached success must not request Chrono twice');
  assert(output.innerHTML.includes('Chrono Trigger'));

  input.value='Super.Metroid.sfc';input.oninput();
  assert.equal(output.textContent,'Click Identify to run a lookup.');
  let prevented=false;
  input.onkeydown({key:'Enter',preventDefault(){prevented=true;}});
  assert(prevented);
  assert.equal(pending.length,2);
  assert(pending[1].url.includes('Super.Metroid.sfc'));
  pending[1].resolve({matched_by:'filename',found:true,title:'Super Metroid'});
  await tick();
  assert(output.innerHTML.includes('Super Metroid'));
  pageVersion++;
  await RENDER.metadata();
  assert.equal(input.value,'Super.Metroid.sfc');
  assert.equal(pending.length,2);
  assert(output.innerHTML.includes('Super Metroid'));

  const refreshing=identify.onclick(); // manual refresh must still run
  assert.equal(pending.length,3);
  pending[2].resolve({matched_by:'filename',found:true,title:'Super Metroid refreshed'});
  await refreshing;
  await tick();
  assert(output.innerHTML.includes('Super Metroid refreshed'));

  input.value='  ';input.oninput();
  await identify.onclick();
  assert.equal(pending.length,3,'blank filename must never hit the API');
  assert.equal(output.textContent,'Enter a filename to identify.');

  input.value='NetworkFail.sfc';input.oninput();identify.onclick();
  pending[3].reject(new Error('offline'));
  await tick();
  assert(output.textContent.includes('Lookup failed'));

  input.value='ApiError.sfc';input.oninput();identify.onclick();
  pending[4].resolve({error:'upstream provider error'});
  await tick();
  assert(output.textContent.includes('Lookup failed'));

  input.value='Nomatch.sfc';input.oninput();identify.onclick();
  pending[5].resolve({matched_by:'filename',found:false,title:''});
  await tick();
  assert(output.innerHTML.includes('Not identified'));
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,7,'a no-match must not be cached as success');
  pending[6].resolve({matched_by:'filename',found:true,title:'Found later'});
  await tick();

  input.value='Stale.sfc';input.oninput();identify.onclick();
  assert.equal(pending.length,8);
  const previous=pending[7];
  input.value='Another.sfc';input.oninput();
  assert(previous.signal.aborted);
  previous.resolve({matched_by:'filename',found:true,title:'Bad stale result'});
  await tick();
  assert(!output.innerHTML.includes('Bad stale result'));

  const anotherLookup=identify.onclick();
  assert.equal(pending.length,9);
  pending[8].resolve({matched_by:'filename',found:true,title:'Another'});
  await anotherLookup;
  await tick();
  assert(output.innerHTML.includes('Another'));
  metadataInvalidate();
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,10,'provider invalidation forces relookup');
  pending[9].resolve({matched_by:'filename',found:true,title:'Another new'});
  await tick();
  assert(output.innerHTML.includes('Another new'));
  console.log('METADATA_TEST_COMPLETE');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],text=True,capture_output=True,timeout=35)
    assert result.returncode==0,result.stderr
    assert "METADATA_TEST_COMPLETE" in result.stdout, "JavaScript assertions did not complete"


def test_metadata_provider_delete_errors_preserve_dialog_in_node():
    """Transport and HTTP deletion failures must not falsely claim success."""
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js not installed; delete-handler behavior NOT TESTED')
    source=ui.JS
    at=source.index("  const del = m.querySelector('#m-del');",source.index('async function editItem('))
    end=source.index('\n}\n\n/* ---------------- settings',at)
    handler=source[at:end]
    script=r'''
const assert=require('node:assert/strict');
const events=[];
let mode='network';
const kind='metadataprovider';
const item={id:'example',name:'Example'};
const spec={label:'Provider'};
const KINDS={metadataprovider:{page:'metadata'}};
const m={querySelector:()=>({onclick:null})};
const confirm=()=>true;
const closeModal=()=>events.push('close');
const toast=t=>events.push('toast:'+t);
const go=p=>events.push('go:'+p);
const metadataInvalidate=()=>events.push('invalidate');
const fetch=async(u,opts)=>{
  assert.equal(u,'/api/v1/metadataprovider/example');
  assert.equal(opts.method,'DELETE');
  if(mode==='network')throw Error('connection lost');
  return {ok:mode==='ok'};
};
''' + handler + r'''
(async()=>{
  await del.onclick();
  assert.deepEqual(events,['toast:Remove failed'], 'network failures do not close dialog');
  events.length=0; mode='http-error';
  await del.onclick();
  assert.deepEqual(events,['toast:Remove failed'], 'HTTP failures do not close dialog');
  events.length=0; mode='ok';
  await del.onclick();
  assert.deepEqual(events,['invalidate','close','toast:Removed','go:metadata'],
    'successful deletion must invalidate cache and navigate');
  console.log('METADATA_TEST_COMPLETE');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert "METADATA_TEST_COMPLETE" in result.stdout, "JavaScript assertions did not complete"
