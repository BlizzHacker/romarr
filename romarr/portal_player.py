"""Compatibility shim for RomM's asynchronously mounted EmulatorJS player."""

# RomM's v2 shell starts loading EmulatorJS independently of its async player
# component. The script can execute before that component provides EJS_player.
# Intercept only the known loaders on a RomM player route. Leave foreign
# resources and normal DOM operations alone.
PLAYER_SHIM = r"""
(function(){
  const append=Node.prototype.appendChild;
  const assetRoot='/assets/emulatorjs/data';
  Node.prototype.appendChild=function(node){
    if(node?.localName!=='script'||!/^\/rom\/\d+\/ejs\/?$/.test(location.pathname)){
      return append.call(this,node);
    }
    const loader=new URL(node.src,location.href);
    const local=loader.origin===location.origin&&loader.pathname===assetRoot+'/loader.js';
    const cdn=loader.origin==='https://cdn.emulatorjs.org'&&
      /^\/(?:nightly|[\d.]+)\/data\/loader\.js$/.test(loader.pathname);
    if(!local&&!cdn)return append.call(this,node);

    const destination=this;
    const page=location.pathname+location.search;
    const expires=Date.now()+10000;
    const stillHere=()=>page===location.pathname+location.search&&destination.isConnected;
    const fail=()=>node.dispatchEvent(new Event('error'));
    const bundledRoot=rw(assetRoot);
    const attach=()=>{
      if(stillHere())append.call(destination,node);
      else fail();
    };
    const bundled=()=>{
      window.EJS_pathtodata=bundledRoot;
      node.src=bundledRoot+'/loader.js';
      attach();
    };
    function start(){
      if(!stillHere()||Date.now()>=expires){fail();return;}
      if(window.EJS_player!=='#game'||!document.getElementById('game')){
        setTimeout(start,25);return;
      }
      if(local){bundled();return;}
      // The mutable nightly mGBA package can fail during WASM startup. In
      // the embedded shell, use the matching bundle shipped by RomM.
      if(window.top!==window&&loader.pathname.startsWith('/nightly/')&&
         /^(?:gba|mgba)$/.test(window.EJS_core||'')&&!window.EJS_threads){
        fetch(bundledRoot+'/loader.js',{signal:AbortSignal.timeout(2500)})
          .then(response=>response.ok&&/javascript|ecmascript/i.test(response.headers.get('Content-Type')||''))
          .then(ready=>{if(!stillHere())fail();else if(ready)bundled();else attach();})
          .catch(attach);
        return;
      }
      attach();
    }
    start();
    return node;
  };
})();
"""
