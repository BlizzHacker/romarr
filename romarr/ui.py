"""The web UI.

Deliberately shaped like Radarr, Sonarr and Lidarr rather than like something
new. Anyone running an *arr stack already knows where things live -- Library,
Wanted, Activity, Settings, System, in that order down a dark rail on the left
-- and an *arr for games that invented its own arrangement would make its users
learn a layout for no benefit.

Where the concepts differ, the difference is real rather than cosmetic:

  * a Quality Profile has no meaning for a cartridge dump. There is no bitrate;
    what actually distinguishes two dumps of the same game is region, revision
    and whether it is a hack or a beta. So Profiles here rank regions.
  * Movies/Series/Artists become Games, and the Calendar has no equivalent at
    all -- a ROM has no air date -- so it is not in the nav rather than being
    present and permanently empty.

One file, no build step, no framework. This runs in a 512MB container beside a
download client and a database.
"""

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --rail:#2a2a2a; --rail-2:#333; --bg:#1c1c1c; --panel:#262626; --line:#3a3a3a;
  --fg:#e1e2e3; --dim:#999; --accent:#f2a33c; --accent-ink:#20130a;
  --ok:#27c24c; --warn:#ff9f1a; --bad:#f05050; --info:#5b9bd5;
}
body{background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}

/* ---- rail ---- */
#rail{position:fixed;left:0;top:0;bottom:0;width:210px;background:var(--rail);
  border-right:1px solid var(--line);overflow-y:auto;z-index:20}
#brand{display:flex;align-items:center;gap:9px;padding:16px 18px;
  font-size:19px;font-weight:700;letter-spacing:-.02em;border-bottom:1px solid var(--line)}
#brand span{color:var(--accent)}
.navgroup{padding:10px 0 4px}
.navhead{padding:6px 18px;font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;color:#777}
.nav{display:flex;align-items:center;gap:10px;padding:9px 18px;color:var(--fg);
  cursor:pointer;border-left:3px solid transparent;font-size:14px}
.nav:hover{background:var(--rail-2)}
.nav.on{background:var(--rail-2);border-left-color:var(--accent);color:var(--accent)}
.nav .ct{margin-left:auto;font-size:11px;background:#444;color:var(--dim);
  padding:1px 7px;border-radius:9px}
.nav.on .ct{background:var(--accent);color:var(--accent-ink)}

/* ---- main ---- */
#main{margin-left:210px;min-height:100vh}
#top{position:sticky;top:0;display:flex;align-items:center;gap:14px;
  padding:12px 22px;background:var(--rail);border-bottom:1px solid var(--line);z-index:10}
#top h1{font-size:17px;font-weight:600;white-space:nowrap}
#top input{flex:1;max-width:420px;padding:7px 12px;background:var(--bg);
  color:var(--fg);border:1px solid var(--line);border-radius:4px;outline:none}
#top input:focus{border-color:var(--accent)}
.page{padding:22px}
.hide{display:none !important}

/* ---- shared ---- */
.btn{padding:7px 15px;background:var(--accent);color:var(--accent-ink);
  font-weight:600;border:0;border-radius:4px;cursor:pointer;font-size:13px}
.btn:hover{filter:brightness(1.1)}
.btn.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
.btn.ghost:hover{background:var(--panel)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.card{background:var(--panel);border:1px solid var(--line);border-radius:5px;
  padding:16px;margin-bottom:14px}
.card h3{font-size:14px;margin-bottom:12px;color:var(--fg);font-weight:600}
.card p.help{color:var(--dim);font-size:12.5px;margin:-6px 0 12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);
  vertical-align:top;font-size:13px}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tr:hover td{background:#2b2b2b}
.pill{font-size:11px;padding:2px 9px;border-radius:10px;border:1px solid var(--line);
  display:inline-block;white-space:nowrap}
.pill.grabbed{color:var(--info);border-color:var(--info)}
.pill.imported{color:var(--ok);border-color:var(--ok)}
.pill.failed{color:var(--bad);border-color:var(--bad)}
.pill.queued{color:var(--warn);border-color:var(--warn)}
.empty{color:var(--dim);padding:26px 4px;text-align:center}
label{display:block;font-size:12.5px;color:var(--dim);margin-bottom:5px}
input[type=text],input[type=number],select{width:100%;padding:8px 11px;
  background:var(--bg);color:var(--fg);border:1px solid var(--line);
  border-radius:4px;outline:none;font-size:13px}
input:focus,select:focus{border-color:var(--accent)}
.field{margin-bottom:14px;max-width:420px}
.check{display:flex;align-items:center;gap:9px;margin-bottom:11px;color:var(--fg)}
.check input{width:16px;height:16px;accent-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:5px;overflow:hidden}
.tile .art{aspect-ratio:3/4;background:#1a1a1a center/cover no-repeat;display:block}
.tile .nm{padding:9px;font-size:12.5px;line-height:1.35}
.tile .pf{padding:0 9px 9px;font-size:11px;color:var(--dim)}
.st{display:flex;gap:22px;flex-wrap:wrap}
.st div{min-width:110px}
.st b{display:block;font-size:19px;font-weight:650}
.st span{font-size:12px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px}
.dot.up{background:var(--ok)}.dot.down{background:var(--bad)}
.dot.warn{background:var(--warn)}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:16px}
.tab{padding:9px 16px;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent;font-size:13px}
.tab.on{color:var(--accent);border-bottom-color:var(--accent)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:60;
  display:flex;align-items:flex-start;justify-content:center;padding:60px 20px;overflow-y:auto}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  width:100%;max-width:520px;padding:20px}
.modal h3{font-size:15px;margin-bottom:4px}
.modal .sub{color:var(--dim);font-size:12.5px;margin-bottom:16px}
.modal .foot{display:flex;gap:8px;margin-top:18px;align-items:center}
.modal .foot .sp{margin-left:auto}
.btn.danger{background:transparent;color:var(--bad);border:1px solid var(--bad)}
.pick{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.pick button{padding:14px;background:var(--bg);border:1px solid var(--line);
  color:var(--fg);border-radius:5px;cursor:pointer;text-align:left}
.pick button:hover{border-color:var(--accent)}
.pick small{display:block;color:var(--dim);font-size:11px;margin-top:3px}
.rowact{display:flex;gap:6px;justify-content:flex-end}
.rowact button{padding:4px 10px;font-size:12px;background:transparent;
  color:var(--dim);border:1px solid var(--line);border-radius:4px;cursor:pointer}
.rowact button:hover{color:var(--fg);border-color:var(--accent)}
.testline{font-size:12.5px;margin-top:10px}
.testline.ok{color:var(--ok)}.testline.bad{color:var(--bad)}
.toast{position:fixed;right:20px;bottom:20px;background:var(--panel);
  border:1px solid var(--accent);border-left-width:3px;border-radius:4px;
  padding:12px 16px;max-width:380px;z-index:50;font-size:13px}
@media(max-width:820px){
  #rail{width:56px}#rail .navhead,#rail .nav span.lbl,#brand span.txt{display:none}
  #main{margin-left:56px}.nav{justify-content:center;padding:12px 0}
}
"""

# The nav, in *arr order. `count` names a stat the badge reads.
NAV = [
    ("Library",  [("library", "Games", "games"), ("add", "Add New", None)]),
    ("Wanted",   [("missing", "Missing", "missing")]),
    ("Activity", [("queue", "Queue", "queued"), ("history", "History", None)]),
    ("Settings", [("media", "Media Management", None), ("profiles", "Profiles", None),
                  ("indexers", "Indexers", None), ("clients", "Download Clients", None),
                  ("libraries", "Libraries", None), ("general", "General", None)]),
    ("System",   [("status", "Status", None), ("tasks", "Tasks", None),
                  ("logs", "Logs", None)]),
]


def _nav_html() -> str:
    out = []
    for group, items in NAV:
        out.append(f'<div class="navgroup"><div class="navhead">{group}</div>')
        for key, label, count in items:
            badge = f'<b class="ct" data-ct="{count}"></b>' if count else ""
            out.append(
                f'<div class="nav" data-page="{key}">'
                f'<span class="lbl">{label}</span>{badge}</div>'
            )
        out.append("</div>")
    return "".join(out)


JS = r"""
const $=s=>document.querySelector(s);
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let PLATFORMS=[], SETTINGS={};

function toast(msg){
  const t=document.createElement('div');
  t.className='toast'; t.textContent=msg; document.body.append(t);
  setTimeout(()=>t.remove(),4200);
}

/* ---------------- routing ---------------- */
function go(page){
  location.hash=page;
  document.querySelectorAll('.nav').forEach(n=>
    n.classList.toggle('on', n.dataset.page===page));
  const titles={library:'Games',add:'Add New Game',missing:'Wanted — Missing',
    queue:'Queue',history:'History',media:'Media Management',profiles:'Profiles',
    indexers:'Indexers',clients:'Download Clients',libraries:'Libraries',
    general:'General',
    status:'System Status',tasks:'Tasks',logs:'Logs'};
  $('#top h1').textContent=titles[page]||'Romarr';
  $('#search').classList.toggle('hide', !['library','add'].includes(page));
  (RENDER[page]||RENDER.library)();
}
addEventListener('hashchange',()=>go(location.hash.slice(1)||'library'));

/* ---------------- pages ---------------- */
const RENDER={};

RENDER.library=async()=>{
  const p=$('#page'); p.innerHTML='<div class="empty">Loading library…</div>';
  const d=await j('/api/v1/game').catch(()=>({items:[],error:'unreachable'}));

  if(d.loading){
    // The first fetch has not landed yet. Say so, rather than showing an
    // empty grid that reads as "your library is empty".
    p.innerHTML=`<div class="card"><h3>Reading the library</h3>
      <p class="help">${esc(d.message||'Fetching from RomM…')}
      ${d.error?` Last attempt: <b>${esc(d.error)}</b>. Retrying with backoff.`:''}</p></div>`;
    return;
  }

  const q=($('#search').value||'').toLowerCase();
  const items=(d.items||[]).filter(g=>!q||g.name.toLowerCase().includes(q));
  const stale=d.error
    ? `<p class="help" style="color:var(--warn)">RomM last refused this list
        (${esc(d.error)}); showing the last good copy.</p>` : '';

  if(!items.length){
    p.innerHTML=stale+`<div class="empty">
      ${q?'No game matches that.':'No games returned by RomM.'}</div>`;
    return;
  }
  p.innerHTML=stale+`<div class="grid">${items.map(g=>`<div class="tile">
    <div class="art" style="background-image:url('${esc(g.cover||'')}')"></div>
    <div class="nm">${esc(g.name)}</div>
    <div class="pf">${esc(g.platform||'')}</div></div>`).join('')}</div>`;
};

RENDER.add=async()=>{
  $('#page').innerHTML=`<div class="card">
    <h3>Request a game</h3>
    <p class="help">Romarr searches your indexers, picks the healthiest release
      for the platform, hands it to the download client and files the ROM into RomM.</p>
    <div class="row">
      <div class="field" style="flex:1;margin:0"><label>Game</label>
        <input type="text" id="g-name" placeholder="Super Mario World"></div>
      <div class="field" style="width:230px;margin:0"><label>Platform</label>
        <select id="g-plat">${PLATFORMS.map(p=>
          `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('')}</select></div>
      <button class="btn" id="g-go" style="margin-top:20px">Search &amp; Grab</button>
    </div>
    <div id="g-out" style="margin-top:16px"></div></div>`;
  $('#g-go').onclick=async()=>{
    const game=$('#g-name').value.trim(), platform=$('#g-plat').value;
    if(!game){toast('Enter a game name');return;}
    $('#g-out').innerHTML='<div class="empty">Searching indexers…</div>';
    const r=await j('/api/request',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({game,platform})});
    $('#g-out').innerHTML=r.ok
      ? `<div class="card" style="margin:0"><h3>Grabbed</h3>
         <p class="help">${esc(r.release)} — ${r.seeders} seeders</p></div>`
      : `<div class="card" style="margin:0"><h3>Not grabbed</h3>
         <p class="help">${esc(r.error||'no usable release')}</p></div>`;
    refreshCounts();
  };
};

RENDER.missing=async()=>{
  const d=await j('/api/v1/wanted/missing');
  $('#page').innerHTML=`<div class="row" style="margin-bottom:14px">
      <button class="btn" id="w-all">Search All</button>
      <span style="color:var(--dim);font-size:12.5px">
        ${d.items.length} game(s) requested but not yet imported</span></div>
    ${d.items.length?`<table><thead><tr><th>Game</th><th>Platform</th>
      <th>Added</th><th>Attempts</th><th>Last error</th></tr></thead><tbody>
      ${d.items.map(w=>`<tr><td>${esc(w.game)}</td><td>${esc(w.platform)}</td>
        <td>${esc(w.added.slice(0,10))}</td><td>${w.attempts}</td>
        <td style="color:var(--dim)">${esc(w.last_error||'—')}</td></tr>`).join('')}
      </tbody></table>`:'<div class="empty">Nothing missing.</div>'}`;
  const b=$('#w-all'); if(b) b.onclick=async()=>{
    b.disabled=true; b.textContent='Searching…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:'MissingGameSearch'})});
    toast(`Searched ${r.searched??0}, grabbed ${r.grabbed??0}`);
    go('missing'); refreshCounts();
  };
};

RENDER.queue=async()=>{
  const d=await j('/api/v1/queue');
  $('#page').innerHTML=d.items.length?`<table><thead><tr><th>Game</th>
    <th>Platform</th><th>Release</th><th>Seeders</th><th>State</th></tr></thead><tbody>
    ${d.items.map(i=>`<tr><td>${esc(i.game)}</td><td>${esc(i.platform)}</td>
      <td>${esc(i.release||'—')}</td><td>${i.seeders||'—'}</td>
      <td><span class="pill ${esc(i.state)}">${esc(i.state)}</span>
      ${i.detail?`<div style="color:var(--dim);font-size:11.5px">${esc(i.detail)}</div>`:''}
      </td></tr>`).join('')}</tbody></table>`
    :'<div class="empty">Queue is empty.</div>';
};

RENDER.history=async()=>{
  const d=await j('/api/v1/history?limit=100');
  $('#page').innerHTML=d.items.length?`<table><thead><tr><th>When</th><th>Event</th>
    <th>Game</th><th>Platform</th><th>Release</th><th>Indexer</th></tr></thead><tbody>
    ${d.items.map(e=>`<tr><td style="color:var(--dim)">${esc(e.at.replace('T',' ').slice(0,16))}</td>
      <td><span class="pill ${esc(e.kind)}">${esc(e.kind)}</span></td>
      <td>${esc(e.game)}</td><td>${esc(e.platform)}</td>
      <td style="color:var(--dim)">${esc(e.release||'—')}</td>
      <td style="color:var(--dim)">${esc(e.indexer||'—')}</td></tr>`).join('')}</tbody></table>`
    :'<div class="empty">No history yet.</div>';
};


/* ---------------- schema-driven editor ---------------- */
/* The form is built from the server's field list, so a new client or indexer
   type needs no change here -- the same thing the *arrs do with their schema
   endpoints. */
let SCHEMA = { downloadclient: {}, indexer: {}, library: {} };

async function loadSchema(kind){
  if(Object.keys(SCHEMA[kind]).length) return SCHEMA[kind];
  const d = await j(`/api/v1/${kind}/schema`);
  SCHEMA[kind] = d.types || {};
  return SCHEMA[kind];
}

function fieldHtml(f, value){
  const v = value === undefined || value === null ? f.default : value;
  const help = f.help ? `<div style="color:var(--dim);font-size:11.5px;margin-top:4px">${esc(f.help)}</div>` : '';
  if(f.type === 'bool')
    return `<label class="check"><input type="checkbox" data-f="${f.name}"
      ${v ? 'checked' : ''}><span>${esc(f.label)}</span></label>${help}`;
  // A list is edited as comma-separated text, which is what the settings file
  // holds anyway, and readForm splits it back apart.
  if(f.type === 'list')
    return `<div class="field"><label>${esc(f.label)}</label>
      <input type="text" data-f="${f.name}" data-list="1"
        value="${esc(Array.isArray(v) ? v.join(', ') : (v ?? ''))}">${help}</div>`;
  const t = f.type === 'int' ? 'number' : (f.type === 'secret' ? 'password' : 'text');
  return `<div class="field"><label>${esc(f.label)}</label>
    <input type="${t}" data-f="${f.name}" value="${esc(v ?? '')}"
      ${f.type === 'secret' ? 'autocomplete="new-password"' : ''}>${help}</div>`;
}

function readForm(){
  const out = {};
  document.querySelectorAll('[data-f]').forEach(el => {
    out[el.dataset.f] = el.type === 'checkbox' ? el.checked
      : el.type === 'number' ? (el.value === '' ? null : Number(el.value))
      : el.dataset.list ? el.value.split(',').map(x => x.trim()).filter(Boolean)
      : el.value;
  });
  return out;
}

function closeModal(){ document.querySelector('.modal')?.remove(); }

/* What each editable kind is called, and which page lists it. Stated once so a
   fourth kind does not mean hunting for three ternaries. */
const KINDS = {
  downloadclient: { label: 'Download Client', page: 'clients' },
  indexer:        { label: 'Indexer',         page: 'indexers' },
  library:        { label: 'Library',         page: 'libraries' },
};

/** Choose a type, then edit it. */
async function addItem(kind){
  const types = await loadSchema(kind);
  const m = document.createElement('div');
  m.className = 'modal';
  m.innerHTML = `<div class="box"><h3>Add ${esc((KINDS[kind]||{}).label || kind)}</h3>
    <div class="sub">Pick a type.</div>
    <div class="pick">${Object.entries(types).map(([k, t]) =>
      `<button data-t="${k}">${esc(t.label)}<small>${esc(t.protocol || k)}</small></button>`).join('')}</div>
    <div class="foot"><button class="btn ghost sp" data-close>Cancel</button></div></div>`;
  document.body.append(m);
  m.onclick = e => { if(e.target === m || e.target.dataset.close !== undefined) closeModal(); };
  m.querySelectorAll('[data-t]').forEach(b => b.onclick = () => {
    closeModal();
    editItem(kind, { type: b.dataset.t });
  });
}

async function editItem(kind, item){
  const types = await loadSchema(kind);
  const spec = types[item.type];
  if(!spec){ toast('Unknown type: ' + item.type); return; }
  const isNew = !item.id;
  // A new entry gets the type's sensible defaults rather than an empty form.
  if(isNew){
    if(spec.default_port && item.port === undefined) item.port = spec.default_port;
    if(item.name === undefined) item.name = spec.label;
  }

  const m = document.createElement('div');
  m.className = 'modal';
  m.innerHTML = `<div class="box">
    <h3>${isNew ? 'Add' : 'Edit'} ${esc(spec.label)}</h3>
    <div class="sub">${esc(spec.protocol || (KINDS[kind]||{}).label || '')}${spec.managed ? ' · manages its own indexers' : ''}</div>
    <div id="fields">${spec.fields.map(f => fieldHtml(f, item[f.name])).join('')}</div>
    <div id="testline"></div>
    <div class="foot">
      <button class="btn ghost" id="m-test">Test</button>
      ${isNew ? '' : '<button class="btn danger" id="m-del">Delete</button>'}
      <button class="btn ghost sp" data-close>Cancel</button>
      <button class="btn" id="m-save">Save</button>
    </div></div>`;
  document.body.append(m);
  m.onclick = e => { if(e.target === m || e.target.dataset.close !== undefined) closeModal(); };

  const payload = () => ({ ...readForm(), type: item.type, id: item.id });
  const line = (ok, msg) => {
    m.querySelector('#testline').className = 'testline ' + (ok ? 'ok' : 'bad');
    m.querySelector('#testline').textContent = msg;
  };

  m.querySelector('#m-test').onclick = async (e) => {
    e.target.disabled = true; line(true, 'Testing…');
    const r = await j(`/api/v1/${kind}/test`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload()) });
    line(r.ok, r.message || (r.ok ? 'Connected' : 'Failed'));
    e.target.disabled = false;
  };

  m.querySelector('#m-save').onclick = async (e) => {
    e.target.disabled = true;
    await j(`/api/v1/${kind}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload()) });
    closeModal(); toast('Saved'); go((KINDS[kind] || {}).page || 'clients');
  };

  const del = m.querySelector('#m-del');
  if(del) del.onclick = async () => {
    if(!confirm(`Remove ${item.name || spec.label}?`)) return;
    await fetch(`/api/v1/${kind}/${item.id}`, { method: 'DELETE' });
    closeModal(); toast('Removed'); go((KINDS[kind] || {}).page || 'clients');
  };
}

/* ---------------- settings ---------------- */
function settingsPage(title, help, body){
  $('#page').innerHTML=`<div class="card"><h3>${title}</h3>
    <p class="help">${help}</p>${body}
    <button class="btn" id="s-save">Save</button></div>`;
  $('#s-save').onclick=async()=>{
    const patch={};
    document.querySelectorAll('[data-k]').forEach(el=>{
      patch[el.dataset.k]= el.type==='checkbox' ? el.checked
        : el.type==='number' ? Number(el.value)
        : el.dataset.list ? el.value.split(',').map(s=>s.trim()).filter(Boolean)
        : el.value;
    });
    SETTINGS=await j('/api/v1/config',{method:'PUT',
      headers:{'content-type':'application/json'},body:JSON.stringify(patch)});
    toast('Saved');
  };
}
const fld=(k,label,val,type='text',extra='')=>`<div class="field"><label>${label}</label>
  <input type="${type}" data-k="${k}" ${extra} value="${esc(val)}"></div>`;
const chk=(k,label,val)=>`<label class="check"><input type="checkbox" data-k="${k}"
  ${val?'checked':''}><span>${label}</span></label>`;

RENDER.media=()=>settingsPage('Media Management',
  'Where imported ROMs are filed. This must be the same path RomM scans.',
  fld('library_path','ROM library root',SETTINGS.library_path)
  +chk('rename_on_import','Rename on import',SETTINGS.rename_on_import)
  +chk('overwrite_existing','Overwrite an existing file',SETTINGS.overwrite_existing)
  +chk('rescan_after_import','Tell RomM to rescan after an import',SETTINGS.rescan_after_import));

RENDER.profiles=()=>settingsPage('Release Profile',
  'A quality profile means nothing for a cartridge dump — there is no bitrate. '
  +'What separates two dumps of one game is region, revision and whether it is a '
  +'hack or a beta, so that is what is ranked here. Earlier regions win.',
  `<div class="field"><label>Preferred regions, best first</label>
     <input type="text" data-k="preferred_regions" data-list="1"
       value="${esc((SETTINGS.preferred_regions||[]).join(', '))}"></div>`
  +chk('allow_beta','Accept betas and prototypes',SETTINGS.allow_beta)
  +chk('allow_rom_hacks','Accept ROM hacks',SETTINGS.allow_rom_hacks)
  +fld('min_seeders','Minimum seeders',SETTINGS.min_seeders,'number')
  +fld('max_size_mb','Maximum size (MB)',SETTINGS.max_size_mb,'number'));

RENDER.clients=async()=>{
  const d=await j('/api/v1/downloadclient');
  const state=c=>!c.configured?'<span class="dot"></span>not configured'
    :c.ok?'<span class="dot up"></span>connected'
    :'<span class="dot down"></span>unreachable';
  const gaps=['torrent','usenet'].filter(p=>
    !d.items.some(c=>c.protocol===p&&c.configured));
  $('#page').innerHTML=`<div class="card">
    <div class="row" style="margin-bottom:12px">
      <h3 style="margin:0">Download Clients</h3>
      <button class="btn sp" id="c-add" style="margin-left:auto">Add</button>
    </div>
    <p class="help">A release is routed by its protocol, so an indexer type
      with no client configured cannot be grabbed at all &mdash; which is why
      unconfigured clients are listed rather than hidden.</p>
    ${gaps.length?`<p class="help" style="color:var(--warn)">
      No client configured for: <b>${gaps.join(', ')}</b>.
      Those results will be found and then refused.</p>`:''}
    ${d.items.length?`<table><thead><tr><th>Client</th><th>Protocol</th><th>Address</th>
      <th>Category</th><th>Status</th><th></th></tr></thead><tbody>
      ${d.items.map((c,i)=>`<tr><td>${esc(c.name)}</td>
        <td><span class="pill">${esc(c.protocol)}</span></td>
        <td style="color:var(--dim)">${esc(c.url||'—')}</td>
        <td>${esc(c.category||'—')}</td>
        <td>${state(c)}</td>
        <td><div class="rowact"><button data-edit="${i}">Edit</button></div></td>
        </tr>`).join('')}</tbody></table>`
    :'<div class="empty">No download clients yet. Add one to start grabbing.</div>'}
  </div>`;
  $('#c-add').onclick=()=>addItem('downloadclient');
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=async()=>{
    const row=d.items[Number(b.dataset.edit)];
    // The table is a status view; the editor needs the stored configuration.
    const all=await j('/api/v1/config');
    const cfg=(all.download_clients||[]).find(x=>x.id===row.id);
    if(cfg) editItem('downloadclient', cfg);
    else toast('That client has no stored configuration to edit');
  });
};

RENDER.libraries=async()=>{
  const d=await j('/api/v1/library');
  const state=l=>!l.ok?'<span class="dot down"></span>unreachable'
    :!l.path_exists?'<span class="dot warn"></span>path missing'
    :'<span class="dot up"></span>connected';
  const rules=l=>(l.platforms&&l.platforms.length)
    ? l.platforms.map(p=>`<span class="pill">${esc(p)}</span>`).join(' ')
    : (l.is_default?'<span style="color:var(--dim)">any platform</span>'
                   :'<span style="color:var(--warn)">nothing routes here</span>');
  const noDefault=d.items.length>1&&!d.items.some(l=>l.is_default);
  const unmounted=d.items.filter(l=>l.ok&&!l.path_exists);
  $('#page').innerHTML=`<div class="card">
    <div class="row" style="margin-bottom:12px">
      <h3 style="margin:0">Libraries</h3>
      <button class="btn sp" id="l-add" style="margin-left:auto">Add</button>
    </div>
    <p class="help">Where finished ROMs are filed. Add more than one to send
      some platforms elsewhere &mdash; a platform rule wins over the default,
      so &ldquo;N64 goes to Retrom&rdquo; is one row here rather than a second
      Romarr. Each server needs its own path, as <b>Romarr</b> sees it.</p>
    ${noDefault?`<p class="help" style="color:var(--warn)">
      No library is marked default, so anything without a matching platform rule
      goes to the first one listed. Mark one to make that a decision.</p>`:''}
    ${unmounted.map(l=>`<p class="help" style="color:var(--warn)">
      <b>${esc(l.name)}</b> answers, but nothing can be imported into it yet.
      ${esc(l.path_hint||'')}</p>`).join('')}
    ${d.items.length?`<table><thead><tr><th>Library</th><th>Type</th>
      <th>Address</th><th>Path</th><th>Platforms</th><th>Status</th><th></th></tr>
      </thead><tbody>
      ${d.items.map((l,i)=>`<tr><td>${esc(l.name)}
        ${l.is_default?'<span class="pill">default</span>':''}</td>
        <td><span class="pill">${esc(l.type)}</span></td>
        <td style="color:var(--dim)">${esc(l.url||'—')}</td>
        <td style="color:var(--dim)">${esc(l.path||'—')}</td>
        <td>${rules(l)}</td>
        <td>${state(l)}</td>
        <td><div class="rowact"><button data-ledit="${i}">Edit</button></div></td>
        </tr>`).join('')}</tbody></table>`
    :'<div class="empty">No library yet. Add one, or nothing can be imported.</div>'}
  </div>`;
  $('#l-add').onclick=()=>addItem('library');
  document.querySelectorAll('[data-ledit]').forEach(b=>b.onclick=async()=>{
    const row=d.items[Number(b.dataset.ledit)];
    // The table is a status view; the editor needs the stored configuration.
    const all=await j('/api/v1/library/config');
    const cfg=(all.items||[]).find(x=>x.id===row.id);
    if(cfg) editItem('library', cfg);
    else toast('That library has no stored configuration to edit');
  });
};

RENDER.indexers=async()=>{
  const d=await j('/api/v1/indexer');
  $('#page').innerHTML=`<div class="card">
    <div class="row" style="margin-bottom:12px">
      <h3 style="margin:0">Indexers</h3>
      <button class="btn" id="i-add" style="margin-left:auto">Add</button>
    </div>
    <p class="help">Add a Newznab or Torznab indexer directly, or point at
      Prowlarr and use everything it already has configured.</p>
    ${d.items.length?`<table><thead><tr><th>Name</th><th>Type</th><th>URL</th>
      <th>Enabled</th><th></th></tr></thead><tbody>
      ${d.items.map((it,i)=>`<tr><td>${esc(it.name||'—')}</td>
        <td><span class="pill">${esc(it.type)}</span></td>
        <td style="color:var(--dim)">${esc(it.url||'—')}</td>
        <td><span class="dot ${it.enable?'up':'down'}"></span>${it.enable?'yes':'no'}</td>
        <td><div class="rowact"><button data-iedit="${i}">Edit</button></div></td>
        </tr>`).join('')}</tbody></table>`
    :'<div class="empty">No indexers configured.</div>'}
  </div>
  ${(d.proxied||[]).length?`<div class="card"><h3>Via Prowlarr</h3>
    <p class="help">Managed in Prowlarr, shown here read-only. Add or remove
      them there and they appear or disappear from this list.</p>
    <table><thead><tr><th>Indexer</th><th>Protocol</th><th>Categories</th>
      <th>Enabled</th></tr></thead><tbody>
      ${d.proxied.map(i=>`<tr><td>${esc(i.name)}</td><td>${esc(i.protocol)}</td>
        <td style="color:var(--dim)">${esc((i.categories||[]).join(', ')||'—')}</td>
        <td><span class="dot ${i.enable?'up':'down'}"></span>${i.enable?'yes':'no'}</td>
        </tr>`).join('')}</tbody></table></div>`
    :(d.error?`<div class="card"><h3>Via Prowlarr</h3>
        <p class="help" style="color:var(--warn)">${esc(d.error)}</p></div>`:'')}`;
  $('#i-add').onclick=()=>addItem('indexer');
  document.querySelectorAll('[data-iedit]').forEach(b=>b.onclick=()=>
    editItem('indexer', d.items[Number(b.dataset.iedit)]));
};

RENDER.general=()=>settingsPage('General',
  'Connections to the rest of the stack. Credentials live in the environment '
  +'file, not here — this page never shows or stores a secret.',
  `<div class="field"><label>Prowlarr</label>
     <input type="text" value="${esc(SETTINGS._prowlarr_url||'')}" disabled></div>
   <div class="field"><label>qBittorrent</label>
     <input type="text" value="${esc(SETTINGS._qbit_url||'')}" disabled></div>
   <div class="field"><label>RomM</label>
     <input type="text" value="${esc(SETTINGS._romm_url||'')}" disabled></div>`
  +chk('auto_import','Import completed downloads automatically',SETTINGS.auto_import)
  +`<div class="field"><label>Protocol</label>
     <select data-k="protocol">
       <option value="torrent"${SETTINGS.protocol==='torrent'?' selected':''}>Torrent</option>
       <option value="usenet"${SETTINGS.protocol==='usenet'?' selected':''}>Usenet</option>
     </select></div>`);

/* ---------------- system ---------------- */
RENDER.status=async()=>{
  const h=await j('/api/v1/system/status');
  const row=(l,ok,extra='')=>`<tr><td>${l}</td>
    <td><span class="dot ${ok?'up':'down'}"></span>${ok?'OK':'Not available'}</td>
    <td style="color:var(--dim)">${esc(extra)}</td></tr>`;
  const g=h.ggrequestz||{};
  $('#page').innerHTML=`<div class="card"><h3>Health</h3><table><tbody>
      ${row('Prowlarr',h.prowlarr,h.prowlarr_url)}
      ${(h.clients||[]).map(c=>row(
        `${c.name} <span class="pill">${esc(c.protocol)}</span>`,
        c.ok, c.configured?c.url:'not configured')).join('')}
      ${(h.libraries||[]).length
        ? (h.libraries||[]).map(l=>row(
            `${esc(l.name)} <span class="pill">${esc(l.type)}</span>${
              l.is_default?' <span class="pill">default</span>':''}`,
            l.ok && l.path_exists,
            !l.ok ? (l.url||'unreachable')
                  : (l.path_exists ? l.path : (l.path_hint||l.path)))).join('')
        : row('RomM',h.romm,h.romm_url)
          +row('ROM library',h.library,h.library?h.library_path:(h.library_path_hint||h.library_path))}
      ${g.configured?row('GG Requestz',g.ok,g.url)
        :`<tr><td>GG Requestz</td><td><span class="dot"></span>not configured</td>
          <td style="color:var(--dim)">set GGREQUESTZ_URL to show the link</td></tr>`}
    </tbody></table></div>
    <div class="card"><h3>About</h3><div class="st">
      <div><b>${esc(h.version)}</b><span>Version</span></div>
      <div><b>${h.platforms}</b><span>Platforms</span></div>
      <div><b>${h.events}</b><span>History events</span></div>
      <div><b>${esc(h.uptime)}</b><span>Uptime</span></div>
    </div></div>`;
};

RENDER.tasks=async()=>{
  const tasks=[
    ['MissingGameSearch','Search for everything in Wanted'],
    ['ImportCompleted','Import finished downloads and rescan RomM'],
    ['RefreshLibrary','Re-read the library from RomM'],
  ];
  $('#page').innerHTML=`<div class="card"><h3>Tasks</h3>
    <p class="help">Run on demand. These are the same jobs the service runs itself.</p>
    <table><tbody>${tasks.map(([n,d])=>`<tr><td><b>${n}</b>
      <div style="color:var(--dim);font-size:12px">${d}</div></td>
      <td style="text-align:right"><button class="btn ghost" data-task="${n}">Run</button></td>
      </tr>`).join('')}</tbody></table></div>`;
  document.querySelectorAll('[data-task]').forEach(b=>b.onclick=async()=>{
    b.disabled=true; b.textContent='Running…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:b.dataset.task})});
    toast(r.message||'Done'); b.disabled=false; b.textContent='Run'; refreshCounts();
  });
};

RENDER.logs=async()=>{
  const d=await j('/api/v1/log?limit=200');
  $('#page').innerHTML=`<div class="card"><h3>Recent events</h3>
    <p class="help">Romarr's own history. Service logs are in journalctl -u romarr,
    or docker logs romarr.</p>
    ${d.items.length?`<pre style="font:12px/1.6 ui-monospace,Menlo,monospace;
      color:var(--dim);white-space:pre-wrap">${d.items.map(e=>
      `${esc(e.at)}  ${esc(e.kind.toUpperCase().padEnd(9))} ${esc(e.game)} `+
      `[${esc(e.platform)}] ${esc(e.detail||e.release||'')}`).join('\n')}</pre>`
    :'<div class="empty">Nothing logged yet.</div>'}</div>`;
};

/* ---------------- boot ---------------- */
async function refreshCounts(){
  const s=await j('/api/v1/system/counts').catch(()=>({}));
  document.querySelectorAll('[data-ct]').forEach(b=>{
    const v=s[b.dataset.ct];
    // null means "not counted yet" -- a dash is honest where 0 would claim
    // the library is empty.
    b.textContent = v == null ? (b.dataset.ct === 'games' ? '—' : '') : (v || '');
    b.style.display = (v == null && b.dataset.ct !== 'games') ? 'none' : '';
  });
}
(async()=>{
  document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>go(n.dataset.page));
  $('#search').oninput=()=>{const p=location.hash.slice(1)||'library';
    if(p==='library') RENDER.library();};
  [PLATFORMS,SETTINGS]=await Promise.all([
    j('/api/platforms').catch(()=>[]), j('/api/v1/config').catch(()=>({}))]);
  go(location.hash.slice(1)||'library');
  refreshCounts(); setInterval(refreshCounts,15000);
})();
"""


def page() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Romarr</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body>
<nav id="rail"><div id="brand">Rom<span>arr</span></div>{_nav_html()}</nav>
<div id="main">
  <div id="top"><h1>Games</h1><input id="search" placeholder="Filter…" autocomplete="off"></div>
  <div class="page" id="page"></div>
</div>
<script>{JS}</script></body></html>"""
