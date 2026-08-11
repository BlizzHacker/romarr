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
/* No gap: the brand is one word split into two nodes so that "arr" can
   carry the accent colour, and a flex gap renders it as "ROM arr". */
#brand{display:flex;align-items:center;gap:0;padding:16px 18px;
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
/* ---- plugin catalogue ---------------------------------------------------
   A card grid rather than a table. A plugin is a thing you decide to trust,
   and that decision needs the description and the permissions in view at the
   same time -- a table row makes you read across five columns to assemble
   what one card says at a glance. */
.cat-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding:0 0 14px;border-bottom:1px solid var(--line);margin-bottom:16px}
.cat-bar input{flex:1;min-width:220px;padding:9px 13px;background:var(--bg);
  color:var(--fg);border:1px solid var(--line);border-radius:7px;outline:none;
  transition:border-color .12s,box-shadow .12s}
.cat-bar input:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(242,163,60,.13)}
.chip{padding:5px 11px;border-radius:999px;border:1px solid var(--line);
  background:var(--bg);color:var(--dim);cursor:pointer;font-size:12px;
  white-space:nowrap;transition:all .12s}
.chip:hover{border-color:#555;color:var(--fg)}
.chip.on{background:var(--accent);border-color:var(--accent);
  color:var(--accent-ink);font-weight:600}
.chip .n{opacity:.65;margin-left:5px}

/* The scroll the catalogue needs once it is more than a screenful. Bounded
   so the page header and the add-your-own panel stay put while it moves. */
.cat-scroll{max-height:calc(100vh - 340px);min-height:220px;overflow-y:auto;
  padding-right:6px;margin-right:-6px}
.cat-scroll::-webkit-scrollbar{width:10px}
.cat-scroll::-webkit-scrollbar-track{background:transparent}
.cat-scroll::-webkit-scrollbar-thumb{background:#3d3d3d;border-radius:5px;
  border:2px solid var(--panel)}
.cat-scroll::-webkit-scrollbar-thumb:hover{background:#4d4d4d}

.pgrid{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.pcard{background:var(--bg);border:1px solid var(--line);border-radius:9px;
  padding:14px 15px;display:flex;flex-direction:column;gap:9px;
  transition:border-color .12s,transform .12s}
.pcard:hover{border-color:#4a4a4a;transform:translateY(-1px)}
.pcard.installed{border-left:3px solid var(--ok)}
.pcard h4{font-size:14px;font-weight:600;display:flex;align-items:center;
  gap:8px;margin:0}
.pcard .by{color:var(--dim);font-size:11px;font-weight:400}
.pcard .desc{color:#b4b4b4;font-size:12.5px;line-height:1.45;flex:1}
.pcard .meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pcard .foot{display:flex;gap:8px;align-items:center;
  border-top:1px solid var(--line);padding-top:9px;margin-top:2px}
.pcard .net{color:#7d7d7d;font-size:11px;margin-left:auto;text-align:right}
.dot-ok{width:7px;height:7px;border-radius:50%;background:var(--ok);
  display:inline-block;flex:none}
.dot-off{width:7px;height:7px;border-radius:50%;background:#555;
  display:inline-block;flex:none}

.panels{display:grid;gap:14px;grid-template-columns:1fr 1fr;margin-top:16px}
@media(max-width:900px){.panels{grid-template-columns:1fr}}
.panel-note{background:rgba(91,155,213,.09);border:1px solid rgba(91,155,213,.3);
  border-radius:7px;padding:10px 12px;color:#a8c8e8;font-size:12px;
  line-height:1.5;margin-top:10px}
.panel-warn{background:rgba(240,80,80,.09);border-color:rgba(240,80,80,.3);
  color:#e8a8a8}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
.field label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--dim)}
.field input,.field textarea{padding:8px 11px;background:var(--bg);
  color:var(--fg);border:1px solid var(--line);border-radius:6px;outline:none;
  font:13px/1.5 inherit;transition:border-color .12s}
.field input:focus,.field textarea:focus{border-color:var(--accent)}
.field textarea{resize:vertical;min-height:58px}
.empty-cat{text-align:center;padding:48px 20px;color:var(--dim)}
.empty-cat b{display:block;color:var(--fg);margin-bottom:6px;font-size:15px}
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
.mini{padding:3px 11px;font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:6px;cursor:pointer;margin-left:6px}.mini.accent{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.okt{color:var(--ok);font-size:12px}
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
    ("Library",  [("library", "Games", "games"), ("add", "Add New", None),
                  ("discover", "Discover", None),
                  ("search", "Interactive Search", None)]),
    ("Wanted",   [("missing", "Missing", "missing"), ("lists", "Lists", None),
                  ("calendar", "Calendar", None)]),
    ("Activity", [("queue", "Queue", "queued"), ("history", "History", None),
                  ("blocklist", "Blocklist", None)]),
    ("Hub",      [("hub", "Plugins", None)]),
    ("Settings", [("media", "Media Management", None), ("profiles", "Profiles", None),
                  ("indexers", "Indexers", None), ("clients", "Download Clients", None),
                  ("libraries", "Libraries", None),
                  ("connections", "Connections", None),
                  ("metadata", "Metadata", None), ("general", "General", None)]),
    ("System",   [("status", "Status", None), ("stats", "Stats", None),
                  ("platforms", "Platforms", None),
                  ("getstarted", "Get Started", None),
                  ("collections", "Collections", None),
                  ("manualimport", "Manual Import", None),
                  ("tasks", "Tasks", None), ("logs", "Logs", None)]),
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
  const titles={library:'Games',add:'Add New Game',search:'Interactive Search',
    missing:'Wanted — Missing',lists:'Import Lists',stats:'Statistics',
    discover:'Discover',
    queue:'Queue',history:'History',media:'Media Management',profiles:'Profiles',
    indexers:'Indexers',clients:'Download Clients',libraries:'Libraries',
    general:'General',
    status:'System Status',platforms:'Platforms',tasks:'Tasks',logs:'Logs',
    blocklist:'Blocklist',connections:'Connections',metadata:'Metadata',
    calendar:'Release Calendar',manualimport:'Manual Import',
    collections:'Collections \u2014 full sets and 1G1R',
    getstarted:'Get Started',
    hub:'ROM Hub — Plugins'};
  $('#top h1').textContent=titles[page]||'ROMarr';
  $('#search').classList.toggle('hide', !['library','add'].includes(page));
  (RENDER[page]||RENDER.library)();
}
addEventListener('hashchange',()=>go(location.hash.slice(1)||'library'));

/* ---------------- pages ---------------- */
const RENDER={};
RENDER.hub=async()=>{
  // Installing a plugin means running its author's code. Whether that code is
  // confined is the thing to know first, so it goes at the top of the page
  // rather than in a log the operator will never read.
  const hs=await j('/api/v1/hub/status').catch(()=>({}));
  const confined=hs.sandboxed===true;
  const sandboxNote=hs.available===false ? '' :
    (confined
      ? '<div class="panel-note" style="border-left:3px solid var(--ok)">'
        +'<b>Plugins run confined.</b> Each runs as its own subprocess with no '
        +'library token and no sockets of its own &mdash; it can only reach the '
        +'hosts it declared. It can still read files ROMarr can.</div>'
      : '<div class="panel-note panel-warn"><b>Plugins run with no '
        +'confinement.</b> '+esc(hs.sandbox_detail||'')+' A plugin you install '
        +'can reach any host and read any file ROMarr can.</div>');

  const p=$('#page');
  p.innerHTML='<div class="empty">Reading the plugin catalogue…</div>';

  // State lives here rather than in the DOM: a filter that has to be read
  // back out of the markup drifts from what is actually displayed.
  let q='', cap='', inst='';

  const load=async()=>{
    const qs=new URLSearchParams();
    if(q) qs.set('q',q);
    if(cap) qs.set('capability',cap);
    if(inst) qs.set('installed',inst);
    return j('/api/v1/hub/catalogue?'+qs.toString())
      .catch(()=>({items:[],facets:{capabilities:[],platforms:[]},error:'ROM Hub unreachable'}));
  };

  const card=pl=>{
    const caps=(pl.capabilities||[]).map(c=>'<span class="pill">'+esc(c)+'</span>').join(' ');
    const net=(pl.network||[]).slice(0,2).join(', ');
    const action = pl.installed
      ? '<span class="'+(pl.enabled?'dot-ok':'dot-off')+'"></span>'
        +'<span class="help" style="margin:0">'+(pl.enabled?'Enabled':'Disabled')+'</span>'
        +'<button class="mini" data-act="'+(pl.enabled?'disable':'enable')+'" '
        +'data-slug="'+esc(pl.slug)+'">'+(pl.enabled?'Disable':'Enable')+'</button>'
      : '<button class="mini accent" data-act="install" data-slug="'+esc(pl.slug)+'">Install'
        +(pl.key_required?' · key needed':'')+'</button>';
    return '<div class="pcard'+(pl.installed?' installed':'')+'">'
      +'<h4>'+esc(pl.name)+(pl.author?'<span class="by">by '+esc(pl.author)+'</span>':'')+'</h4>'
      +'<div class="desc">'+esc(pl.description||'No description supplied.')+'</div>'
      +'<div class="meta">'+(caps||'<span class="help" style="margin:0">no capabilities declared</span>')+'</div>'
      +'<div class="foot">'+action
      +(net?'<span class="net">reaches '+esc(net)+'</span>':'')+'</div></div>';
  };

  const render=d=>{
    const items=d.items||[];
    const facets=(d.facets&&d.facets.capabilities)||[];
    const chips=facets.map(([name,n])=>
      '<span class="chip'+(cap===name?' on':'')+'" data-cap="'+esc(name)+'">'
      +esc(name)+'<span class="n">'+n+'</span></span>').join('');

    p.innerHTML=sandboxNote+'<div class="card"><h3>Plugins'
      +'<span class="help" style="margin-left:10px">'+items.length+' of '+(d.total||0)+'</span></h3>'
      +'<div class="cat-bar">'
      +'<input id="pq" placeholder="Search plugins by name, author or description…" value="'+esc(q)+'">'
      +'<span class="chip'+(inst==='1'?' on':'')+'" data-inst="1">Installed</span>'
      +'<span class="chip'+(inst==='0'?' on':'')+'" data-inst="0">Available</span>'
      +chips+'</div>'
      +(d.error?'<div class="panel-note panel-warn">'+esc(d.error)+'</div>':'')
      +'<div class="cat-scroll">'
      +(items.length?'<div class="pgrid">'+items.map(card).join('')+'</div>'
        :'<div class="empty-cat"><b>Nothing matches</b>'
         +'Try a different search, or add your own plugin below.</div>')
      +'</div></div>'

      +'<div class="panels">'
      +'<div class="card"><h3>Add your own</h3>'
      +'<p class="help">Install a plugin straight from its repository — yours, '
      +'or somebody else\'s that is not in the catalogue yet.</p>'
      +'<div class="field"><label>Repository URL</label>'
      +'<input id="ownurl" placeholder="https://github.com/you/rom-hub-your-plugin"></div>'
      +'<button class="mini accent" id="ownadd">Check and install</button>'
      +'<div id="ownmsg"></div>'
      +'<div class="panel-note">A plugin is code ROMarr runs. Only https, and only '
      +'from a forge where you can read the source first — the check tells you which '
      +'hosts are allowed.</div></div>'

      +'<div class="card"><h3>Submit to be featured</h3>'
      +'<p class="help">Propose your plugin for the shared catalogue so everyone '
      +'can find it.</p>'
      +'<div class="field"><label>Slug</label><input id="sslug" placeholder="your-plugin"></div>'
      +'<div class="field"><label>Name</label><input id="sname" placeholder="Your Plugin"></div>'
      +'<div class="field"><label>Repository</label><input id="srepo" placeholder="https://github.com/you/your-plugin"></div>'
      +'<div class="field"><label>Capabilities</label><input id="scaps" placeholder="search, importer"></div>'
      +'<div class="field"><label>Description</label><textarea id="sdesc" '
      +'placeholder="What it does, and what somebody is trusting when they run it."></textarea></div>'
      +'<button class="mini accent" id="ssubmit">Prepare submission</button>'
      +'<div id="smsg"></div>'
      +'<div class="panel-note">ROMarr does not post this for you. It prepares the '
      +'entry and hands you a link to review and submit yourself.</div></div>'
      +'</div>';

    const qi=$('#pq');
    let t=null;
    qi.oninput=()=>{clearTimeout(t);t=setTimeout(async()=>{
      q=qi.value.trim();
      const at=qi.selectionStart;
      render(await load());
      const n=$('#pq'); n.focus(); n.setSelectionRange(at,at);
    },220);};

    p.querySelectorAll('.chip[data-cap]').forEach(c=>c.onclick=async()=>{
      cap = cap===c.dataset.cap ? '' : c.dataset.cap; render(await load());});
    p.querySelectorAll('.chip[data-inst]').forEach(c=>c.onclick=async()=>{
      inst = inst===c.dataset.inst ? '' : c.dataset.inst; render(await load());});

    p.querySelectorAll('button[data-act]').forEach(b=>b.onclick=async()=>{
      b.disabled=true; b.textContent='Working…';
      await fetch('/api/v1/hub/plugin',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({slug:b.dataset.slug,action:b.dataset.act})});
      render(await load());
    });

    $('#ownadd').onclick=async()=>{
      const msg=$('#ownmsg');
      const r=await fetch('/api/v1/hub/source/check',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:$('#ownurl').value.trim()})});
      const d=await r.json();
      msg.innerHTML='<div class="panel-note'+(d.ok?'':' panel-warn')+'">'
        +esc(d.ok?('Source accepted: '+d.host+'. Installing…'):d.reason)+'</div>';
    };

    $('#ssubmit').onclick=async()=>{
      const msg=$('#smsg');
      const r=await fetch('/api/v1/hub/submit',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          slug:$('#sslug').value.trim(), name:$('#sname').value.trim(),
          repository:$('#srepo').value.trim(),
          description:$('#sdesc').value.trim(),
          capabilities:$('#scaps').value.split(',').map(x=>x.trim()).filter(Boolean)})});
      const d=await r.json();
      if(d.problems){
        msg.innerHTML='<div class="panel-note panel-warn"><b>Fix these:</b><br>'
          +d.problems.map(esc).join('<br>')+'</div>';
      }else{
        msg.innerHTML='<div class="panel-note">Ready. '
          +'<a href="'+esc(d.submit_url)+'" target="_blank" rel="noopener">'
          +'Open the submission</a> to review and post it yourself.</div>';
      }
    };
  };

  render(await load());
};

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
  // The shelf: status, rating and notes set by the operator, overlaid on the
  // grid. One fetch for the whole page -- the metadata is tiny.
  const meta=await j('/api/v1/game/meta').catch(()=>({items:[]}));
  const shelf={};
  (meta.items||[]).forEach(x=>{
    shelf[`${x.platform}/${(x.game||'').toLowerCase()}`]=x; });
  const mark=g=>{
    const x=shelf[`${g.platform||''}/${(g.name||'').toLowerCase()}`];
    if(!x) return '';
    const bits=[];
    if(x.status) bits.push(`<span class="pill">${esc(x.status)}</span>`);
    if(x.rating) bits.push(`<span class="pill">★ ${x.rating}</span>`);
    return bits.length?`<div style="margin-top:2px">${bits.join(' ')}</div>`:'';
  };
  p.innerHTML=stale+`<div class="grid">${items.map((g,i)=>`<div class="tile"
      data-shelf="${i}" style="cursor:pointer" title="Click to set status, rating, notes">
    <div class="art" style="background-image:url('${esc(g.cover||'')}')"></div>
    <div class="nm">${esc(g.name)}</div>
    <div class="pf">${esc(g.platform||'')}</div>${mark(g)}</div>`).join('')}</div>`;
  document.querySelectorAll('[data-shelf]').forEach(t=>t.onclick=()=>
    shelfEditor(items[Number(t.dataset.shelf)],
      shelf[`${items[Number(t.dataset.shelf)].platform||''}/`
        +`${(items[Number(t.dataset.shelf)].name||'').toLowerCase()}`]||{}));
};

function shelfEditor(g, meta){
  const m=document.createElement('div');
  m.className='modal';
  const opt=(v,label)=>`<option value="${v}"${(meta.status||'')===v?' selected':''}>${label}</option>`;
  m.innerHTML=`<div class="box">
    <h3>${esc(g.name)}</h3>
    <div class="sub">${esc(g.platform||'')} &mdash; the shelf: what you are
      doing with this game, and what you thought of it.</div>
    <div class="field"><label>Status</label>
      <select data-f="status">${opt('','—')}${opt('playing','Playing')}
        ${opt('completed','Completed')}${opt('shelved','Shelved')}</select></div>
    <div class="field"><label>Rating</label>
      <select data-f="rating"><option value="0">unrated</option>
        ${[1,2,3,4,5,6,7,8,9,10].map(n=>`<option value="${n}"${
          meta.rating===n?' selected':''}>${'★'.repeat(Math.ceil(n/2))} ${n}/10</option>`).join('')}
      </select></div>
    <div class="field"><label>Notes</label>
      <textarea data-f="notes" rows="4"
        style="width:100%;resize:vertical">${esc(meta.notes||'')}</textarea></div>
    <div class="foot">
      <button class="btn ghost sp" data-close>Cancel</button>
      <button class="btn" id="sh-save">Save</button>
    </div></div>`;
  document.body.append(m);
  m.onclick=e=>{ if(e.target===m||e.target.dataset.close!==undefined) closeModal(); };
  m.querySelector('#sh-save').onclick=async()=>{
    const f=readForm();
    const r=await j('/api/v1/game/meta',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({platform:g.platform||'',game:g.name,
        status:f.status,rating:Number(f.rating)||0,notes:f.notes})});
    closeModal();
    toast(r.error?r.error:'Saved'); if(!r.error) go('library');
  };
}

RENDER.add=async()=>{
  $('#page').innerHTML=`<div class="card">
    <h3>Request a game</h3>
    <p class="help">ROMarr searches your indexers, picks the healthiest release
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

RENDER.search=async()=>{
  $('#page').innerHTML=`<div class="card">
    <h3>Interactive search</h3>
    <p class="help">Every release your indexers returned, scored, with the
      reasoning shown &mdash; and a Grab button, so you can overrule the ranking
      when you disagree with it. This is the page to reach for when a request
      took the wrong release: the reason it outranked the one you wanted is
      written next to it.</p>
    <div class="row">
      <div class="field" style="flex:1;margin:0"><label>Game</label>
        <input type="text" id="s-name" placeholder="Phantasy Star IV"></div>
      <div class="field" style="width:230px;margin:0"><label>Platform</label>
        <select id="s-plat"><option value="">Any platform</option>${PLATFORMS.map(p=>
          `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('')}</select></div>
      <button class="btn" id="s-go" style="margin-top:20px">Search</button>
    </div>
    <p class="help" style="margin-top:10px">Without a platform there is no
      platform evidence to score on, so the ranking means less. Pick one when
      you can.</p>
    <div id="s-out" style="margin-top:16px"></div></div>`;

  const mb=n=>n>=1048576?(n/1048576).toFixed(1)+' MB'
    :n>0?(n/1024).toFixed(0)+' KB':'—';

  const run=async()=>{
    const game=$('#s-name').value.trim(), platform=$('#s-plat').value;
    if(!game){toast('Enter a game name');return;}
    $('#s-out').innerHTML='<div class="empty">Searching every indexer…</div>';
    const d=await j(`/api/v1/release?game=${encodeURIComponent(game)}`
      +`&platform=${encodeURIComponent(platform)}`);
    if(d.error){
      $('#s-out').innerHTML=`<p class="help" style="color:var(--warn)">${esc(d.error)}</p>`;
      return;
    }
    if(!d.items.length){
      $('#s-out').innerHTML='<div class="empty">Nothing found. A search where '
        +'every indexer fails looks exactly like one that found nothing &mdash; '
        +'check Indexers if this is unexpected.</div>';
      return;
    }
    $('#s-out').innerHTML=`
      <p class="help"><b>${d.found}</b> releases, <b>${d.accepted}</b> of them
        acceptable. Rejected rows are shown too, because why a release was
        refused is usually the answer you came for.</p>
      <table><thead><tr><th>Score</th><th>Release</th><th>Size</th>
        <th>Seeders</th><th>Indexer</th><th>Why</th><th></th></tr></thead><tbody>
      ${d.items.map((r,i)=>`<tr style="${r.accepted?'':'opacity:.62'}">
        <td><span class="pill ${r.accepted?'imported':'failed'}">${r.score}</span></td>
        <td>${esc(r.title)}${r.info_url?` <a href="${esc(r.info_url)}" target="_blank"
            rel="noopener noreferrer" title="Open on ${esc(r.indexer||'the indexer')}"
            style="text-decoration:none">&#8599;</a>`:''}
          ${r.private?' <span class="pill">private</span>':''}
          ${r.protocol==='usenet'?' <span class="pill">usenet</span>':''}</td>
        <td style="white-space:nowrap">${mb(r.size)}</td>
        <td>${r.seeders}</td>
        <td style="color:var(--dim)">${esc(r.indexer||'—')}</td>
        <td style="color:var(--dim);font-size:12px">${r.reasons.map(esc).join('<br>')}</td>
        <td><div class="rowact">${r.grabbable
          ? `<button data-grab="${esc(r.id)}">Grab</button>`
          : '<span style="color:var(--dim);font-size:12px">no link</span>'}</div></td>
        </tr>`).join('')}</tbody></table>`;

    document.querySelectorAll('[data-grab]').forEach(b=>b.onclick=async()=>{
      b.disabled=true; b.textContent='Grabbing…';
      const r=await j('/api/v1/release/grab',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({id:b.dataset.grab})});
      b.textContent=r.ok?'Grabbed':'Failed';
      toast(r.ok?`Grabbed ${r.release}`:(r.error||'Could not grab that release'));
      refreshCounts();
    });
  };
  $('#s-go').onclick=run;
  $('#s-name').onkeydown=e=>{ if(e.key==='Enter') run(); };
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

RENDER.lists=async()=>{
  const d=await j('/api/v1/importlist').catch(()=>({items:[]}));
  $('#page').innerHTML=`<div class="row" style="margin-bottom:14px">
      <button class="btn" id="l-add">Add List</button>
      <button class="btn ghost" id="l-sync">Sync Now</button>
      <span style="color:var(--dim);font-size:12.5px">Feed Wanted from a
        list &mdash; a top-100 article, a homebrew catalogue, a friend's
        spreadsheet. Each title is added once, ever; full sets and 1G1R live
        under Collections.</span></div>
    ${d.items.length?`<table><thead><tr><th>Name</th><th>Type</th>
      <th>Platform</th><th>Added so far</th><th>Enabled</th><th></th></tr></thead><tbody>
      ${d.items.map((l,i)=>`<tr><td><b>${esc(l.name||'—')}</b>${
          l.type==='url'?`<div style="color:var(--dim);font-size:11.5px">${esc(l.url||'')}</div>`:''}</td>
        <td>${esc(l.type)}</td><td>${esc(l.platform||'per line')}</td>
        <td>${l.added_count||0}</td>
        <td><span class="dot ${l.enable!==false?'up':'down'}"></span></td>
        <td style="text-align:right"><div class="rowact">
          <button data-ledit="${i}">Edit</button></div></td></tr>`).join('')}
      </tbody></table>`
      :'<div class="empty">No lists yet. Paste one, or connect an account, and let the clock do the asking.</div>'}
    <div class="card" style="margin-top:14px"><h3>Connected accounts</h3>
      <p class="help">Steam, GOG, Xbox, PlayStation and itch.io connect as
        list types above &mdash; add a list and pick the store. The ones that
        cannot connect, and why (no marketing, just the truth):</p>
      <div id="l-noapi" style="color:var(--dim);font-size:12.5px"></div></div>`;
  j('/api/v1/importlist/schema').then(s=>{
    const rows=Object.entries(s.no_api||{});
    $('#l-noapi').innerHTML=rows.map(([store,why])=>
      `<p style="margin:6px 0"><b>${esc(store)}</b> — ${esc(why)}</p>`).join('');
  }).catch(()=>{});
  $('#l-add').onclick=()=>editList({type:'paste'});
  $('#l-sync').onclick=async e=>{
    e.target.disabled=true; e.target.textContent='Syncing…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:'ListSync'})});
    toast(r.message||'Synced'); go('lists'); refreshCounts();
  };
  document.querySelectorAll('[data-ledit]').forEach(b=>b.onclick=()=>
    editList(d.items[Number(b.dataset.ledit)]));
};

function editList(item){
  const isNew=!item.id;
  const m=document.createElement('div');
  m.className='modal';
  m.innerHTML=`<div class="box">
    <h3>${isNew?'Add':'Edit'} Import List</h3>
    <div class="sub">One title per line. '# comments', ranking numbers and
      'Title&nbsp;&rarr;tab&larr;&nbsp;platform' lines are understood.</div>
    <div class="field"><label>Name</label>
      <input type="text" data-f="name" value="${esc(item.name||'')}"></div>
    <div class="field"><label>Type</label>
      <select data-f="type">
        <option value="paste"${!['url','steam','gog'].includes(item.type)?' selected':''}>Pasted list</option>
        <option value="url"${item.type==='url'?' selected':''}>List at a URL</option>
        <option value="steam"${item.type==='steam'?' selected':''}>Steam library / wishlist</option>
        <option value="gog"${item.type==='gog'?' selected':''}>GOG profile</option>
        <option value="xbox"${item.type==='xbox'?' selected':''}>Xbox (OpenXBL)</option>
        <option value="psn"${item.type==='psn'?' selected':''}>PlayStation (NPSSO)</option>
        <option value="itchio"${item.type==='itchio'?' selected':''}>itch.io purchases</option>
      </select></div>
    <div class="field"><label>Default platform</label>
      <select data-f="platform"><option value="">Named per line</option>
        ${PLATFORMS.map(p=>`<option value="${esc(p.slug)}"${
          item.platform===p.slug?' selected':''}>${esc(p.name)}</option>`).join('')}
      </select></div>
    <div class="field" data-lt="url" style="${item.type==='url'?'':'display:none'}">
      <label>URL</label>
      <input type="text" data-f="url" value="${esc(item.url||'')}"
        placeholder="https://example.org/top-100.txt"></div>
    <div data-lt="steam" style="${item.type==='steam'?'':'display:none'}">
      <div class="field"><label>SteamID (64-bit)</label>
        <input type="text" data-f="steam_id" value="${esc(item.steam_id||'')}"
          placeholder="76561198000000000"></div>
      <div class="field"><label>Web API key</label>
        <input type="password" data-f="api_key" autocomplete="new-password"
          value="${esc(item.api_key||'')}"
          placeholder="steamcommunity.com/dev/apikey"></div>
      <div class="field"><label>Source</label>
        <select data-f="source">
          <option value="owned"${item.source!=='wishlist'?' selected':''}>Owned games</option>
          <option value="wishlist"${item.source==='wishlist'?' selected':''}>Wishlist</option>
        </select></div>
      <p class="help">The profile's game details must be public. Titles land
        against the default platform above &mdash; the scorer decides what each
        title means there.</p></div>
    <div class="field" data-lt="gog" style="${item.type==='gog'?'':'display:none'}">
      <label>GOG username</label>
      <input type="text" data-f="gog_username" value="${esc(item.gog_username||'')}"
        placeholder="a public gog.com profile name"></div>
    <div data-lt="xbox" style="${item.type==='xbox'?'':'display:none'}">
      <div class="field"><label>OpenXBL API key</label>
        <input type="password" data-f="openxbl_key" autocomplete="new-password"
          value="${esc(item.openxbl_key||'')}" placeholder="sign in once at xbl.io"></div>
      <p class="help">Pulls your title history &mdash; every game the account
        has played. Microsoft exposes no purchase list to anyone, so played
        IS the practical library.</p></div>
    <div data-lt="psn" style="${item.type==='psn'?'':'display:none'}">
      <div class="field"><label>NPSSO token</label>
        <input type="password" data-f="npsso" autocomplete="new-password"
          value="${esc(item.npsso||'')}"
          placeholder="from ca.account.sony.com/api/authz/v3/ssocookie"></div>
      <p class="help">Sign in at playstation.com, then open the ssocookie URL
        above and copy the token. It expires every couple of months; when a
        sync fails, grab a fresh one.</p></div>
    <div class="field" data-lt="itchio" style="${item.type==='itchio'?'':'display:none'}">
      <label>itch.io API key</label>
      <input type="password" data-f="itchio_key" autocomplete="new-password"
        value="${esc(item.itchio_key||'')}"
        placeholder="itch.io/user/settings/api-keys"></div>
    <div class="field" data-lt="paste" style="${['url','steam','gog'].includes(item.type)?'display:none':''}">
      <label>Titles</label>
      <textarea data-f="content" rows="10"
        style="width:100%;resize:vertical">${esc(item.content||'')}</textarea></div>
    <label class="check"><input type="checkbox" data-f="enable"
      ${item.enable!==false?'checked':''}><span>Enabled</span></label>
    <div id="testline"></div>
    <div class="foot">
      <button class="btn ghost" id="l-preview">Preview</button>
      ${isNew?'':'<button class="btn danger" id="l-del">Delete</button>'}
      <button class="btn ghost sp" data-close>Cancel</button>
      <button class="btn" id="l-save">Save</button>
    </div></div>`;
  document.body.append(m);
  m.onclick=e=>{ if(e.target===m||e.target.dataset.close!==undefined) closeModal(); };
  m.querySelector('[data-f=type]').onchange=e=>{
    const kind=['url','steam','gog','xbox','psn','itchio'].includes(e.target.value)
      ?e.target.value:'paste';
    m.querySelectorAll('[data-lt]').forEach(el=>
      el.style.display=el.dataset.lt===kind?'':'none');
  };
  const payload=()=>({...readForm(), id:item.id});
  const line=(ok,msg)=>{
    m.querySelector('#testline').className='testline '+(ok?'ok':'bad');
    m.querySelector('#testline').textContent=msg;
  };
  m.querySelector('#l-preview').onclick=async e=>{
    e.target.disabled=true; line(true,'Reading…');
    const r=await j('/api/v1/importlist/preview',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(payload())});
    if(r.error){ line(false,r.error); }
    else{
      const unresolved=r.items.filter(x=>x.unresolved).length;
      line(!unresolved,`${r.total} title(s)`
        +(unresolved?`, ${unresolved} with no resolvable platform`:'')
        +(r.total?` — first: ${r.items[0].game}`:''));
    }
    e.target.disabled=false;
  };
  m.querySelector('#l-save').onclick=async()=>{
    await j('/api/v1/importlist',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(payload())});
    closeModal(); toast('Saved'); go('lists');
  };
  const del=m.querySelector('#l-del');
  if(del) del.onclick=async()=>{
    if(!confirm(`Remove ${item.name||'this list'}?`)) return;
    await fetch(`/api/v1/importlist/${item.id}`,{method:'DELETE'});
    closeModal(); toast('Removed'); go('lists');
  };
}

RENDER.discover=async()=>{
  const shelf=(location.hash.split(':')[1])||'popular';
  const tabs=[['popular','Popular'],['new','New releases'],['upcoming','Upcoming']];
  $('#page').innerHTML=`<div class="row" style="margin-bottom:14px">
      ${tabs.map(([k,l])=>`<button class="btn ${k===shelf?'':'ghost'}"
        data-shelf="${k}">${l}</button>`).join('')}
    </div><div id="d-out"><div class="empty">Browsing…</div></div>`;
  document.querySelectorAll('[data-shelf]').forEach(b=>b.onclick=()=>{
    location.hash='discover:'+b.dataset.shelf; go('discover');});
  const d=await j('/api/v1/discover?shelf='+shelf).catch(()=>({items:[]}));
  if(!d.items||!d.items.length){
    $('#d-out').innerHTML=`<div class="empty">${esc(d.error||'Nothing to show.')}</div>`;
    return;
  }
  const requestable=g=>(g.platforms||[]).filter(p=>
    PLATFORMS.some(x=>x.name.toLowerCase()===String(p).toLowerCase()
      ||x.slug===String(p).toLowerCase()));
  $('#d-out').innerHTML=`<div class="grid">${d.items.map((g,i)=>{
    const plats=requestable(g);
    return `<div class="tile">
      <div class="art" style="background-image:url('${esc(g.cover_url||'')}')"></div>
      <div class="nm">${esc(g.title)}</div>
      <div class="pf">${esc(g.released||'')}${g.rating?' · ★'+g.rating.toFixed(1):''}</div>
      ${plats.length?`<div class="rowact" style="margin-top:4px">
        <button data-dreq="${i}">Request</button></div>`
        :`<div class="pf" style="opacity:.6">no retro platform</div>`}</div>`;
  }).join('')}</div>
  <p class="help" style="margin-top:10px">Request shows only for games on a
    platform ROMarr models — the scorer takes it from there, DAT
    verification included.</p>`;
  document.querySelectorAll('[data-dreq]').forEach(b=>b.onclick=async()=>{
    const g=d.items[Number(b.dataset.dreq)];
    const plats=requestable(g);
    const pick=plats.length===1?plats[0]
      :prompt('Which platform?\n'+plats.join(', '), plats[0]);
    if(!pick) return;
    const match=PLATFORMS.find(x=>x.name.toLowerCase()===String(pick).toLowerCase()
      ||x.slug===String(pick).toLowerCase());
    if(!match){toast('Unknown platform');return;}
    b.disabled=true; b.textContent='Requesting…';
    const r=await j('/api/request',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({game:g.title,platform:match.slug})});
    b.textContent=r.ok?'Grabbed':'Wanted';
    toast(r.ok?`Grabbed ${r.release}`:(r.error||'Added to Wanted'));
    refreshCounts();
  });
};

RENDER.stats=async()=>{
  const s=await j('/api/v1/stats');
  const hours=Math.floor((s.uptime_seconds||0)/3600);
  const bar=(entries)=>{
    const rows=Object.entries(entries||{});
    if(!rows.length) return '<div class="empty">Nothing yet.</div>';
    const max=Math.max(...rows.map(([,v])=>v));
    return rows.map(([k,v])=>`<div style="display:flex;align-items:center;gap:8px;margin:4px 0">
      <span style="width:180px;color:var(--dim);font-size:12.5px;text-align:right">${esc(k)}</span>
      <div style="flex:1;background:var(--line);border-radius:3px;height:14px">
        <div style="width:${Math.max(4,Math.round(v/max*100))}%;height:14px;
          background:var(--acc);border-radius:3px"></div></div>
      <b style="width:48px">${v}</b></div>`).join('');
  };
  $('#page').innerHTML=`
    ${s.update_available?`<div class="card" style="border-color:var(--warn)">
      <h3>Update available</h3><p class="help">ROMarr ${esc(s.latest_version)} is out;
      this install is running ${esc(s.version)}. Nothing updates itself &mdash;
      pull the new image when it suits you.</p></div>`:''}
    <div class="card"><h3>This install</h3><div class="st">
      <div><b>${esc(s.version)}</b><span>Version</span></div>
      <div><b>${s.library_games??'—'}</b><span>Games in library</span></div>
      <div><b>${s.wanted}</b><span>Wanted</span></div>
      <div><b>${hours}h</b><span>Uptime</span></div>
      <div><b>${(s.events||{}).grabbed||0}</b><span>Grabbed, ever</span></div>
      <div><b>${(s.events||{}).imported||0}</b><span>Imported, ever</span></div>
      <div><b>${(s.events||{}).failed||0}</b><span>Failures</span></div>
      <div><b>${s.average_rating??'—'}</b><span>Avg rating (${s.rated||0} rated)</span></div>
    </div></div>
    <div class="card"><h3>Imports by platform</h3>${bar(s.imported_by_platform)}</div>
    <div class="card"><h3>Grabs by indexer</h3>${bar(s.grabbed_by_indexer)}</div>
    <div class="card"><h3>Shelf</h3><div class="st">
      <div><b>${(s.statuses||{}).playing||0}</b><span>Playing</span></div>
      <div><b>${(s.statuses||{}).completed||0}</b><span>Completed</span></div>
      <div><b>${(s.statuses||{}).shelved||0}</b><span>Shelved</span></div>
    </div><p class="help">Set from any game tile in the Library.</p></div>`;
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
  if(f.type === 'select')
    return `<div class="field"><label>${esc(f.label)}</label>
      <select data-f="${f.name}">${(f.options||[]).map(o=>{
        const [ov,ot]=Array.isArray(o)?o:[o,o];
        return `<option value="${esc(ov)}"${String(v ?? '')===String(ov)?' selected':''}>${esc(ot)}</option>`;
      }).join('')}</select>${help}</div>`;
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

const sel=(k,label,options,val,help='')=>`<div class="field"><label>${label}</label>
  <select data-k="${k}">${options.map(([v,t])=>
    `<option value="${v}"${String(val)===v?' selected':''}>${t}</option>`).join('')}</select>
  ${help?`<div style="color:var(--dim);font-size:11.5px;margin-top:4px">${help}</div>`:''}</div>`;

RENDER.media=()=>settingsPage('Media Management',
  'Where imported ROMs are filed. This must be the same path your library server scans.',
  fld('library_path','ROM library root',SETTINGS.library_path)
  +sel('library_layout','Folder structure',
     [['flat','Structure A — platform/rom'],['nested','Structure B — platform/roms/rom']],
     SETTINGS.library_layout||'flat',
     'Match your library server. RomM Structure A files as '
     +'&lt;root&gt;/&lt;platform&gt;/&lt;rom&gt;; Structure B adds a roms/ level. '
     +'Per-library overrides live on the Libraries page.')
  +chk('rename_on_import','Rename on import',SETTINGS.rename_on_import)
  +chk('overwrite_existing','Overwrite an existing file',SETTINGS.overwrite_existing)
  +chk('rescan_after_import','Tell your library server to rescan after an import',SETTINGS.rescan_after_import)
  +sel('translation_policy','Fan translations in 1G1R sets',
     [['exclude','Exclude — published dumps only'],
      ['fill','Fill gaps — use a T-En only when no preferred-region dump exists'],
      ['prefer','Prefer — take a T-En over the region winner when one exists'],
      ['keep_both','Keep both — original plus the T-En in a Translations subfolder']],
     SETTINGS.translation_policy||'exclude',
     'What Collections does with an English fan translation. The Digimon-only-'
     +'in-Japan case: "Fill" downloads the translation instead of leaving the '
     +'game Japanese-only; "Keep both" files it under Translations/ so your '
     +'library shows it as a variant.'));

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
  // Answering is not the same as usable. A RomM whose credentials had
  // expired answered its heartbeat and rejected every read, so this said
  // "connected" while nothing could be read out of it.
  const state=l=>!l.ok?'<span class="dot down"></span>unreachable'
    :l.readable===false?'<span class="dot down"></span>cannot read'
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
      ROMarr. Each server needs its own path, as <b>ROMarr</b> sees it.</p>
    ${noDefault?`<p class="help" style="color:var(--warn)">
      No library is marked default, so anything without a matching platform rule
      goes to the first one listed. Mark one to make that a decision.</p>`:''}
    ${unmounted.map(l=>`<p class="help" style="color:var(--warn)">
      <b>${esc(l.name)}</b> answers, but nothing can be imported into it yet.
      ${esc(l.path_hint||'')}</p>
      ${l.readable===false?`<p class="panel-note panel-warn">${esc(l.detail||'')}</p>`:''}`).join('')}
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
     </select></div>
   <h3 style="margin-top:18px">The clock</h3>
   <p class="help">How often the scheduled jobs run. Zero turns a job off;
     changes apply at the next tick, no restart.</p>`
  +fld('auto_import_interval_minutes','Import check (minutes)',
       SETTINGS.auto_import_interval_minutes,'number')
  +fld('search_missing_interval_hours','Wanted search (hours)',
       SETTINGS.search_missing_interval_hours,'number')
  +fld('rss_sync_interval_minutes','RSS sync (minutes)',
       SETTINGS.rss_sync_interval_minutes,'number')
  +fld('list_sync_interval_hours','List sync (hours)',
       SETTINGS.list_sync_interval_hours,'number')
  +chk('update_check','Check github.com daily for a newer ROMarr (never auto-updates)',
       SETTINGS.update_check));

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
      ${h.stream_url
        ? row('Stream server',(h.play_routes||{}).stream>0,h.stream_url)
        : `<tr><td>Stream server</td><td><span class="dot"></span>not configured</td>
           <td style="color:var(--dim)">set STREAM_SERVER_URL to play PS2, GameCube,
           Wii, Dreamcast and 3DS here</td></tr>`}
    </tbody></table></div>
    ${playCard(h.play_routes||{})}
    <div class="card"><h3>About</h3><div class="st">
      <div><b>${esc(h.version)}</b><span>Version</span></div>
      <div><b>${h.platforms}</b><span>Platforms</span></div>
      <div><b>${h.dats||0}</b><span>DATs loaded</span></div>
      <div><b>${h.dat_games||0}</b><span>Known dumps</span></div>
      <div><b>${h.events}</b><span>History events</span></div>
      <div><b>${esc(h.uptime)}</b><span>Uptime</span></div>
    </div></div>

    <div class="card"><h3>Backup and export</h3>
    <p class="help">A snapshot restores an install: settings, libraries,
    indexers, clients, history and the wanted list. Credentials are stripped
    unless you ask for them, so the safe file is the default and the one
    holding secrets takes a deliberate click.</p>
    <div class="row" style="flex-wrap:wrap;gap:8px">
      <button class="btn" id="bk-dl">Download backup</button>
      <button class="btn ghost" id="bk-dls">Download with credentials</button>
      <button class="btn ghost" id="bk-rs">Restore from file…</button>
      <input type="file" id="bk-file" accept="application/json" style="display:none">
    </div>
    <div id="bk-msg" class="testline"></div>
    <p class="help" style="margin-top:16px">Export the library itself, for a
    spreadsheet or another tool:</p>
    <div class="row" style="flex-wrap:wrap;gap:8px">
      <select id="ex-what">
        <option value="library">Library</option>
        <option value="wanted">Wanted</option>
        <option value="blocklist">Blocklist</option>
      </select>
      <select id="ex-fmt"><option value="json">JSON</option>
        <option value="csv">CSV</option></select>
      <button class="btn ghost" id="ex-go">Export</button>
    </div>
    <p class="help" style="margin-top:16px">Or as a frontend's own format:</p>
    <div class="row" style="flex-wrap:wrap;gap:8px" id="fe-row"></div>
    </div>`;

  const msg=(ok,text)=>{const m=$('#bk-msg');
    m.className='testline '+(ok?'ok':'bad'); m.textContent=text;};
  const save=(url,name)=>{const a=document.createElement('a');
    a.href=url; a.download=name; document.body.append(a); a.click(); a.remove();};

  $('#bk-dl').onclick=()=>save('/api/v1/backup','romarr-backup.json');
  $('#bk-dls').onclick=()=>{
    if(confirm('This file will contain your API key and every stored password '
      +'in plain text.\n\nDownload it?'))
      save('/api/v1/backup?secrets=1','romarr-backup-with-credentials.json');
  };
  $('#bk-rs').onclick=()=>$('#bk-file').click();
  $('#bk-file').onchange=async e=>{
    const file=e.target.files[0]; if(!file) return;
    if(!confirm('Restore from '+file.name+'?\n\nThis replaces your current '
      +'settings, libraries, indexers and clients.')) { e.target.value=''; return; }
    msg(true,'Restoring…');
    try{
      const r=await j('/api/v1/restore',{method:'POST',
        headers:{'Content-Type':'application/json'}, body:await file.text()});
      msg(!r.error, r.error||'Restored. Reloading…');
      if(!r.error) setTimeout(()=>location.reload(),1200);
    }catch(_){ msg(false,'That file is not a ROMarr backup.'); }
    e.target.value='';
  };
  $('#ex-go').onclick=()=>save('/api/v1/export?what='+$('#ex-what').value
    +'&format='+$('#ex-fmt').value,
    'romarr-'+$('#ex-what').value+'.'+$('#ex-fmt').value);

  const fe=await j('/api/v1/frontend/formats').catch(()=>({formats:[]}));
  $('#fe-row').innerHTML=(fe.formats||[]).map(f=>
    '<button class="btn ghost fe-btn" data-f="'+esc(f.name||f)+'">'
    +esc(f.label||f.name||f)+'</button>').join('')
    ||'<span class="help" style="margin:0">No frontend formats available.</span>';
  $('#fe-row').querySelectorAll('.fe-btn').forEach(b=>b.onclick=()=>
    save('/api/v1/frontend/export?format='+b.dataset.f,
         'romarr-'+b.dataset.f+'.export'));
};

// How the supported platforms can actually be played, on THIS install.
//
// It belongs on the status page rather than in the docs because the answer
// depends on the operator's own setup: configuring a stream server moves five
// platforms out of "download only", and nothing else in the UI would show
// that it had worked.
const playCard=r=>`<div class="card"><h3>How platforms play here</h3>
  <div class="st">
    <div><b>${r.local||0}</b><span>In the browser (EmulatorJS)</span></div>
    <div><b>${r.stream||0}</b><span>Streamed (headless RetroArch)</span></div>
    <div><b>${r.archive||0}</b><span>On Archive.org</span></div>
    <div><b>${r.download_only||0}</b><span>Download only</span></div>
  </div>
  <p class="help">Every platform can be downloaded. ${
    (r.download_only||0)>0
      ? `${r.download_only} have no player on this install &mdash; a stream
         server is what plays PS2, GameCube, Wii, Dreamcast and 3DS.`
      : 'Every supported platform also plays here without downloading.'}
    See <a href="#platforms">Platforms</a> for the per-platform answer.</p></div>`;

RENDER.platforms=async()=>{
  const rows=await j('/api/platforms').catch(()=>[]);
  const badge=k=>`<span class="pill">${esc(k)}</span>`;
  $('#page').innerHTML=`<div class="card"><h3>Platforms</h3>
    <p class="help">What ROMarr can request, and how each one plays on this
      install. Disc platforms are included: nine of them run in the browser on
      a stock RomM, and the rest stream from a stream server.</p>
    <table><thead><tr><th>Platform</th><th>Media</th><th>Plays</th>
      <th>Ceiling</th><th>Extensions</th></tr></thead><tbody>
    ${rows.map(p=>`<tr>
      <td>${esc(p.name)}<div style="color:var(--dim);font-size:12px">${esc(p.slug)}</div></td>
      <td>${badge(p.media)}</td>
      <td>${(p.play_routes||[]).map(badge).join(' ')}</td>
      <td style="white-space:nowrap">${p.max_size_mb>=1024
          ? (p.max_size_mb/1024).toFixed(0)+' GB' : p.max_size_mb+' MB'}</td>
      <td style="color:var(--dim);font-size:12px">${esc((p.extensions||[]).join(' '))}</td>
    </tr>`).join('')}
    </tbody></table></div>`;
};

// --- Blocklist -------------------------------------------------------------
// Every other *arr shows you a list of blocked releases. This shows why each
// one is blocked, which is the only form of the answer anybody can act on.
RENDER.blocklist=async()=>{
  const d=await j('/api/v1/blocklist').catch(()=>({items:[]}));
  const items=d.items||[];
  const when=t=>t?new Date(t*1000).toLocaleString():'—';
  $('#page').innerHTML='<div class="card"><h3>Blocklist '
    +'<span class="help" style="margin-left:8px">'+items.length+'</span></h3>'
    +'<p class="help">Releases ROMarr will never take again. Each carries the '
    +'reason it was blocked, so lifting one is a decision rather than a guess.</p>'
    +(items.length
      ?'<table><thead><tr><th>Release</th><th>Indexer</th><th>Reason</th>'
       +'<th>Blocked</th><th></th></tr></thead><tbody>'
       +items.map(i=>'<tr><td><b>'+esc(i.title||'(untitled)')+'</b>'
         +'<div class="help" style="margin:2px 0 0;font-size:11px">'+esc(i.id)+'</div></td>'
         +'<td>'+esc(i.indexer||'—')+'</td>'
         +'<td>'+esc(i.reason||'—')+'</td>'
         +'<td style="white-space:nowrap">'+esc(when(i.blocked_at))+'</td>'
         +'<td><button class="mini" data-un="'+esc(i.id)+'">Unblock</button></td></tr>').join('')
       +'</tbody></table>'
      :'<div class="empty-cat"><b>Nothing blocked</b>'
       +'A release lands here when a download fails or you reject it.</div>')
    +'</div>';
  $('#page').querySelectorAll('button[data-un]').forEach(b=>b.onclick=async()=>{
    await fetch('/api/v1/blocklist/'+encodeURIComponent(b.dataset.un),{method:'DELETE'});
    RENDER.blocklist();
  });
};

// --- Connections -----------------------------------------------------------
RENDER.connections=async()=>{
  const [schema,cfg]=await Promise.all([
    j('/api/v1/connection/schema').catch(()=>({types:[]})),
    j('/api/v1/config').catch(()=>({}))]);
  const have=cfg.connections||[];
  const types=schema.types||[];
  $('#page').innerHTML='<div class="card"><h3>Connections</h3>'
    +'<p class="help">Where ROMarr tells you what it did. A grab notification '
    +'carries the reasons the release was chosen, not just its name.</p>'
    +(have.length
      ?'<table><thead><tr><th>Name</th><th>Type</th><th>Events</th><th></th></tr></thead><tbody>'
       +have.map(c=>'<tr><td><b>'+esc(c.name||c.type)+'</b></td>'
         +'<td><span class="pill">'+esc(c.type)+'</span></td>'
         +'<td>'+esc((c.events||['all']).join(', '))+'</td>'
         +'<td><span class="'+(c.enable===false?'dot-off':'dot-ok')+'"></span></td></tr>').join('')
       +'</tbody></table>'
      :'<div class="empty-cat"><b>No connections yet</b>Pick a provider below.</div>')
    +'<div class="row" style="margin-top:14px">'
    +'<button class="btn ghost" id="ctest">Send a test notification</button>'
    +'<span id="ctestmsg" class="help" style="margin:0"></span></div></div>'

    +'<div class="card"><h3>Available providers</h3>'
    +'<div class="pgrid">'+types.map(t=>
      '<div class="pcard"><h4>'+esc(t.label)+'</h4>'
      +'<div class="desc">'+esc(t.help)+'</div>'
      +'<div class="meta">'+(t.fields||[]).map(f=>'<span class="pill">'+esc(f)+'</span>').join(' ')+'</div>'
      +'</div>').join('')+'</div></div>';

  $('#ctest').onclick=async()=>{
    const m=$('#ctestmsg'); m.textContent='Sending…';
    const r=await fetch('/api/v1/connection/test',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    const res=d.results||[];
    m.textContent = res.length
      ? res.map(x=>x.name+': '+(x.ok?'delivered':'failed')).join(' · ')
      : 'No connections configured.';
  };
};

// --- Metadata --------------------------------------------------------------
RENDER.metadata=async()=>{
  const schema=await j('/api/v1/metadata/schema').catch(()=>({providers:[]}));
  const probe=await j('/api/v1/metadata/lookup?filename='
    +encodeURIComponent('Chrono.Trigger.USA.v1.1.smc')).catch(()=>({}));
  $('#page').innerHTML='<div class="card"><h3>Metadata</h3>'
    +'<p class="help">ROMarr looks a game up by its <b>DAT-verified name</b> when it '
    +'has one, and only falls back to parsing the filename when it does not. '
    +'Every result says which was used, because a cover matched from a guess '
    +'deserves less trust than one matched from a hash.</p>'
    +'<table><thead><tr><th>Provider</th><th>Needs</th><th>Notes</th></tr></thead><tbody>'
    +(schema.providers||[]).map(p=>'<tr><td><b>'+esc(p.label)+'</b></td>'
      +'<td>'+(p.fields||[]).map(f=>'<span class="pill">'+esc(f)+'</span>').join(' ')+'</td>'
      +'<td class="help" style="margin:0">'+esc(p.help)+'</td></tr>').join('')
    +'</tbody></table></div>'

    +'<div class="card"><h3>Try it</h3>'
    +'<p class="help">A live lookup against whatever you have configured.</p>'
    +'<div class="field"><label>Filename</label>'
    +'<input id="mfn" value="Chrono.Trigger.USA.v1.1.smc"></div>'
    +'<button class="mini accent" id="mgo">Identify</button>'
    +'<div id="mout" style="margin-top:12px">'+metaResult(probe)+'</div></div>';

  $('#mgo').onclick=async()=>{
    const d=await j('/api/v1/metadata/lookup?filename='
      +encodeURIComponent($('#mfn').value)).catch(()=>({}));
    $('#mout').innerHTML=metaResult(d);
  };
};

const metaResult=d=>{
  if(!d||!d.matched_by) return '<div class="help">No answer.</div>';
  const badge = d.matched_by==='dat'
    ? '<span class="pill" style="background:var(--ok);color:#08210d">matched by DAT — exact</span>'
    : '<span class="pill">matched by filename — a guess</span>';
  return '<div class="pcard"><h4>'+esc(d.title||'Not identified')+'</h4>'
    +'<div class="meta">'+badge+(d.source?'<span class="pill">'+esc(d.source)+'</span>':'')+'</div>'
    +(d.summary?'<div class="desc">'+esc(d.summary)+'</div>':'')
    +(d.found?'':'<div class="help" style="margin:0">Configure a provider above to '
      +'turn this into a title, cover and description.</div>')+'</div>';
};

// --- Calendar --------------------------------------------------------------
RENDER.calendar=async()=>{
  const d=await j('/api/v1/calendar').catch(()=>({items:[],error:'unavailable'}));
  const items=d.items||[];
  $('#page').innerHTML='<div class="card"><h3>Release Calendar</h3>'
    +'<p class="help">Games out recently or due soon. The window looks both ways '
    +'on purpose — most of what you want to acquire came out last month, not next.</p>'
    +(d.error?'<div class="panel-note panel-warn">'+esc(d.error)+'</div>':'')
    +(items.length
      ?'<div class="pgrid">'+items.map(g=>'<div class="pcard">'
        +'<h4>'+esc(g.title)+'</h4>'
        +'<div class="meta"><span class="pill">'+esc(g.released||'TBA')+'</span>'
        +(g.upcoming?'<span class="pill" style="background:var(--info);color:#06131f">upcoming</span>':'')
        +'</div>'
        +'<div class="desc">'+esc((g.platforms||[]).slice(0,4).join(', '))+'</div>'
        +'</div>').join('')+'</div>'
      :'<div class="empty-cat"><b>Nothing to show</b>'
       +'Add a metadata provider with an API key under Settings → Metadata.</div>')
    +'</div>';
};

// --- Manual Import ---------------------------------------------------------
// Radarr calls this Manual Import and it exists for the same reason: somebody
// arrives with a library already on disk, and telling them to re-download
// everything ROMarr could have adopted is absurd.
RENDER.getstarted=async()=>{
  // The question a capable new self-hoster actually asked: "if I host ROMs,
  // where do I play them?" ROMarr acquires and files; something else plays.
  // Saying so plainly beats implying ROMarr is a frontend it is not.
  const [status,libs,plat]=await Promise.all([
    j('/api/v1/system/status').catch(()=>({})),
    j('/api/v1/library').catch(()=>({items:[]})),
    j('/api/platforms').catch(()=>({platforms:[]}))
  ]);
  const items=libs.items||[];
  const platforms=plat.platforms||[];
  const ok=x=>x?'<span class="pill" style="background:var(--ok);color:#08210d">ready</span>'
                :'<span class="pill" style="background:var(--warn);color:#2a1c05">not set up</span>';

  const indexerOk=!!(status.prowlarr||(status.indexers||0)>0);
  const clientOk=!!(status.qbittorrent||status.sabnzbd||status.nzbget
                    ||(status.download_clients||0)>0);
  const libOk=items.some(l=>l.ok);
  const datOk=(status.dats||0)>0||!!status.dat_games;

  const step=(n,title,done,body)=>'<div style="display:flex;gap:14px;'
    +'padding:14px 0;border-bottom:1px solid var(--line)">'
    +'<div style="flex:0 0 30px;height:30px;border-radius:50%;display:flex;'
    +'align-items:center;justify-content:center;font-weight:600;'
    +'background:'+(done?'var(--ok)':'var(--rail-2)')+';'
    +'color:'+(done?'#08210d':'var(--dim)')+'">'+(done?'✓':n)+'</div>'
    +'<div style="flex:1"><div style="font-weight:600;margin-bottom:3px">'
    +title+' '+ok(done)+'</div>'
    +'<div class="help" style="margin:0">'+body+'</div></div></div>';

  const counts={};
  platforms.forEach(p=>{const r=(p.play&&p.play.route)||'unknown';
    counts[r]=(counts[r]||0)+1;});
  const routeRow=(k,label,what)=>counts[k]
    ?'<tr><td><b>'+counts[k]+'</b> platforms</td><td>'+esc(label)+'</td>'
     +'<td class="help" style="margin:0">'+what+'</td></tr>':'';

  $('#page').innerHTML=
    '<div class="card"><h3>What ROMarr is</h3>'
    +'<p class="help">ROMarr is the acquisition and automation layer: it '
    +'searches your indexers, grabs the best release, checks it against a '
    +'No-Intro or Redump DAT, and files it into your library. '
    +'<b>It is not an emulator and not a game launcher.</b> Something else '
    +'plays the ROM — ROMarr makes sure the right file is in the right place '
    +'for it.</p>'
    +'<div style="font-family:ui-monospace,monospace;font-size:12.5px;'
    +'background:var(--bg);border:1px solid var(--line);border-radius:6px;'
    +'padding:14px;overflow-x:auto;line-height:1.9">'
    +'<b>Acquire</b> → <b>Verify</b> → <b>File</b> → <b>Scan</b> → <b>Play</b><br>'
    +'<span style="color:var(--dim)">indexers &nbsp; DAT hashes &nbsp; '
    +'library root &nbsp; library server &nbsp; a frontend</span><br>'
    +'<span style="color:var(--dim)">└─ ROMarr does the first four ─┘ &nbsp; '
    +'└ you choose this ┘</span>'
    +'</div></div>'

    +'<div class="card" style="margin-top:16px"><h3>Your setup</h3>'
    +step(1,'An indexer',indexerOk,
       'Prowlarr, or Torznab/Newznab indexers added directly under '
       +'<a href="#indexers">Indexers</a>. This is where releases are found.')
    +step(2,'A download client',clientOk,
       'qBittorrent for torrents, SABnzbd or NZBGet for usenet. Set under '
       +'<a href="#clients">Download Clients</a>.')
    +step(3,'A library',libOk,
       'Where ROMs are filed and what serves them. RomM, Gaseous, Retrom, or '
       +'a plain folder that Batocera, ES-DE, EmuDeck or LaunchBox reads. '
       +'Set under <a href="#libraries">Libraries</a>.')
    +step(4,'DATs (optional, recommended)',datOk,
       'No-Intro and Redump checksums. With them ROMarr can say a file is the '
       +'exact known-good dump rather than merely the right size — and '
       +'<a href="#collections">Collections</a> can tell you what a complete '
       +'set is missing.')
    +'</div>'

    +'<div class="card" style="margin-top:16px"><h3>Where you actually play</h3>'
    +'<p class="help">ROMarr files the ROM; one of these runs it. You do not '
    +'need all of them — one is enough.</p>'
    +'<table><thead><tr><th>If your library is</th><th>You play in</th></tr></thead><tbody>'
    +'<tr><td>RomM</td><td>RomM\'s built-in EmulatorJS, in the browser</td></tr>'
    +'<tr><td>Gaseous or Retrom</td><td>Their own web players and clients</td></tr>'
    +'<tr><td>A folder</td><td>Batocera, RetroPie, Recalbox, ES-DE, EmuDeck, '
    +'Lakka, muOS, LaunchBox or Playnite — they all read a per-platform '
    +'directory, which is exactly what ROMarr writes</td></tr>'
    +'</tbody></table>'
    +'<p class="help">ROMarr can also export your library as LaunchBox XML, an '
    +'ES-DE <code>gamelist.xml</code> or Playnite JSON — see '
    +'<a href="#status">System</a>.</p>'
    +(Object.keys(counts).length
      ?'<h3 style="margin-top:18px">On this install</h3>'
       +'<table><thead><tr><th></th><th>Route</th><th>What that means</th>'
       +'</tr></thead><tbody>'
       +routeRow('browser','Browser emulator',
          'Playable in the browser through your library server.')
       +routeRow('stream','Streamed',
          'Too heavy for the browser; rendered by a headless RetroArch and '
          +'streamed to you.')
       +routeRow('download','Download only',
          'ROMarr fetches and verifies it; you play it in a native emulator.')
       +'</tbody></table>':'')
    +'</div>'

    +'<div class="card" style="margin-top:16px"><h3>Next</h3>'
    +'<p class="help">'
    +'<a href="#add">Request a game</a> to watch the whole chain run once, or '
    +'<a href="#manualimport">Manual Import</a> to adopt ROMs you already have. '
    +'If you want a whole system at once, <a href="#collections">Collections</a> '
    +'compares a DAT against your shelf and requests only what is missing.'
    +'</p></div>';
};

RENDER.collections=async()=>{
  const st=await j('/api/v1/collection').catch(()=>({dats:[],batches:[]}));
  const dats=st.dats||[];
  $('#page').innerHTML='<div class="card"><h3>Plan a set</h3>'
    +(dats.length
      ?'<p class="help">Compare a DAT against your library. Nothing is '
       +'requested until you say so.</p>'
       +'<div class="row" style="flex-wrap:wrap;gap:10px">'
       +'<select id="cdat">'+dats.map(d=>'<option>'+esc(d)+'</option>').join('')+'</select>'
       +'<select id="cplat"><option value="">platform (match on disk)</option>'
       +PLATFORMS.map(p=>'<option value="'+esc(p.slug)+'">'+esc(p.name)+'</option>').join('')
       +'</select>'
       +'<label class="help" style="margin:0"><input type="checkbox" id="c1g1r" checked> '
       +'One game, one ROM</label>'
       +'<input id="cregions" placeholder="usa,world,europe,japan" '
       +'style="flex:1;min-width:180px;padding:7px 10px;background:var(--bg);'
       +'color:var(--fg);border:1px solid var(--line);border-radius:6px">'
       +'<button class="btn" id="cplan">Preview plan</button></div>'
       +'<div class="row" style="flex-wrap:wrap;gap:14px;margin-top:10px">'
       +['proto','beta','demo','hack','unlicensed'].map(k=>
          '<label class="help" style="margin:0"><input type="checkbox" class="cex" '
          +'value="'+k+'" checked> exclude '+k+'</label>').join('')
       +'</div>'
       +'<div class="row" style="gap:8px;margin-top:10px;align-items:center">'
       +'<label class="help" style="margin:0">Fan translations</label>'
       +'<select id="ctrans">'
       +[['exclude','exclude (published dumps only)'],
         ['fill','fill gaps (T-En only where no preferred region exists)'],
         ['prefer','prefer (T-En over the region winner)'],
         ['keep_both','keep both (original + T-En under Translations/)']].map(o=>
          '<option value="'+o[0]+'"'+((SETTINGS.translation_policy||'exclude')===o[0]?' selected':'')
          +'>'+o[1]+'</option>').join('')
       +'</select></div>'
      :'<div class="empty-cat"><b>No DAT loaded</b>'
       +'Point DAT_PATH at a directory of No-Intro or Redump DATs. Without one '
       +'there is no list of what a complete set contains.</div>')
    +'<div id="cres" style="margin-top:16px"></div></div>'
    +'<div class="card" style="margin-top:16px"><h3>Batches</h3>'
    +'<div id="cbatch"></div></div>';

  const drawBatches=async()=>{
    const s=await j('/api/v1/collection').catch(()=>({batches:[]}));
    const b=s.batches||[];
    $('#cbatch').innerHTML=b.length
      ?'<table><thead><tr><th>Set</th><th>Progress</th><th>Done</th>'
       +'<th>Failed</th><th>Left</th><th></th></tr></thead><tbody>'
       +b.map(x=>'<tr data-id="'+esc(x.id)+'"><td>'+esc(x.dat||x.platform||x.id)+'</td>'
         +'<td style="min-width:150px"><div style="background:var(--bg);'
         +'border:1px solid var(--line);border-radius:4px;height:16px;overflow:hidden">'
         +'<div style="height:100%;width:'+x.percent+'%;background:var(--accent)"></div>'
         +'</div><span class="help" style="margin:0">'+x.percent+'% — '+esc(x.status)+'</span></td>'
         +'<td>'+x.done+'</td><td>'+x.failed+'</td><td>'+x.remaining+'</td>'
         +'<td class="row" style="gap:6px">'
         +'<button class="btn ghost cstep">Run next</button>'
         +'<button class="btn ghost cact" data-a="'+(x.status==='paused'?'resume':'pause')+'">'
         +(x.status==='paused'?'Resume':'Pause')+'</button>'
         +(x.failed?'<button class="btn ghost cact" data-a="retry">Retry failed</button>':'')
         +'<button class="btn ghost cact" data-a="cancel">Cancel</button></td></tr>').join('')
       +'</tbody></table>'
      :'<div class="empty">No set is being acquired.</div>';

    $('#cbatch').querySelectorAll('.cstep').forEach(el=>el.onclick=async e=>{
      const id=e.target.closest('tr').dataset.id;
      e.target.disabled=true; e.target.textContent='Running…';
      await j('/api/v1/collection/step',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
      drawBatches();
    });
    $('#cbatch').querySelectorAll('.cact').forEach(el=>el.onclick=async e=>{
      const id=e.target.closest('tr').dataset.id, action=e.target.dataset.a;
      if(action==='cancel'&&!confirm('Cancel this set? Titles already requested are unaffected.')) return;
      await j('/api/v1/collection/control',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id,action:action})});
      drawBatches();
    });
  };
  drawBatches();

  const planBtn=$('#cplan');
  if(!planBtn) return;
  planBtn.onclick=async()=>{
    const out=$('#cres');
    out.innerHTML='<div class="empty">Planning…</div>';
    const ex=[...document.querySelectorAll('.cex')].filter(c=>c.checked)
      .map(c=>c.value).join(',');
    const q='dat='+encodeURIComponent($('#cdat').value)
      +'&platform='+encodeURIComponent($('#cplat').value)
      +'&onegame='+($('#c1g1r').checked?'1':'0')
      +'&regions='+encodeURIComponent($('#cregions').value.trim())
      +'&translation_policy='+encodeURIComponent($('#ctrans').value)
      +'&exclude='+encodeURIComponent(ex);
    const d=await j('/api/v1/collection/plan?'+q).catch(()=>({error:'plan failed'}));
    if(d.error){out.innerHTML='<div class="panel-note panel-warn">'+esc(d.error)+'</div>';return;}
    const c=d.counts;
    const tile=(n,label,colour)=>'<div style="flex:1;min-width:110px;padding:10px 12px;'
      +'background:var(--bg);border:1px solid var(--line);border-radius:6px">'
      +'<div style="font-size:22px;font-weight:600;color:'+colour+'">'+n+'</div>'
      +'<div class="help" style="margin:0">'+label+'</div></div>';
    out.innerHTML='<div class="row" style="gap:10px;flex-wrap:wrap">'
      +tile(c.expected,'in the set','var(--fg)')
      +tile(c.have,'you have','var(--ok)')
      +tile(c.missing,'missing','var(--warn)')
      +tile(c.bad,'bad dumps','var(--bad)')
      +tile(c.excluded,'excluded by policy','var(--dim)')
      +'</div>'
      +'<p class="help">'+esc(d.dat)+(d.dat_version?' ('+esc(d.dat_version)+')':'')
      +' — region order '+esc((d.policy.regions||[]).join(' → '))+'.</p>'
      +'<div class="row"><button class="btn" id="cstart">Request '+c.missing
      +' missing</button>'
      +'<label class="help" style="margin:0">at <input id="cpp" type="number" '
      +'min="1" max="50" value="5" style="width:60px;padding:5px;background:var(--bg);'
      +'color:var(--fg);border:1px solid var(--line);border-radius:4px"> per pass</label></div>'
      +'<table style="margin-top:14px"><thead><tr><th>Title</th><th>Status</th>'
      +'<th>Why this dump</th></tr></thead><tbody>'
      +d.titles.slice(0,300).map(t=>{
        const pill=t.status==='verified'
          ?'<span class="pill" style="background:var(--ok);color:#08210d">verified</span>'
          :t.status==='bad'
            ?'<span class="pill" style="background:var(--bad);color:#2a0b0b">bad dump</span>'
            :t.status==='missing'
              ?'<span class="pill" style="background:var(--warn);color:#2a1c05">missing</span>'
              :'<span class="pill">present</span>';
        const clones=(t.discarded||[]).length
          ?'<details><summary class="help" style="margin:0;cursor:pointer">'
           +t.discarded.length+' other dump'+(t.discarded.length>1?'s':'')+'</summary>'
           +'<ul style="margin:6px 0 0 16px">'+t.discarded.map(x=>
             '<li class="help" style="margin:0">'+esc(x.name)+' — '+esc(x.why)+'</li>').join('')
           +'</ul></details>':'';
        return '<tr><td>'+esc(t.name)
          +(t.outside_preference?' <span class="pill" title="No dump in your preferred regions; kept so the game is not lost">outside regions</span>':'')
          +'</td><td>'+pill+'</td><td class="help" style="margin:0">'+esc(t.why)
          +clones+'</td></tr>';
      }).join('')+'</tbody></table>'
      +(d.titles.length>300?'<p class="help">Showing the first 300 of '
        +d.titles.length+'.</p>':'');

    $('#cstart').onclick=async e=>{
      e.target.disabled=true; e.target.textContent='Queueing…';
      const r=await j('/api/v1/collection/start',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({dat:$('#cdat').value,
          platform:$('#cplat').value,
          per_pass:parseInt($('#cpp').value,10)||5,
          one_game_one_rom:$('#c1g1r').checked,
          regions:($('#cregions').value.trim()||'').split(',').filter(Boolean),
          translation_policy:$('#ctrans').value,
          exclude:ex.split(',').filter(Boolean)})});
      toast(r.error?r.error:('Queued '+(r.queued||0)+' titles'));
      e.target.disabled=false; e.target.textContent='Request missing';
      drawBatches();
    };
  };
};

RENDER.manualimport=async()=>{
  $('#page').innerHTML='<div class="card"><h3>Manual Import</h3>'
    +'<p class="help">Point ROMarr at a directory you already have. It works out '
    +'which platform each file belongs to and verifies against your DATs, then '
    +'you choose what to adopt.</p>'
    +'<div class="row"><input id="mipath" placeholder="/downloads/roms" '
    +'style="flex:1;padding:8px 12px;background:var(--bg);color:var(--fg);'
    +'border:1px solid var(--line);border-radius:6px">'
    +'<button class="btn" id="miscan">Scan</button></div>'
    +'<div id="mires" style="margin-top:14px"></div></div>';

  const opts=sel=>PLATFORMS.map(p=>'<option value="'+esc(p.slug)+'"'
      +(p.slug===sel?' selected':'')+'>'+esc(p.name)+'</option>').join('');

  // The distinction the whole page turns on. UNKNOWN means "not in the DAT you
  // loaded" -- normal for homebrew, translations and anything newer than your
  // DAT -- and must never be presented as a problem. Only a bad dump is.
  const verdict=v=>v==='verified'
    ? '<span class="pill" style="background:var(--ok);color:#08210d">verified</span>'
    : v==='bad-dump'
      ? '<span class="pill" style="background:var(--bad);color:#2a0b0b">bad dump</span>'
      : '<span class="pill" title="Not in your DAT. Often perfectly fine.">unknown</span>';

  async function adopt(row, force){
    const btn=row.querySelector('.mi-go');
    const st=row.querySelector('.mi-st');
    btn.disabled=true; st.textContent='Importing…';
    const d=await j('/api/v1/manualimport',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:row.dataset.path,
        platform:row.querySelector('.mi-pf').value, force:!!force})})
      .catch(()=>({ok:false,refused:[{reason:'request failed'}]}));

    if(d.ok){
      const one=d.imported[0]||{};
      st.innerHTML=one.forced
        ? '<span style="color:var(--warn)">imported (forced)</span>'
        : '<span style="color:var(--ok)">imported</span>';
      btn.remove();
      row.querySelector('.mi-pf').disabled=true;
      return;
    }
    const bad=(d.refused||[])[0]||{};
    st.innerHTML='<span style="color:var(--bad)">'+esc(bad.reason||'refused')+'</span>';
    btn.disabled=false;
    if(bad.needs_force){
      // Never a silent reinterpretation: forcing is a second, separate act,
      // and the button says exactly what it does.
      btn.textContent='Import anyway';
      btn.style.background='var(--bad)';
      btn.onclick=()=>{
        if(confirm('This file did not match a known dump.\n\n'
          +'Import it anyway? It will be recorded as a forced import, and '
          +'ROMarr will not claim it is verified.')) adopt(row,true);
      };
    }
  }

  $('#miscan').onclick=async()=>{
    const out=$('#mires');
    out.innerHTML='<div class="empty">Scanning…</div>';
    const d=await j('/api/v1/manualimport?path='
      +encodeURIComponent($('#mipath').value)).catch(()=>({error:'scan failed'}));
    if(d.error){out.innerHTML='<div class="panel-note panel-warn">'+esc(d.error)+'</div>';return;}
    const c=d.candidates||[];
    out.innerHTML='<p class="help">'+c.length+' importable, '+(d.skipped||0)+' skipped.</p>'
      +(c.length
        ?'<div class="row" style="margin-bottom:10px">'
         +'<button class="btn ghost" id="miall">Import everything verified</button></div>'
         +'<table><thead><tr><th>File</th><th>Platform</th><th>Why</th>'
         +'<th>DAT</th><th></th><th></th></tr></thead><tbody>'
         +c.slice(0,200).map(x=>'<tr data-path="'+esc(x.path||'')+'" '
           +'data-verdict="'+esc(x.verdict)+'">'
           +'<td>'+esc(x.filename)+'</td>'
           +'<td><select class="mi-pf">'+opts(x.platform)+'</select></td>'
           +'<td class="help" style="margin:0">'+esc(x.reason)+'</td>'
           +'<td>'+(x.hollow?'<span class="pill" style="background:var(--bad);color:#2a0b0b" title="'+esc(x.hollow)+'">EMPTY FILE</span><br>':'')+verdict(x.verdict)+(x.header_says?'<br><span class="pill" style="background:var(--warn);color:#2a1c05" title="'+esc(x.header_detail||'')+'">header says '+esc(x.header_says)+'</span>':'')+'</td>'
           +'<td><button class="btn mi-go">Import</button></td>'
           +'<td class="mi-st help" style="margin:0"></td></tr>').join('')
         +'</tbody></table>'
        :'<div class="empty-cat"><b>Nothing importable there</b>'
         +'Check the path, or that the files carry extensions ROMarr knows.</div>');

    out.querySelectorAll('tbody tr').forEach(row=>{
      const btn=row.querySelector('.mi-go');
      if(btn) btn.onclick=()=>adopt(row,false);
    });
    const all=out.querySelector('#miall');
    if(all) all.onclick=async()=>{
      all.disabled=true;
      // Verified only. A bulk button that also forced bad dumps would be a
      // way to override verification without ever deciding to.
      for(const row of out.querySelectorAll('tbody tr[data-verdict="verified"]')){
        if(row.querySelector('.mi-go')) await adopt(row,false);
      }
      all.disabled=false;
    };
  };
};


RENDER.tasks=async()=>{
  const d=await j('/api/v1/system/tasks').catch(()=>({items:[]}));
  const every=s=>!s?'off'
    :s%3600===0?`every ${s/3600} h`
    :s%60===0?`every ${s/60} min`:`every ${s} s`;
  const scheduled=(d.items||[]).map(t=>[t.name,t.label,t]);
  const manualOnly=[['RefreshLibrary','Re-read the library from RomM',null]];
  $('#page').innerHTML=`<div class="card"><h3>Tasks</h3>
    <p class="help">The service runs these on its own clock &mdash; intervals live
      under Settings &rarr; General. Run starts one now regardless.</p>
    <table><thead><tr><th>Task</th><th>Schedule</th><th>Last ran</th>
      <th>Result</th><th></th></tr></thead><tbody>
    ${scheduled.concat(manualOnly).map(([n,label,t])=>`<tr><td><b>${n}</b>
      <div style="color:var(--dim);font-size:12px">${esc(label)}</div></td>
      <td>${t?every(t.interval_seconds):'manual only'}</td>
      <td style="color:var(--dim)">${t&&t.last_run?esc(t.last_run.replace('T',' ').slice(0,16)):'—'}</td>
      <td style="color:var(--dim);font-size:12px">${
        t&&t.last_error?`<span style="color:var(--warn)">${esc(t.last_error)}</span>`
        :esc((t&&t.last_result)||'—')}</td>
      <td style="text-align:right"><button class="btn ghost" data-task="${n}">Run</button></td>
      </tr>`).join('')}</tbody></table></div>`;
  document.querySelectorAll('[data-task]').forEach(b=>b.onclick=async()=>{
    b.disabled=true; b.textContent='Running…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:b.dataset.task})});
    toast(r.message||'Done'); go('tasks'); refreshCounts();
  });
};

let LOG_TIMER=null;
RENDER.logs=async()=>{
  clearInterval(LOG_TIMER);
  $('#page').innerHTML=`<div class="card"><h3>Live log</h3>
    <p class="help">The process log, tailed live — the same lines journalctl
      or docker logs would show. Events (grabs, imports) stay under
      Activity &rarr; History.</p>
    <div class="row" style="margin-bottom:10px">
      <select id="lg-level">
        <option value="">Everything</option>
        <option value="INFO" selected>Info and up</option>
        <option value="WARNING">Warnings and up</option>
        <option value="ERROR">Errors only</option>
      </select>
      <label class="check" style="margin:0"><input type="checkbox" id="lg-follow"
        checked><span>Follow</span></label>
    </div>
    <pre id="lg-out" style="font:12px/1.6 ui-monospace,Menlo,monospace;
      color:var(--dim);white-space:pre-wrap;max-height:70vh;overflow:auto"></pre></div>`;
  let since=0;
  const paint=rows=>{
    if(!rows.length) return;
    const out=$('#lg-out'); if(!out) return;
    out.textContent+=rows.map(r=>
      `${r.at.replace('T',' ').slice(0,19)}  ${r.level.padEnd(7)} ${r.name}  ${r.message}`)
      .join('\n')+'\n';
    if($('#lg-follow')?.checked) out.scrollTop=out.scrollHeight;
  };
  const pull=async()=>{
    if(!$('#lg-out')){clearInterval(LOG_TIMER);return;}
    const level=$('#lg-level').value;
    const d=await j(`/api/v1/log/tail?since=${since}&level=${level}`).catch(()=>null);
    if(!d) return;
    paint(d.items||[]); since=d.latest||since;
  };
  $('#lg-level').onchange=()=>{since=0;$('#lg-out').textContent='';pull();};
  await pull();
  LOG_TIMER=setInterval(pull,2000);
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
<title>ROMarr</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body>
<nav id="rail"><div id="brand">ROM<span>arr</span></div>{_nav_html()}</nav>
<div id="main">
  <div id="top"><h1>Games</h1><input id="search" placeholder="Filter…" autocomplete="off"></div>
  <div class="page" id="page"></div>
</div>
<script>{JS}</script></body></html>"""


# ------------------------------------------------------------ the door in --
#
# The API gate was built before the door. Every page of the UI loaded, and then
# every request it made came back 401, because a browser had no way to present
# a credential and no screen on which to offer one -- and the log line telling
# the operator to look under Settings pointed at a page that was itself 401ing.
# That is issue #8: an install that looks like it works and cannot be used.
#
# Two states, because a fresh install has nobody in it yet:
#
#   setup  -- nothing has claimed this install. The first visitor sets the
#             password. This is how Jellyfin, Gitea, Nextcloud and Authentik
#             all bootstrap, and it beats printing a secret into the log where
#             it stays forever and gets pasted into bug reports.
#   signin -- somebody has. Password, or an API key for anyone who kept one.
#
# An operator who does not want an unclaimed window at all sets ROMARR_PASSWORD
# or ROMARR_API_KEY in their template, and never sees the setup screen.

LOGIN_CSS = """
body{display:flex;align-items:center;justify-content:center;min-height:100vh;
  padding:20px}
.box{width:100%;max-width:380px;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;padding:28px}
.box h1{display:flex;gap:0;font-size:24px;font-weight:700;letter-spacing:-.02em;
  justify-content:center;margin-bottom:6px}
.box h1 span{color:var(--accent)}
.box .sub{text-align:center;color:var(--dim);font-size:13px;margin-bottom:22px}
.box label{display:block;font-size:12px;color:var(--dim);margin:14px 0 5px;
  text-transform:uppercase;letter-spacing:.06em}
.box input{width:100%;padding:9px 12px;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:4px;outline:none;font-size:14px}
.box input:focus{border-color:var(--accent)}
.box button{width:100%;margin-top:20px;padding:10px;background:var(--accent);
  color:var(--accent-ink);border:0;border-radius:4px;font-size:14px;
  font-weight:600;cursor:pointer}
.box button:hover{filter:brightness(1.08)}
.box button:disabled{opacity:.6;cursor:default}
.note{margin-top:18px;padding:11px 13px;background:var(--bg);
  border:1px solid var(--line);border-left:3px solid var(--info);
  border-radius:4px;color:var(--dim);font-size:12px;line-height:1.6}
.err{margin-top:16px;padding:10px 12px;border-radius:4px;font-size:13px;
  background:#3a2020;border:1px solid var(--bad);color:#ffb4b4;display:none}
.err.on{display:block}
.alt{margin-top:16px;text-align:center;font-size:12px;color:var(--dim)}
.alt a{cursor:pointer}
@media(max-width:420px){.box{padding:22px 18px}}
"""


def login_page(*, claimed: bool, totp: bool = False) -> str:
    """The sign-in screen, or the first-run claim screen.

    Server-rendered and served unauthenticated, because the whole failure was
    a browser with no way to obtain a credential.
    """
    if claimed:
        title, action = "Sign in", "Sign in"
        fields = """
  <label for="password">Password</label>
  <input id="password" type="password" autocomplete="current-password" autofocus>
""" + ("""
  <label for="totp">Two-factor code</label>
  <input id="totp" inputmode="numeric" autocomplete="one-time-code"
         placeholder="000000">
""" if totp else "") + """
  <div class="alt"><a id="usekey">Use an API key instead</a></div>
  <div id="keyrow" style="display:none">
    <label for="apikey">API key</label>
    <input id="apikey" type="password" autocomplete="off">
  </div>
"""
        note = ("Lost the password? Set <code>ROMARR_API_KEY</code> in the "
                "container's environment and restart, then sign in with it.")
    else:
        title, action = "Set your password", "Create password"
        fields = """
  <label for="password">New password</label>
  <input id="password" type="password" autocomplete="new-password"
         minlength="8" autofocus>
  <label for="confirm">Confirm password</label>
  <input id="confirm" type="password" autocomplete="new-password" minlength="8">
"""
        note = ("Nobody has claimed this ROMarr yet, so this screen is open. "
                "Set the password now. To skip this step on future installs, "
                "put <code>ROMARR_PASSWORD</code> in your container "
                "environment.")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{title} &middot; ROMarr</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}{LOGIN_CSS}</style></head><body>
<form class="box" id="f" autocomplete="on">
  <h1>ROM<span>arr</span></h1>
  <div class="sub">{title}</div>
  {fields}
  <button type="submit" id="go">{action}</button>
  <div class="err" id="err"></div>
  <div class="note">{note}</div>
</form>
<script>
var claimed = {str(bool(claimed)).lower()};
var f = document.getElementById('f'), err = document.getElementById('err'),
    go = document.getElementById('go');
var keylink = document.getElementById('usekey');
if (keylink) keylink.onclick = function() {{
  document.getElementById('keyrow').style.display = 'block';
  keylink.parentNode.style.display = 'none';
  document.getElementById('apikey').focus();
}};
function fail(m) {{ err.textContent = m; err.className = 'err on';
                    go.disabled = false; go.textContent = claimed ?
                    'Sign in' : 'Create password'; }}
f.onsubmit = async function(e) {{
  e.preventDefault();
  err.className = 'err';
  var pw = document.getElementById('password').value;
  var body, url;
  if (claimed) {{
    var keyEl = document.getElementById('apikey');
    var key = keyEl ? keyEl.value : '';
    var totpEl = document.getElementById('totp');
    url = '/api/v1/login';
    body = {{password: pw, apikey: key, totp: totpEl ? totpEl.value : ''}};
  }} else {{
    var confirmEl = document.getElementById('confirm');
    if (pw.length < 8) return fail('Use at least 8 characters.');
    if (pw !== confirmEl.value) return fail('The two passwords do not match.');
    url = '/api/v1/setup';
    body = {{password: pw}};
  }}
  go.disabled = true; go.textContent = 'Working…';
  try {{
    var r = await fetch(url, {{method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body)}});
    if (r.ok) {{ location.href = '/'; return; }}
    var d = {{}};
    try {{ d = await r.json(); }} catch (_) {{}}
    fail(d.detail || d.error || ('Refused (HTTP ' + r.status + ').'));
  }} catch (_) {{ fail('Could not reach ROMarr.'); }}
}};
</script></body></html>"""
