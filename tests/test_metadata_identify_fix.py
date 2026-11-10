"""Metadata Identify regressions for the embedded browser JavaScript.

The tests use simulated browser requests without calling a live ROMarr API.
"""
import shutil
import subprocess

import pytest
from romarr import ui


def test_metadata_identify_hooks_are_scoped():
    """Require the expected Metadata handlers in the embedded JavaScript."""
    source=ui.JS
    meta=source[source.index('RENDER.metadata=async()=>{'):source.index('// --- Calendar')]
    assert "metadataCachedFilename===input.value.trim()" in meta
    assert "result?.found && result.title && result.matched_by" in meta
    assert "if(result?.error) throw new Error" in meta
    assert "input.onkeydown=e=>" in meta
    assert "if(e.key==='Enter')" in meta
    assert "metadataFilename=input.value" in meta
    assert "mine.revision!==metadataProviderRevision" in meta
    assert "if(kind==='metadataprovider') metadataInvalidate();" in source
    assert "metadataInvalidate();" in source
    assert "metadataLookup?.abort();" in source
    assert "location.hash==='#metadata'" in meta
    assert 'Chrono.Trigger.USA.v1.1.smc' in source
    assert 'metadata-table-scroll' not in meta


def test_metadata_identify_cache_and_stale_requests_in_node():
    """Exercise filename retention, cache invalidation, and stale requests."""
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
let providerRevision='first';
let providerResponse='ok';
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
const fetch=u=>{
  assert.equal(u,'/api/v1/metadataprovider');
  if(providerResponse==='offline')return Promise.reject(Error('offline'));
  if(providerResponse==='http-error')
    return Promise.resolve({ok:false,json:async()=>({error:'Unavailable'})});
  if(providerResponse==='malformed')
    return Promise.resolve({ok:true,json:async()=>({items:[]})});
  return Promise.resolve({ok:true,json:async()=>({items:[],revision:providerRevision})});
};
const j=(u,options)=>{
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

  // A different tab/API client changes a provider: force a fresh lookup.
  providerRevision='changed-from-another-tab';
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,11,'provider revision changed: relookup required');
  pending[10].resolve({matched_by:'filename',found:true,title:'After provider change'});
  await tick();
  assert(output.innerHTML.includes('After provider change'));
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,11,'unchanged provider revision preserves success');
  assert(output.innerHTML.includes('After provider change'));

  // Do not invalidate or consume provider quota when verification fails.
  for(const failure of ['offline','http-error','malformed']){
    providerResponse=failure;
    pageVersion++;
    await RENDER.metadata();
    assert.equal(metadataCachedFilename,'Another.sfc');
    assert.equal(metadataProviderRevision,'changed-from-another-tab');
    assert.equal(pending.length,11,'failed provider fetch must not auto-lookup');
    assert(output.textContent.includes('Provider settings unavailable'));
    assert(!output.innerHTML.includes('After provider change'),
      'an unverified cached result must not be presented as current');
  }
  providerResponse='ok';
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,11,'unchanged revision after outage reuses cache');
  assert(output.innerHTML.includes('After provider change'));
  providerRevision='changed-after-outage';
  pageVersion++;
  await RENDER.metadata();
  assert.equal(pending.length,12,'changed revision after outage must relookup');
  pending[11].resolve({matched_by:'filename',found:true,title:'Updated after outage'});
  await tick();
  assert(output.innerHTML.includes('Updated after outage'));
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
  return {ok:mode==='ok',status:mode==='absent'?404:mode==='http-error'?500:200};
};
''' + handler + r'''
(async()=>{
  await del.onclick();
  assert.deepEqual(events,['toast:Remove failed'], 'network failures do not close dialog');
  events.length=0; mode='http-error';
  await del.onclick();
  assert.deepEqual(events,['toast:Remove failed'], 'HTTP failures do not close dialog');
  events.length=0; mode='absent';
  await del.onclick();
  assert.deepEqual(events,['invalidate','close','toast:Removed','go:metadata'],
    '404 means the entry is absent; refresh the list');
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


def test_metadata_revision_changes_when_providers_reload(tmp_path):
    """Use an opaque revision to identify reloads without exposing credentials."""
    from romarr.app import ROMarr

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    initial = service.metadata_revision
    assert len(initial) == 32
    service.store.put_item("metadata_providers", {
        "type": "rawg", "name": "Example", "api_key": "secret", "enable": True,
    })
    service.reload_metadata()
    assert service.metadata_revision != initial
    assert "secret" not in service.metadata_revision
    current = service.metadata_revision
    service.reload_metadata()
    assert service.metadata_revision == current, 'no-op reload should preserve revision'
    assert service.store.delete_item('metadata_providers', 'absent-provider') is False
    service.reload_metadata()
    assert service.metadata_revision == current, 'absent deletion should not rotate revision'
    # A real change must still rotate the revision, including nested changes.
    service.store.settings['metadata_providers'][0]['enable'] = False
    service.reload_metadata()
    assert service.metadata_revision != current
    assert service.metadata.providers[0]['enable'] is False


def test_restore_reloads_metadata_providers_over_http(tmp_path):
    """A restored provider configuration must update the live chain and revision."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from romarr.app import ROMarr, make_handler
    from romarr.ops import make_backup

    service=ROMarr({'ROMARR_DATA':str(tmp_path/'settings.json'),
                    'ROMARR_API_KEY':'testkey'})
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(service))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{server.server_address[1]}'

    def request(path,method='GET',body=None):
        """Use the authenticated loopback endpoint for restore assertions."""
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(base+path,data=data,method=method,
             headers={'X-Api-Key':'testkey','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=10) as response:
            assert response.status==200
            return json.load(response)

    try:
        original=service.metadata_revision
        new_providers=[{'type':'rawg','name':'Restored','enable':False,
                        'api_key':'secret'}]
        body=make_backup({**service.store.settings,
                          'metadata_providers':new_providers},include_secrets=True)
        assert request('/api/v1/restore','POST',body)['ok'] is True
        assert service.metadata.providers==new_providers
        assert service.metadata_revision!=original
        payload=request('/api/v1/metadataprovider')
        assert payload['revision']==service.metadata_revision
        assert payload['items'][0]['api_key']=='********'
        assert 'secret' not in payload['revision']
        restored=service.metadata_revision
        assert request('/api/v1/restore','POST',body)['ok'] is True
        assert service.metadata_revision==restored, 'identical restore should not rotate revision'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
