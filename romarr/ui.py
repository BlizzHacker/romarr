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

/* ---- the theme -----------------------------------------------------------
   One accent, three modes. Every colour below is a token because a mode has to
   be nothing but a block of overrides -- a literal left anywhere in the sheet
   is a patch that stays dark when the operator asks for light, and that is how
   half-themed apps happen.

   Only three tokens are written from script: --accent, and the two that cannot
   be derived in CSS. A colour the user picked has no guaranteed contrast, and
   deciding whether it needs black or white on top of it, or how far to walk it
   before it is legible as link text, is luminance maths CSS has no way to do.
   Everything else here is derived with color-mix so a new accent costs one
   custom-property write rather than a stylesheet. */
:root{
  color-scheme:dark;
  --bg:#12141a; --surface:#1a1d25; --surface-2:#212734; --surface-3:#2b3240;
  --rail:#0d0f14; --rail-2:#1a1f2a;
  --line:#282e3a; --line-soft:#1f2530;
  --fg:#e8eaf0; --fg-2:#b4bbca; --dim:#89909f;
  --ok:#3ecf6d; --warn:#f0a92c; --bad:#f4645f; --info:#5aa2e8;
  --shadow-1:0 1px 2px rgba(0,0,0,.45);
  --shadow-2:0 2px 6px rgba(0,0,0,.35),0 14px 30px -16px rgba(0,0,0,.8);
  --shadow-3:0 30px 70px -24px rgba(0,0,0,.85);
  --edge:inset 0 1px 0 rgba(255,255,255,.045);
  --art:#0b0d11;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#eef1f6; --surface:#fff; --surface-2:#f5f7fb; --surface-3:#e7ebf2;
  --rail:#fff; --rail-2:#eef1f7;
  --line:#dce2ec; --line-soft:#eaeef5;
  --fg:#161b24; --fg-2:#404859; --dim:#697083;
  --ok:#12a150; --warn:#b8760a; --bad:#d43b37; --info:#2b6fbd;
  --shadow-1:0 1px 2px rgba(16,24,40,.06);
  --shadow-2:0 1px 3px rgba(16,24,40,.09),0 14px 30px -18px rgba(16,24,40,.32);
  --shadow-3:0 34px 70px -26px rgba(16,24,40,.38);
  --edge:inset 0 1px 0 rgba(255,255,255,.9);
  --art:#e3e8f0;
}
/* Easy on the eyes: warm paper, almost no blue, and a deliberately narrow
   contrast range. It is a light mode you can sit in after midnight -- pure
   white at 2am is the thing being avoided, so the surfaces stay dimmer than
   light mode and the ink stays browner than black. */
:root[data-theme="sepia"]{
  color-scheme:light;
  --bg:#e6dbcb; --surface:#f3ebde; --surface-2:#ece2d2; --surface-3:#dfd2bd;
  --rail:#e0d4c0; --rail-2:#d5c6ae;
  --line:#d2c3ab; --line-soft:#ded0ba;
  --fg:#3d362c; --fg-2:#5e5446; --dim:#8a7f6e;
  --ok:#4f8f56; --warn:#a8742a; --bad:#b5544c; --info:#4c7a9e;
  --shadow-1:0 1px 2px rgba(74,58,36,.09);
  --shadow-2:0 1px 3px rgba(74,58,36,.10),0 12px 26px -16px rgba(74,58,36,.30);
  --shadow-3:0 28px 60px -24px rgba(74,58,36,.36);
  --edge:inset 0 1px 0 rgba(255,255,255,.5);
  --art:#d9cbb4;
}

/* Derived once, here, so no rule below ever names a colour twice. Declared
   after the modes on purpose: these read whatever the mode set, lazily. */
:root{
  --accent:#f2a33c; --accent-text:#f2a33c; --accent-ink:#141414;
  --accent-soft:color-mix(in srgb,var(--accent) 15%,transparent);
  --accent-softer:color-mix(in srgb,var(--accent) 8%,transparent);
  --accent-line:color-mix(in srgb,var(--accent) 42%,transparent);
  --accent-hi:color-mix(in srgb,var(--accent) 86%,#fff);
  --ring:0 0 0 3px color-mix(in srgb,var(--accent) 26%,transparent);
  --panel:var(--surface);
  --r-sm:7px; --r-md:11px; --r-lg:15px; --r-pill:999px;
  --t:.15s cubic-bezier(.4,0,.2,1);
}

html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
    "Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
a{color:var(--accent-text);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
::selection{background:var(--accent-soft);color:var(--fg)}
:focus-visible{outline:2px solid var(--accent-text);outline-offset:2px}
*{scrollbar-width:thin;scrollbar-color:var(--surface-3) transparent}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:var(--r-pill);
  border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--dim);background-clip:padding-box}

/* ---- rail ---- */
#rail{position:fixed;left:0;top:0;bottom:0;width:222px;background:var(--rail);
  border-right:1px solid var(--line);z-index:20;
  display:flex;flex-direction:column;overflow:hidden}
/* No gap: the brand is one word split into two nodes so that "arr" can
   carry the accent colour, and a flex gap renders it as "ROM arr". */
#brand{display:flex;align-items:center;gap:0;padding:17px 18px 16px;
  font-size:20px;font-weight:700;letter-spacing:-.025em;
  border-bottom:1px solid var(--line-soft);flex:none;overflow:hidden;
  white-space:nowrap}
#brand span{color:var(--accent-text)}
#navwrap{flex:1;overflow-y:auto;padding:6px 0 10px}
.navgroup{padding:8px 0 2px}
.navhead{padding:6px 20px;font-size:10.5px;font-weight:600;
  text-transform:uppercase;letter-spacing:.1em;color:var(--dim);opacity:.8}
.nav{display:flex;align-items:center;gap:10px;margin:1px 10px;
  padding:8px 11px;color:var(--fg-2);cursor:pointer;font-size:13.5px;
  border-radius:var(--r-sm);position:relative;
  transition:background var(--t),color var(--t)}
.nav:hover{background:var(--rail-2);color:var(--fg)}
.nav.on{background:var(--accent-soft);color:var(--accent-text);font-weight:600}
/* The active marker is a rounded bar rather than *arr's flat 3px border, and
   it lives on a pseudo-element so the row can keep its own radius. */
.nav.on::before{content:"";position:absolute;left:-6px;top:7px;bottom:7px;
  width:3px;border-radius:var(--r-pill);background:var(--accent)}
.nav .ct{margin-left:auto;font-size:10.5px;font-weight:600;
  background:var(--surface-3);color:var(--dim);padding:1px 7px;
  border-radius:var(--r-pill)}
/* A zero is written as "" rather than "0", which left an empty pill sitting
   in the rail claiming to be a number. */
.nav .ct:empty{display:none}
.nav.on .ct{background:var(--accent);color:var(--accent-ink)}

/* ---- the theme control ----
   In the rail rather than under Settings: it is a comfort control, and a
   comfort control you have to navigate to is one you change once and then
   live with. */
#railfoot{flex:none;border-top:1px solid var(--line-soft);padding:12px 12px 14px;
  background:var(--rail)}
.thm-lbl{font-size:10.5px;font-weight:600;text-transform:uppercase;
  letter-spacing:.1em;color:var(--dim);opacity:.8;margin:0 0 8px 2px}
.thm-modes{display:flex;gap:3px;background:var(--surface-2);padding:3px;
  border-radius:var(--r-sm);margin-bottom:10px}
/* Longhand, not the `font` shorthand: `inherit` is not a family name, so
   `font:600 11px/1.3 inherit` is invalid and the browser drops the whole
   declaration -- a button silently keeps the UA's own font instead. */
.thm{flex:1;padding:5px 4px;font-family:inherit;font-size:11px;
  font-weight:600;line-height:1.3;color:var(--dim);
  background:transparent;border:0;border-radius:5px;cursor:pointer;
  transition:background var(--t),color var(--t)}
.thm:hover{color:var(--fg)}
.thm.on{background:var(--surface);color:var(--fg);box-shadow:var(--shadow-1)}
.thm-accents{display:flex;flex-wrap:wrap;gap:6px}
.sw{width:19px;height:19px;padding:0;border-radius:var(--r-pill);
  border:1px solid rgba(128,128,128,.35);background:var(--sw,var(--accent));
  cursor:pointer;position:relative;transition:transform var(--t)}
.sw:hover{transform:scale(1.14)}
.sw.on{box-shadow:0 0 0 2px var(--rail),0 0 0 4px var(--accent-text)}
.sw.pick{display:grid;place-items:center;overflow:hidden;
  background:conic-gradient(#f2635f,#f2a33c,#7bd88f,#2dd4bf,#5aa2f0,#c084fc,#f2635f)}
.sw.pick input{position:absolute;inset:-4px;width:calc(100% + 8px);
  height:calc(100% + 8px);opacity:0;cursor:pointer;border:0;padding:0}

/* ---- main ---- */
#main{margin-left:222px;min-height:100vh}
#top{position:sticky;top:0;display:flex;align-items:center;gap:14px;
  padding:13px 26px;z-index:10;border-bottom:1px solid var(--line);
  background:color-mix(in srgb,var(--bg) 80%,transparent);
  -webkit-backdrop-filter:saturate(1.6) blur(12px);
  backdrop-filter:saturate(1.6) blur(12px)}
#top h1{font-size:17px;font-weight:650;letter-spacing:-.02em;white-space:nowrap}
#top input{flex:1;max-width:420px;padding:8px 13px;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:var(--r-sm);
  outline:none;transition:border-color var(--t),box-shadow var(--t)}
#top input:focus{border-color:var(--accent-line);box-shadow:var(--ring)}
.page{padding:24px 26px 48px;max-width:1600px}

/* ---- plugin catalogue ---------------------------------------------------
   A card grid rather than a table. A plugin is a thing you decide to trust,
   and that decision needs the description and the permissions in view at the
   same time -- a table row makes you read across five columns to assemble
   what one card says at a glance. */
.cat-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  padding:0 0 14px;border-bottom:1px solid var(--line-soft);margin-bottom:16px}
.cat-bar input{flex:1;min-width:220px;padding:9px 13px;background:var(--bg);
  color:var(--fg);border:1px solid var(--line);border-radius:var(--r-sm);
  outline:none;transition:border-color var(--t),box-shadow var(--t)}
.cat-bar input:focus{border-color:var(--accent-line);box-shadow:var(--ring)}
.chip{padding:5px 12px;border-radius:var(--r-pill);border:1px solid var(--line);
  background:var(--bg);color:var(--dim);cursor:pointer;font-size:12px;
  white-space:nowrap;transition:all var(--t)}
.chip:hover{border-color:var(--accent-line);color:var(--fg)}
.chip.on{background:var(--accent);border-color:var(--accent);
  color:var(--accent-ink);font-weight:600}
.chip .n{opacity:.65;margin-left:5px}

/* The scroll the catalogue needs once it is more than a screenful. Bounded
   so the page header and the add-your-own panel stay put while it moves. */
.cat-scroll{max-height:calc(100vh - 340px);min-height:220px;overflow-y:auto;
  padding-right:6px;margin-right:-6px}

.pgrid{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.pcard{background:var(--bg);border:1px solid var(--line);
  border-radius:var(--r-md);padding:15px 16px;display:flex;
  flex-direction:column;gap:9px;
  transition:border-color var(--t),transform var(--t),box-shadow var(--t)}
.pcard:hover{border-color:var(--accent-line);transform:translateY(-2px);
  box-shadow:var(--shadow-2)}
.pcard.installed{border-left:3px solid var(--ok)}
.pcard h4{font-size:14px;font-weight:650;display:flex;align-items:center;
  gap:8px;margin:0;letter-spacing:-.01em}
.pcard .by{color:var(--dim);font-size:11px;font-weight:400}
.pcard .desc{color:var(--fg-2);font-size:12.5px;line-height:1.5;flex:1}
.pcard .meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pcard .foot{display:flex;gap:8px;align-items:center;
  border-top:1px solid var(--line-soft);padding-top:10px;margin-top:2px}
.pcard .net{color:var(--dim);font-size:11px;margin-left:auto;text-align:right}
.dot-ok{width:7px;height:7px;border-radius:50%;background:var(--ok);
  display:inline-block;flex:none}
.dot-off{width:7px;height:7px;border-radius:50%;background:var(--surface-3);
  display:inline-block;flex:none}

.panels{display:grid;gap:14px;grid-template-columns:1fr 1fr;margin-top:16px}
@media(max-width:900px){.panels{grid-template-columns:1fr}}
/* A callout, not a card: bordered on one edge so it reads as an aside even
   when it is the third one on the page. */
.panel-note{background:color-mix(in srgb,var(--info) 11%,transparent);
  border:1px solid color-mix(in srgb,var(--info) 32%,transparent);
  border-left:3px solid var(--info);border-radius:var(--r-sm);
  padding:11px 13px;color:var(--fg-2);font-size:12px;line-height:1.55;
  margin-top:10px}
.panel-warn{background:color-mix(in srgb,var(--bad) 11%,transparent);
  border-color:color-mix(in srgb,var(--bad) 32%,transparent);
  border-left-color:var(--bad)}
/* Used by Friends and Discover for an inline block of prose. It had no rule
   at all, so it rendered as loose text in the middle of a card. */
.panel{background:var(--bg);border:1px solid var(--line-soft);
  border-radius:var(--r-md);padding:12px 14px;color:var(--fg-2);
  font-size:12.5px;line-height:1.6}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}
.field label{font-size:10.5px;font-weight:600;text-transform:uppercase;
  letter-spacing:.07em;color:var(--dim)}
.field input,.field textarea{padding:9px 12px;background:var(--bg);
  color:var(--fg);border:1px solid var(--line);border-radius:var(--r-sm);
  outline:none;font-family:inherit;font-size:13px;line-height:1.5;
  transition:border-color var(--t),box-shadow var(--t)}
.field input:focus,.field textarea:focus{border-color:var(--accent-line);
  box-shadow:var(--ring)}
.field textarea{resize:vertical;min-height:58px}
.field input:disabled{color:var(--dim);background:var(--surface-2);
  cursor:not-allowed}
.empty-cat{text-align:center;padding:48px 20px;color:var(--dim)}
.empty-cat b{display:block;color:var(--fg);margin-bottom:6px;font-size:15px}
.hide{display:none !important}

/* ---- shared ---- */
.btn{padding:8px 16px;background:var(--accent);color:var(--accent-ink);
  font-weight:600;border:0;border-radius:var(--r-sm);cursor:pointer;
  font-size:13px;line-height:1.35;
  transition:filter var(--t),box-shadow var(--t),transform var(--t)}
.btn:hover{filter:brightness(1.07);box-shadow:var(--shadow-1)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.55;cursor:default;filter:none;transform:none}
.btn.ghost{background:var(--surface);color:var(--fg);
  border:1px solid var(--line);box-shadow:var(--edge)}
.btn.ghost:hover{background:var(--surface-2);border-color:var(--accent-line);
  filter:none}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.card{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-lg);padding:18px 20px;margin-bottom:16px;
  box-shadow:var(--shadow-1),var(--edge)}
/* The accent tick. It is the one place the operator's chosen colour appears
   on every page, which is what makes the choice feel like it took. */
.card h3{font-size:14.5px;font-weight:650;letter-spacing:-.01em;
  margin-bottom:13px;color:var(--fg);display:flex;align-items:center;gap:9px}
.card h3::before{content:"";flex:none;width:3px;height:15px;border-radius:2px;
  background:var(--accent)}
.card p.help{color:var(--dim);font-size:12.5px;margin:-7px 0 13px}
.help{color:var(--dim);font-size:12.5px;line-height:1.6}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line-soft);
  vertical-align:top;font-size:13px}
th{color:var(--dim);font-size:10.5px;font-weight:600;text-transform:uppercase;
  letter-spacing:.07em;background:var(--surface-2)}
thead th:first-child{border-top-left-radius:var(--r-sm)}
thead th:last-child{border-top-right-radius:var(--r-sm)}
tbody tr{transition:background var(--t)}
tbody tr:hover td{background:var(--surface-2)}
tbody tr:last-child td{border-bottom:0}
/* Queue and History put a table straight on the page with no card around it,
   so the table has to carry its own chrome there. */
#page>table{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-lg);box-shadow:var(--shadow-1),var(--edge);
  overflow:hidden}
.mini{padding:4px 12px;font-size:12px;border:1px solid var(--line);
  background:var(--surface);color:var(--fg);border-radius:var(--r-sm);
  cursor:pointer;margin-left:6px;transition:all var(--t)}
.mini:hover{border-color:var(--accent-line)}
.mini.accent{background:var(--accent);color:var(--accent-ink);
  border-color:var(--accent)}
.okt{color:var(--ok);font-size:12px}
.pill{font-size:11px;padding:2px 10px;border-radius:var(--r-pill);
  border:1px solid var(--line);background:var(--surface-2);color:var(--fg-2);
  display:inline-block;white-space:nowrap;font-weight:500}
.pill.grabbed{color:var(--info);border-color:color-mix(in srgb,var(--info) 45%,transparent);
  background:color-mix(in srgb,var(--info) 13%,transparent)}
.pill.imported{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent);
  background:color-mix(in srgb,var(--ok) 13%,transparent)}
.pill.failed{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent);
  background:color-mix(in srgb,var(--bad) 13%,transparent)}
.pill.queued{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent);
  background:color-mix(in srgb,var(--warn) 13%,transparent)}
/* An empty page is a state, not an accident. Given a dashed enclosure it
   reads as "there is a place for things here and it is empty" rather than as
   a page that failed to load. */
.empty{color:var(--dim);padding:40px 20px;text-align:center;font-size:13px;
  background:var(--surface-2);border:1px dashed var(--line);
  border-radius:var(--r-lg)}
label{display:block;font-size:12.5px;color:var(--dim);margin-bottom:5px}
/* `input:not([type])` is in here because most of this UI writes a bare
   <input>, and an attribute selector does not match an attribute that is not
   written -- which is how half the fields kept the browser default box. */
input:not([type]),input[type=text],input[type=number],input[type=password],
input[type=search],select,textarea{
  width:100%;padding:9px 12px;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:var(--r-sm);outline:none;
  font-size:13px;font-family:inherit;
  transition:border-color var(--t),box-shadow var(--t)}
input:focus,select:focus,textarea:focus{border-color:var(--accent-line);
  box-shadow:var(--ring)}
.field{margin-bottom:14px;max-width:420px}
.check{display:flex;align-items:center;gap:9px;margin-bottom:11px;
  color:var(--fg);font-size:13px;cursor:pointer}
.check input{width:16px;height:16px;accent-color:var(--accent);flex:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));
  gap:16px}
.tile{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-md);overflow:hidden;box-shadow:var(--shadow-1);
  transition:transform var(--t),box-shadow var(--t),border-color var(--t)}
.tile:hover{transform:translateY(-3px);box-shadow:var(--shadow-2);
  border-color:var(--accent-line)}
/* background-color only: the cover comes in as an inline background-image and
   a shorthand here would be overridden by it, taking the placeholder with it. */
.tile .art{aspect-ratio:3/4;background-color:var(--art);background-position:center;
  background-size:cover;background-repeat:no-repeat;display:block}
.tile .nm{padding:10px 10px 2px;font-size:12.5px;line-height:1.35;
  font-weight:500}
.tile .pf{padding:2px 10px 10px;font-size:11px;color:var(--dim)}
/* A month, seven columns wide. Squares carry a count, so an empty month is
   visibly empty rather than indistinguishable from a month that failed to
   load -- the failure mode the old Calendar page had permanently. */
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
.cal .dow{font-size:10.5px;color:var(--dim);text-align:center;padding:2px 0;
  text-transform:uppercase;letter-spacing:.06em}
.cal button{background:var(--bg);border:1px solid var(--line-soft);
  border-radius:var(--r-sm);padding:6px 4px;min-height:52px;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:2px;color:var(--fg-2);font-size:12px;transition:border-color var(--t)}
.cal button:hover{border-color:var(--accent-line)}
.cal button.on{border-color:var(--accent);background:var(--surface);
  color:var(--fg)}
.cal button.none{opacity:.42;cursor:default}
.cal button.pad{background:none;border:none;cursor:default;min-height:0}
.cal button b{font-size:11px;font-weight:650;color:var(--accent)}
/* Stat blocks, as tiles rather than a row of loose numbers. */
.st{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:10px}
.st div{min-width:0;background:var(--bg);border:1px solid var(--line-soft);
  border-radius:var(--r-md);padding:12px 14px}
.st b{display:block;font-size:22px;font-weight:650;letter-spacing:-.02em;
  line-height:1.2}
.st span{font-size:11.5px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;
  margin-right:7px;background:var(--surface-3)}
.dot.up{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.dot.down{background:var(--bad);box-shadow:0 0 0 3px color-mix(in srgb,var(--bad) 22%,transparent)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px color-mix(in srgb,var(--warn) 22%,transparent)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);
  margin-bottom:18px}
.tab{padding:9px 15px;cursor:pointer;color:var(--dim);font-size:13px;
  font-weight:500;border-bottom:2px solid transparent;margin-bottom:-1px;
  border-radius:var(--r-sm) var(--r-sm) 0 0;transition:all var(--t)}
.tab:hover{color:var(--fg);background:var(--surface-2)}
.tab.on{color:var(--accent-text);border-bottom-color:var(--accent)}
.modal{position:fixed;inset:0;background:color-mix(in srgb,#000 55%,transparent);
  -webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);z-index:60;
  display:flex;align-items:flex-start;justify-content:center;
  padding:60px 20px;overflow-y:auto;animation:fade .16s ease}
.modal .box{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-lg);width:100%;max-width:520px;padding:22px 24px;
  box-shadow:var(--shadow-3);animation:rise .2s cubic-bezier(.2,.8,.3,1)}
.modal h3{font-size:16px;font-weight:650;letter-spacing:-.01em;margin-bottom:4px}
.modal .sub{color:var(--dim);font-size:12.5px;margin-bottom:18px}
.modal .foot{display:flex;gap:8px;margin-top:20px;align-items:center;
  flex-wrap:wrap}
.modal .foot .sp{margin-left:auto}
@keyframes fade{from{opacity:0}}
@keyframes rise{from{opacity:0;transform:translateY(10px) scale(.985)}}
.btn.danger{background:transparent;color:var(--bad);
  border:1px solid color-mix(in srgb,var(--bad) 50%,transparent)}
.btn.danger:hover{background:color-mix(in srgb,var(--bad) 12%,transparent);
  filter:none}
.pick{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.pick button{padding:14px;background:var(--bg);border:1px solid var(--line);
  color:var(--fg);border-radius:var(--r-md);cursor:pointer;text-align:left;
  font-family:inherit;font-size:13px;font-weight:600;line-height:1.4;
  transition:all var(--t)}
.pick button[aria-pressed=true]{border-color:var(--accent);background:var(--accent-softer)}
.pick button:hover{border-color:var(--accent-line);background:var(--surface-2);
  transform:translateY(-1px)}
.pick small{display:block;color:var(--dim);font-size:11px;margin-top:3px;
  font-weight:400}
.rowact{display:flex;gap:6px;justify-content:flex-end}
.rowact button{padding:4px 11px;font-size:12px;background:transparent;
  color:var(--dim);border:1px solid var(--line);border-radius:var(--r-sm);
  cursor:pointer;transition:all var(--t)}
.rowact button:hover{color:var(--accent-text);border-color:var(--accent-line);
  background:var(--accent-softer)}
.testline{font-size:12.5px;margin-top:10px}
.testline.ok{color:var(--ok)}.testline.bad{color:var(--bad)}
.toast{position:fixed;right:22px;bottom:22px;background:var(--surface);
  border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:var(--r-md);padding:13px 17px;max-width:380px;z-index:50;
  font-size:13px;color:var(--fg);box-shadow:var(--shadow-2);
  animation:toastin .22s cubic-bezier(.2,.8,.3,1)}
@keyframes toastin{from{opacity:0;transform:translateY(10px)}}
details summary{list-style:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"\\25B8 ";color:var(--accent-text)}
details[open] summary::before{content:"\\25BE "}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.92em;background:var(--surface-2);padding:1px 5px;
  border-radius:4px;color:var(--fg-2)}

/* Narrow, not collapsed. Every nav entry here is a word -- there is no icon
   set to fall back on -- so the old 56px rail hid the labels and left a
   column of blank clickable boxes. It gives up width instead. */
@media(max-width:820px){
  #rail{width:168px}
  #main{margin-left:168px}
  #brand{font-size:17px;padding:14px 13px 13px}
  .navhead{padding:5px 14px}
  .nav{padding:7px 9px;margin:1px 7px;font-size:13px;gap:6px}
  .nav.on::before{left:-5px}
  #top{padding:11px 14px}
  .page{padding:16px 14px 40px}
  #railfoot{padding:10px 9px 12px}
  .thm{font-size:10px;padding:5px 2px}
  .sw{width:17px;height:17px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(126px,1fr));gap:12px}
  .pgrid{grid-template-columns:1fr}
  .panels{grid-template-columns:1fr}
}
@media(max-width:540px){
  #rail{width:138px}
  #main{margin-left:138px}
  #brand{font-size:15px;padding:13px 10px 12px}
  .nav{padding:7px 8px;margin:1px 5px;font-size:12.5px}
  .thm-modes{flex-direction:column;gap:2px}
  .thm-accents{justify-content:center;gap:5px}
  .page{padding:14px 12px 36px}
  .card{padding:15px 14px}
  th,td{padding:8px 8px}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{transition:none !important;animation:none !important}
}
"""


# The accents offered as one click. Stated once, in Python, so the swatch row
# and the boot script's fallback cannot disagree about what "Amber" is.
ACCENTS = [
    ("#f2a33c", "Amber"),
    ("#f2635f", "Coral"),
    ("#c084fc", "Violet"),
    ("#5aa2f0", "Azure"),
    ("#2dd4bf", "Teal"),
    ("#7bd88f", "Fern"),
]

# Runs in <head>, before anything paints. Reading the stored choice from the
# body script instead would repaint the whole shell in front of the operator on
# every single load -- the white flash every themed app gets wrong once.
#
# The luminance maths is here rather than in CSS because CSS cannot do it. An
# accent the user picked has no guaranteed contrast against anything: navy on
# the dark mode is unreadable as link text, and white-on-yellow is unreadable
# as button text. So two colours are derived from the one that was picked --
# --accent-text, walked along its own lightness until it clears 4.5:1 against
# whatever the mode's background turned out to be, and --accent-ink, whichever
# of near-black or white sits better on the accent itself. The mode background
# is read back out of the cascade rather than restated here, because a copy of
# it in this file is a copy that drifts the first time a mode is retuned.
THEME_BOOT = r"""
(function(){
  var MODES=['dark','light','sepia'], KEY='romarr.theme', AKEY='romarr.accent',
      FALLBACK='ACCENT_FALLBACK', root=document.documentElement;

  function store(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }
  function recall(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }

  function rgb(h){
    h=String(h||'').trim().replace('#','');
    if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    if(!/^[0-9a-fA-F]{6}$/.test(h)) return null;
    var n=parseInt(h,16); return [n>>16&255,n>>8&255,n&255];
  }
  function hex(c){ return '#'+c.map(function(v){
    return ('0'+Math.round(Math.max(0,Math.min(255,v))).toString(16)).slice(-2);
  }).join(''); }
  function lum(c){
    var a=c.map(function(v){ v/=255;
      return v<=.03928 ? v/12.92 : Math.pow((v+.055)/1.055,2.4); });
    return .2126*a[0]+.7152*a[1]+.0722*a[2];
  }
  function ratio(a,b){
    var x=lum(a), y=lum(b);
    return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
  }
  function toHsl(c){
    var r=c[0]/255,g=c[1]/255,b=c[2]/255,
        mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn,h=0,s=0,l=(mx+mn)/2;
    if(d){
      s=d/(1-Math.abs(2*l-1));
      h = mx===r ? ((g-b)/d)%6 : mx===g ? (b-r)/d+2 : (r-g)/d+4;
      h*=60; if(h<0) h+=360;
    }
    return [h,s,l];
  }
  function toRgb(h,s,l){
    var c=(1-Math.abs(2*l-1))*s, x=c*(1-Math.abs((h/60)%2-1)), m=l-c/2,
        t = h<60?[c,x,0]:h<120?[x,c,0]:h<180?[0,c,x]
           :h<240?[0,x,c]:h<300?[x,0,c]:[c,0,x];
    return [(t[0]+m)*255,(t[1]+m)*255,(t[2]+m)*255];
  }
  /* The accent as *text*: same hue, walked toward whichever end of the
     lightness scale is away from the background until it is legible. */
  function legible(accent,bg){
    var c=rgb(accent), b=rgb(bg);
    if(!c||!b) return accent;
    if(ratio(c,b)>=4.5) return accent;
    var hsl=toHsl(c), up=lum(b)<.5, best=accent, score=ratio(c,b);
    for(var i=1;i<=100;i++){
      var l=up?Math.min(1,hsl[2]+i/100):Math.max(0,hsl[2]-i/100),
          t=toRgb(hsl[0],hsl[1],l), r=ratio(t,b);
      if(r>score){ score=r; best=hex(t); }
      if(r>=4.5) return hex(t);
    }
    return best;
  }
  /* And the text drawn *on* the accent: no walking, just whichever of the two
     extremes wins, because a button label has to stay a flat legible block. */
  function ink(accent){
    var c=rgb(accent); if(!c) return '#141414';
    return ratio(c,[20,20,20])>=ratio(c,[255,255,255]) ? '#141414' : '#ffffff';
  }

  function apply(mode,accent){
    root.setAttribute('data-theme',mode);
    root.style.setProperty('--accent',accent);
    // Read back rather than remember: whatever the mode block just set is the
    // background this accent actually has to survive.
    var bg=getComputedStyle(root).getPropertyValue('--bg').trim()||'#12141a';
    root.style.setProperty('--accent-text',legible(accent,bg));
    root.style.setProperty('--accent-ink',ink(accent));
    // Announced rather than pushed: this runs in <head>, so the rail's
    // controls do not exist yet and cannot be told directly. Whoever ends up
    // owning them subscribes, and then no caller of set() can leave the
    // buttons showing a mode the page is not in.
    try{ root.dispatchEvent(new CustomEvent('themechange')); }catch(e){}
  }

  var mode=recall(KEY);
  if(MODES.indexOf(mode)<0){
    // Nothing stored, so the operating system's answer is the best guess
    // available. It is a guess, not a subscription -- once a choice is stored
    // it wins, because somebody who picked light meant light.
    mode = (window.matchMedia
      && matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  }
  var accent=recall(AKEY);
  if(!rgb(accent)) accent=FALLBACK;
  apply(mode,accent);

  window.ROMTHEME={
    modes:MODES,
    mode:function(){ return root.getAttribute('data-theme'); },
    accent:function(){ return root.style.getPropertyValue('--accent').trim()
                              || FALLBACK; },
    set:function(mode,accent){
      var m=MODES.indexOf(mode)>=0?mode:this.mode(),
          a=rgb(accent)?accent:this.accent();
      apply(m,a); store(KEY,m); store(AKEY,a);
    }
  };
})();
""".replace("ACCENT_FALLBACK", ACCENTS[0][0])


def _theme_control_html() -> str:
    """The mode switch and the accent swatches, for the rail's footer."""
    swatches = "".join(
        f'<button class="sw" type="button" data-accent="{value}" '
        f'style="--sw:{value}" title="{name}" '
        f'aria-label="{name} accent"></button>'
        for value, name in ACCENTS
    )
    return (
        '<div id="railfoot">'
        '<div class="thm-lbl">Theme</div>'
        '<div class="thm-modes" role="group" aria-label="Colour mode">'
        '<button class="thm" type="button" data-theme-set="dark" '
        'title="Dark">Dark</button>'
        '<button class="thm" type="button" data-theme-set="light" '
        'title="Light">Light</button>'
        '<button class="thm" type="button" data-theme-set="sepia" '
        'title="Easy on the eyes — warm, low contrast, less blue light">'
        'Easy</button></div>'
        f'<div class="thm-accents">{swatches}'
        '<label class="sw pick" title="Any colour you like">'
        f'<input type="color" id="accent-pick" value="{ACCENTS[0][0]}" '
        'aria-label="Custom accent colour"></label>'
        '</div></div>'
    )

# The nav, in *arr order. `count` names a stat the badge reads.
NAV = [
    ("Library",  [("library", "Games", "games"), ("add", "Add New", None),
                  ("discover", "Discover", None),
                  ("search", "Interactive Search", None)]),
    ("Wanted",   [("missing", "Missing", "missing"), ("lists", "Lists", None),
                  ("calendar", "Calendar", None)]),
    ("Activity", [("queue", "Queue", "queued"), ("history", "History", None),
                  ("blocklist", "Blocklist", None)]),
    ("Hub",      [("hub", "Plugins", None), ("peers", "Friends", None)]),
    ("Settings", [("setup", "Setup Guide", None), ("media", "Media Management", None), ("profiles", "Profiles", None),
                  ("indexers", "Indexers", None), ("clients", "Download Clients", None),
                  ("libraries", "Libraries", None),
                  ("connections", "Connections", None),
                  ("metadata", "Metadata", None), ("general", "General", None)]),
    ("System",   [("status", "Status", None), ("stats", "Stats", None),
                  ("update", "Update", None),
                  ("ecosystem", "Ecosystem", None),
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
// Library counts here run to six figures; grouped, they are readable at a
// glance instead of a digit-counting exercise.
const fmt=n=>Number(n||0).toLocaleString();
const fmtBytes=n=>{let v=Number(n||0),u='B';for(const next of ['KiB','MiB','GiB','TiB']){if(v<1024)break;v/=1024;u=next;}return v.toFixed(v<10&&u!=='B'?1:0)+' '+u;};
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
    discover:'Discover',ecosystem:'The Ecosystem',peers:'Friends',
    queue:'Queue',history:'History',media:'Media Management',profiles:'Profiles',
    indexers:'Indexers',clients:'Download Clients',libraries:'Libraries',
    general:'General',
    status:'System Status',platforms:'Platforms',tasks:'Tasks',logs:'Logs',
    update:'Update',
    blocklist:'Blocklist',connections:'Connections',metadata:'Metadata',
    calendar:'Release Calendar',manualimport:'Manual Import',
    collections:'Collections \u2014 full sets and 1G1R',
    getstarted:'Get Started',setup:'Setup Guide',
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
  let hs=await j('/api/v1/hub/status').catch(()=>({}));
  const p=$('#page');
  p.innerHTML='<div class="empty">Reading the plugin catalogue…</div>';

  // State lives here rather than in the DOM: a filter that has to be read
  // back out of the markup drifts from what is actually displayed.
  let q='', cap='', inst='', actionError='';

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
      +(String(pl.repository||'').startsWith('https://')
        ?'<a href="'+esc(pl.repository)+'" target="_blank" rel="noopener">Instructions and source</a>':'')
      +(pl.key_required?'<div class="help">Requires an account or key. Check the instructions before installing.</div>':'')
      +'<div class="foot">'+action
      +(net?'<span class="net">reaches '+esc(net)+'</span>':'')+'</div></div>';
  };

  const render=d=>{
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
    const acq=hs.acquisition||'';
    const acqNote=hs.available===false
      ?'<div class="panel-note panel-warn"><b>ROM Hub is not available in this install.</b> '
        +'<a href="https://github.com/BlizzHacker/romarr/blob/main/docs/PLUGINS.md" target="_blank" rel="noopener">See the installation guide</a>.</div>'
      :acq==='ready'
        ?'<div class="panel-note" style="border-left:3px solid var(--ok)"><b>Ready for search.</b> Enabled sources: '
          +esc((hs.searchable_plugins||[]).join(', '))+'. Choose Interactive Search to try one.</div>'
        :'<div class="panel-note"><b>Finish connecting a source.</b> '
          +((hs.searchable_plugins||[]).length
            ?'Your sources are enabled. Use Set up ROM Hub downloads above to choose where downloads are saved.'
            :'Install and enable a search source below, then use Set up ROM Hub downloads to choose a download folder.')
          +'</div>';

    const items=d.items||[];
    const facets=(d.facets&&d.facets.capabilities)||[];
    const chips=facets.map(([name,n])=>
      '<span class="chip'+(cap===name?' on':'')+'" data-cap="'+esc(name)+'">'
      +esc(name)+'<span class="n">'+n+'</span></span>').join('');

    p.innerHTML='<div class="card"><h3>Add game sources with ROM Hub</h3>'
      +'<p>ROM Hub brings extra game sources into ROMarr. Pick a source below, install it, and enable it. Some sources need an account or key; their project pages explain what to enter.</p>'
      +'<p class="help">To use a source in Interactive Search, add a ROM Hub download client and choose a writable download folder. You can keep Prowlarr alongside it, or use plugins on their own.</p>'
      +'<div class="row"><button class="btn" id="hub-connect">Set up ROM Hub downloads</button>'
      +'<a class="btn ghost" href="https://github.com/BlizzHacker/rom-hub" target="_blank" rel="noopener">Plugin catalogue and help</a>'
      +'<button class="btn ghost" id="hub-search">Open Interactive Search</button></div></div>'
      +acqNote+sandboxNote+'<div class="card"><h3>Plugins'
      +'<span class="help" style="margin-left:10px">'+items.length+' of '+(d.total||0)+'</span></h3>'
      +'<div class="cat-bar">'
      +'<input id="pq" placeholder="Search plugins by name, author or description…" value="'+esc(q)+'">'
      +'<span class="chip'+(inst==='1'?' on':'')+'" data-inst="1">Installed</span>'
      +'<span class="chip'+(inst==='0'?' on':'')+'" data-inst="0">Available</span>'
      +chips+'</div>'
      +(d.error?'<div class="panel-note panel-warn">'+esc(d.error)+'</div>':'')
      +(actionError?'<div class="panel-note panel-warn">'+esc(actionError)+'</div>':'')
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

    $('#hub-connect').onclick=()=>editItem('downloadclient',{type:'hub'},()=>RENDER.hub());
    $('#hub-search').onclick=()=>go('search');
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
      const slug=b.dataset.slug, action=b.dataset.act;
      b.disabled=true; b.textContent='Working…'; actionError='';
      try{
        const r=await fetch('/api/v1/hub/plugin',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({slug,action})});
        let result={};
        try{result=await r.json();}catch(_){result={};}
        if(!r.ok||result.ok!==true){
          actionError=result.error||result.reason
            ||('Could not '+action+' '+slug+' (HTTP '+r.status+').');
        }
      }catch(e){
        actionError='Could not '+action+' '+slug+': '
          +(e&&e.message?e.message:'ROMarr did not answer.');
      }
      hs=await j('/api/v1/hub/status').catch(()=>hs);
      render(await load());
    });

    $('#ownadd').onclick=async()=>{
      const button=$('#ownadd'), url=$('#ownurl').value.trim();
      if(!url){toast('Paste a plugin repository URL first.');return;}
      button.disabled=true; button.textContent='Installing...'; actionError='';
      try{
        const r=await fetch('/api/v1/hub/source/install',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
        const d=await r.json();
        if(!r.ok||!d.ok)actionError=d.error||d.reason||d.err||d.out||'The plugin could not be installed.';
        else toast('Plugin installed. Check its instructions for any account setup.');
      }catch(e){actionError='Could not reach ROMarr. Please try again.';}
      hs=await j('/api/v1/hub/status').catch(()=>hs);
      render(await load());
      if(actionError)$('#ownurl').value=url;
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

let LIB={platform:'',genre:'',region:'',decade:'',origin:'',source:'',sort:'',
         offset:0,limit:120};
// What each catalogue is called on screen. Spelled out rather than shown as
// the raw key, because "archive" and "flashpoint" mean nothing to somebody
// who did not set the indexers up, and "cloud" has to say plainly that
// ROMarr does not know where those came from.
const SOURCE_LABEL={
  local:'On disk here',
  archive:'Archive.org (streams)',
  flashpoint:'Flashpoint (streams)',
  cloud:'Catalogued, source unknown'};
const num=n=>(n===null||n===undefined)?'—':Number(n).toLocaleString();
RENDER.library=async()=>{
  const p=$('#page');
  if(!p.querySelector('#lib-grid')) p.innerHTML='<div class="empty">Loading library…</div>';
  const q=($('#search').value||'').trim();
  const d=await j(`/api/v1/game?platform=${encodeURIComponent(LIB.platform)}`
    +`&q=${encodeURIComponent(q)}&offset=${LIB.offset}&limit=${LIB.limit}`
    +`&genre=${encodeURIComponent(LIB.genre)}`
    +`&region=${encodeURIComponent(LIB.region)}`
    +`&decade=${encodeURIComponent(LIB.decade)}`
    +`&origin=${encodeURIComponent(LIB.origin)}`
    +`&source=${encodeURIComponent(LIB.source)}`
    +`&sort=${encodeURIComponent(LIB.sort)}`)
    .catch(()=>({items:[],error:'unreachable'}));

  if(d.loading){
    // The first fetch has not landed yet. Say so, rather than showing an
    // empty grid that reads as "your library is empty".
    p.innerHTML=`<div class="card"><h3>Reading the library</h3>
      <p class="help">${esc(d.message||'Fetching from RomM…')}
      ${d.error?` Last attempt: <b>${esc(d.error)}</b>. Retrying with backoff.`:''}</p></div>`;
    return;
  }

  const items=d.items||[];
  const T=d.totals||{};
  // Every platform the library server holds, from the first second --
  // even ones the background walk has not reached. When a platform is only
  // partly cached the chip says so rather than under-reporting silently.
  //
  // The number on a chip is the platform's whole row count, and on its own
  // that was actively misleading: "Browser 94,415" sat beside "Nintendo DS
  // 8,112" looking like the same claim, when not one of those 94,415 is a
  // file on this disk. A chip whose contents are wholly or partly catalogued
  // now shows the split and says which is which on hover.
  const chips=(d.platforms||[]).map(x=>{
    const part=x.cached!==undefined&&x.cached<x.count;
    const cat=x.catalogued||0, disk=x.on_disk||0;
    const mixed=cat>0;
    const tip=[part?`${x.cached} of ${x.count} loaded so far`:'',
               mixed?`${disk} on disk · ${cat} catalogued elsewhere`:'']
              .filter(Boolean).join(' — ');
    return `<button class="btn ${LIB.platform===x.platform?'':'ghost'}" data-plat="${esc(x.platform)}"
      style="padding:4px 10px;font-size:12px"
      title="${esc(tip)}">${esc(x.platform)}
      <span style="opacity:.7">${part?x.cached+'/'+x.count:x.count}</span>
      ${mixed?`<span style="opacity:.55;font-size:11px">(${disk}&nbsp;here)</span>`:''}</button>`;
  }).join('');
  const f=d.facets||{};
  const sel=(key,label,list,fmt)=>{
    if(!list||!list.length) return '';
    return `<select data-lf="${key}" style="padding:4px 8px;font-size:12px">
      <option value="">${label}</option>
      ${list.map(x=>`<option value="${esc(x.value)}"${LIB[key]===x.value?' selected':''}
        >${esc(fmt?fmt(x.value):x.value)} (${x.count})</option>`).join('')}
    </select>`;
  };
  const coverage=f.identified&&d.grand_total
    ? Math.round(f.identified/d.grand_total*100) : 0;
  // The headline. Three numbers where there used to be one, because the one
  // was the other two added together and nothing said so: a shelf of 166,548
  // reads as a collection twice the size of the 72,120 files that are
  // actually here. The middle number is the interesting one -- it is what
  // would have to be fetched or streamed before anybody could play it.
  const srcRows=(f.sources||[]).filter(x=>x.value!=='local');
  const banner=`<div class="card" style="margin-bottom:12px">
    <div class="st">
      <div><b>${num(T.on_disk)}</b><span>On disk here</span></div>
      <div><b>${num(T.catalogued)}</b><span>Catalogued, streams</span></div>
      <div><b>${num(T.total)}</b><span>Total known</span></div>
    </div>
    ${srcRows.length?`<p class="help" style="margin:8px 0 0">
      Catalogued entries by source:
      ${srcRows.map(x=>`<b>${num(x.count)}</b> ${esc(SOURCE_LABEL[x.value]||x.value)}`)
        .join(' · ')}. Source is read from the filename each indexer writes,
      so anything ROMarr cannot place stays “source unknown” rather than
      being filed under a guess.</p>`:''}
    ${T.counted_from==='cached rows'
      ?`<p class="help" style="margin:6px 0 0">Counted from the ${num(d.cached_total)}
        rows read so far — this library server does not report the split
        itself${d.partial?', and the read is still running':''}.</p>`
      :((T.total!==null&&T.total!==undefined&&d.cached_total<T.total)
        ?`<p class="help" style="margin:6px 0 0">${num(d.cached_total)} of those
          are loaded and browsable here${d.partial?' — still reading':''}.</p>`:'')}
  </div>`;
  const head=banner+`<div class="row" style="flex-wrap:wrap;gap:6px;margin-bottom:10px">
      <button class="btn ${LIB.platform?'ghost':''}" data-plat=""
        style="padding:4px 10px;font-size:12px">All
        <span style="opacity:.7">${d.grand_total}</span></button>${chips}</div>
    <div class="row" style="flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:center">
      ${sel('genre','Any genre',f.genres)}
      ${sel('region','Any region',f.regions)}
      ${sel('decade','Any decade',f.decades)}
      ${(f.origins||[]).length>1?sel('origin','On disk & catalogued',f.origins,
        v=>v==='local'?'On disk (plays now)':'Catalogued (needs fetching)'):''}
      ${(f.sources||[]).length>1?sel('source','Any source',f.sources,
        v=>SOURCE_LABEL[v]||v):''}
      <select data-lf="sort" style="padding:4px 8px;font-size:12px">
        <option value=""${LIB.sort?'':' selected'}>Platform, then name</option>
        <option value="name"${LIB.sort==='name'?' selected':''}>Name A–Z</option>
        <option value="rating"${LIB.sort==='rating'?' selected':''}>Top rated</option>
        <option value="year"${LIB.sort==='year'?' selected':''}>Newest first</option>
      </select>
      ${(LIB.genre||LIB.region||LIB.decade||LIB.origin||LIB.source||LIB.sort)
        ?'<button class="btn ghost" id="lf-clear" style="padding:4px 10px;font-size:12px">Clear filters</button>':''}
      ${(f.genres||[]).length?`<span class="help" style="margin:0;font-size:11.5px">
        genre/year known for ${f.identified} of ${d.grand_total} (${coverage}%) —
        run a metadata scan in your library server to raise it</span>`:''}
    </div>
    <p class="help" style="margin-bottom:10px">
      Showing ${d.total?LIB.offset+1:0}–${Math.min(LIB.offset+items.length,d.total)}
      of ${d.total}${LIB.platform?` on ${esc(LIB.platform)}`:''}${q?` matching “${esc(q)}”`:''}
      · ${d.grand_total} cached across ${(d.platforms||[]).length} platforms
      ${d.partial?' · <b>still loading from the library server…</b>':''}
      ${d.error?` · <span style="color:var(--warn)">last refresh: ${esc(d.error)}</span>`:''}</p>`;

  if(!items.length){
    p.innerHTML=head+`<div class="empty">
      ${q||LIB.platform?'No game matches that.':'No games returned by the library.'}</div>`;
    bindLibraryChips(p); return;
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
  const grid=`<div class="grid" id="lib-grid">${items.map((g,i)=>`<div class="tile"
      data-shelf="${i}" style="cursor:pointer" title="Click to set status, rating, notes">
    <div class="art" style="background-image:url('${esc(g.cover||'')}')"></div>
    <div class="nm">${esc(g.name)}</div>
    <div class="pf">${esc(g.platform||'')}</div>${mark(g)}</div>`).join('')}</div>`;
  const more=LIB.offset+items.length<d.total
    ?`<div class="row" style="justify-content:center;margin:16px 0">
       <button class="btn" id="lib-more">Show more
         (${d.total-LIB.offset-items.length} left)</button></div>`:'';
  p.innerHTML=head+grid+more;
  bindLibraryChips(p);
  const moreBtn=p.querySelector('#lib-more');
  if(moreBtn) moreBtn.onclick=()=>{LIB.limit+=240;go('library');};
  document.querySelectorAll('[data-shelf]').forEach(t=>t.onclick=()=>
    shelfEditor(items[Number(t.dataset.shelf)],
      shelf[`${items[Number(t.dataset.shelf)].platform||''}/`
        +`${(items[Number(t.dataset.shelf)].name||'').toLowerCase()}`]||{}));
};

function bindLibraryChips(p){
  p.querySelectorAll('[data-plat]').forEach(b=>b.onclick=()=>{
    LIB.platform=b.dataset.plat; LIB.offset=0; LIB.limit=120;
    go('library');
  });
  p.querySelectorAll('[data-lf]').forEach(s=>s.onchange=()=>{
    LIB[s.dataset.lf]=s.value; LIB.offset=0; LIB.limit=120;
    go('library');
  });
  const clear=p.querySelector('#lf-clear');
  if(clear) clear.onclick=()=>{
    LIB.genre=LIB.region=LIB.decade=LIB.origin=LIB.source=LIB.sort='';
    LIB.offset=0; LIB.limit=120; go('library');
  };
}

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
      <th>Added</th><th>Attempts</th><th>Last error</th><th></th></tr></thead><tbody>
      ${d.items.map(w=>`<tr><td>${esc(w.game)}</td><td>${esc(w.platform)}</td>
        <td>${esc(w.added.slice(0,10))}</td><td>${w.attempts}</td>
        <td style="color:var(--dim)">${esc(w.last_error||'—')}</td>
        <td><button class="mini" data-drop-game="${esc(w.game)}"
          data-drop-platform="${esc(w.platform)}"
          title="Stop looking for this. It stays out of Wanted until you ask for it again."
          >Remove</button></td></tr>`).join('')}
      </tbody></table>`:'<div class="empty">Nothing missing.</div>'}`;
  const b=$('#w-all'); if(b) b.onclick=async()=>{
    b.disabled=true; b.textContent='Searching…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:'MissingGameSearch'})});
    toast(`Searched ${r.searched??0}, grabbed ${r.grabbed??0}`);
    go('missing'); refreshCounts();
  };
  // Dropping a request, which Wanted could not do at all: the list was only
  // ever emptied by an import, so a typo went on being searched for forever.
  $('#page').querySelectorAll('button[data-drop-game]').forEach(x=>x.onclick=async()=>{
    const game=x.dataset.dropGame, platform=x.dataset.dropPlatform;
    if(!confirm(`Stop looking for "${game}" on ${platform}?`)) return;
    x.disabled=true;
    const r=await fetch('/api/v1/wanted/missing?game='+encodeURIComponent(game)
      +'&platform='+encodeURIComponent(platform),{method:'DELETE'});
    if(r.ok){ toast(`Removed ${game}`); go('missing'); refreshCounts(); }
    else { x.disabled=false; toast('Could not remove that request'); }
  });
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
        <td>${l.added_count||0}${l.unmatched_count
          ?` <span class="pill" title="Titles with no ROM platform — modern store games. Open Edit to see them.">${l.unmatched_count} kept aside</span>`:''}
          ${l.last_sync?`<div style="font-size:11px;color:${l.last_sync.error?'var(--warn)':'var(--dim)'}">
            ${l.last_sync.error?esc(l.last_sync.error.slice(0,90))
              :`fetched ${l.last_sync.fetched} at ${esc((l.last_sync.at||'').replace('T',' ').slice(0,16))}`}</div>`:''}</td>
        <td><span class="dot ${l.enable!==false?'up':'down'}"></span></td>
        <td style="text-align:right"><div class="rowact">
          <button data-ledit="${i}">Edit</button></div></td></tr>`).join('')}
      </tbody></table>`
      :'<div class="empty">No lists yet. Paste one, or connect an account, and let the clock do the asking.</div>'}
    <div class="card" style="margin-top:14px"><h3>Connect an account</h3>
      <p class="help">One click each. You are already signed in to these in
        your browser &mdash; that is what makes it one click.</p>
      <div class="row" style="flex-wrap:wrap;gap:10px;margin:10px 0">
        <a class="btn" href="/api/v1/connect/steam"
           style="text-decoration:none">Sign in through Steam</a>
        <button class="btn ghost" data-token="gog">Connect GOG</button>
        <button class="btn ghost" data-token="epic">Connect Epic</button>
        <button class="btn ghost" data-token="ea">Connect EA</button>
        <button class="btn ghost" data-token="battlenet">Connect Battle.net</button>
        <button class="btn ghost" data-token="psn">Connect PlayStation</button>
        <button class="btn ghost" data-token="xbox">Connect Xbox</button>
        <button class="btn ghost" data-token="itchio">Connect itch.io</button>
        <button class="btn ghost" data-token="humble">Connect Humble</button>
        <button class="btn ghost" data-paste="amazon">Amazon / Prime</button>
        <button class="btn ghost" data-paste="ubisoft">Ubisoft</button>
      </div>
      <p class="help"><b>Steam</b> is a true one-click: Steam's own
        "Sign in through Steam", no key, no app registration &mdash; it comes
        straight back with your library connected. <b>Epic, EA and
        Battle.net</b> pull your <i>owned</i> library through the same web
        APIs Playnite uses &mdash; none of them issues an application key,
        so each opens the page your signed-in browser already has and takes
        one paste. <b>GOG</b> needs only your public profile name.</p>
      <div id="l-token"></div>
      <h3 style="margin-top:18px">Games installed on your gaming PC</h3>
      <p class="help">A second route, useful alongside the above: every
        launcher writes what it installed to disk, so this needs no
        credential of any kind &mdash; and it catches anything a store's
        API misses. Run it <b>on the PC you play on</b>:</p>
      <pre style="font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);
        background:var(--bg);border:1px solid var(--line);border-radius:6px;
        padding:10px;white-space:pre-wrap">python scripts/connect_launchers.py --url ${location.origin} --key &lt;your API key&gt;</pre>
      <div class="row" style="margin:8px 0">
        <button class="btn ghost" id="l-scan">Scan the ROMarr server</button>
        <span class="help" style="margin:0">Only useful if ROMarr runs on
          the machine you play on &mdash; it scans wherever ROMarr itself
          lives, which is usually a server with no launchers.</span></div>
      <div id="l-noapi" style="color:var(--dim);font-size:12.5px"></div></div>`;

  document.querySelectorAll('[data-paste]').forEach(b=>b.onclick=async()=>{
    const s=await j('/api/v1/connect/sources').catch(()=>({paste_sources:{}}));
    const spec=(s.paste_sources||{})[b.dataset.paste];
    if(!spec){toast('Unknown store');return;}
    window.open(spec.open,'_blank','noopener');
    toast(spec.how);
    editList({type:'paste',name:spec.label+' library'});
  });
  document.querySelectorAll('[data-token]').forEach(b=>b.onclick=async()=>{
    const s=await j('/api/v1/connect/sources').catch(()=>({token_sources:{}}));
    const spec=(s.token_sources||{})[b.dataset.token];
    if(!spec){toast('Unknown store');return;}
    window.open(spec.open,'_blank','noopener');
    // A modal, not an inline panel: window.open moves the person to another
    // tab, and when they come back a box further down the page is a box
    // they never see. This one is in front of them and focused.
    const m=document.createElement('div');
    m.className='modal';
    m.innerHTML=`<div class="box">
      <h3>Connect ${esc(spec.label)}</h3>
      <div class="sub">A tab just opened. ${esc(spec.how)}</div>
      <div class="field"><label>Paste it here</label>
        <textarea id="tok-v" rows="4" autocomplete="off" spellcheck="false"
          placeholder='paste the whole thing — {"npsso":"..."} is fine'
          style="width:100%;resize:vertical;font:12px/1.5 ui-monospace,
          Menlo,monospace"></textarea>
        <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
          Paste the entire page if you like — ROMarr pulls the value out.</div>
      </div>
      <div id="testline"></div>
      <div class="foot">
        <button class="btn ghost sp" data-close>Cancel</button>
        <button class="btn" id="tok-save">Connect</button>
      </div></div>`;
    document.body.append(m);
    m.onclick=e=>{ if(e.target===m||e.target.dataset.close!==undefined) closeModal(); };
    setTimeout(()=>m.querySelector('#tok-v')?.focus(),50);
    $('#tok-save').onclick=async()=>{
      const value=$('#tok-v').value.trim();
      if(!value){toast('Paste the value first');return;}
      const body={name:spec.label+' library',type:b.dataset.token,
                  platform:'',enable:true};
      body[spec.field]=value;
      const line=(ok,msg)=>{
        const el=m.querySelector('#testline');
        el.className='testline '+(ok?'ok':'bad'); el.textContent=msg;
      };
      line(true,'Connecting…');
      const saved=await j('/api/v1/importlist',{method:'POST',
        headers:{'content-type':'application/json'},body:JSON.stringify(body)});
      if(saved.error){line(false,saved.error);return;}
      const r=await j('/api/v1/command',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({name:'ListSync'})});
      // The failure reason belongs here, in front of the person who just
      // pasted, not in a toast that vanishes.
      const failed=(r.failures||[]).find(f=>f.id===saved.id);
      if(failed){ line(false,failed.reason); return; }
      closeModal(); toast(r.message||'Connected'); go('lists');
    };
  });
  j('/api/v1/importlist/schema').then(s=>{
    const el=$('#l-noapi'); if(!el) return;   // page may have changed
    const rows=Object.entries(s.no_api||{});
    el.innerHTML=rows.map(([store,why])=>
      `<p style="margin:6px 0"><b>${esc(store)}</b> — ${esc(why)}</p>`).join('');
  }).catch(()=>{});
  const scanBtn=$('#l-scan');
  if(scanBtn) scanBtn.onclick=async()=>{
    scanBtn.disabled=true; scanBtn.textContent='Scanning…';
    const r=await j('/api/v1/launchers/connect',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:'Local launchers'})}).catch(()=>({error:'scan failed'}));
    toast(r.error||r.message||'Done');
    if(r.ok) go('lists'); else { scanBtn.disabled=false; scanBtn.textContent='Scan this machine'; }
  };
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
    ${(item.unmatched||[]).length?`<details style="margin-top:10px">
      <summary class="help" style="margin:0;cursor:pointer">
        ${item.unmatched.length} title(s) kept aside — no ROM platform.
        These are your modern store games; ROMarr acquires ROMs, so they
        wait here rather than pretending to be cartridges.</summary>
      <div style="color:var(--dim);font-size:12px;max-height:180px;
        overflow:auto;margin-top:6px">${item.unmatched.map(esc).join('<br>')}
      </div></details>`:''}
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

let DISC={mode:'library',shelf:'top-rated',genre:''};
RENDER.discover=async()=>{
  // Two sources, and the local one is the default because it works with
  // nothing configured: your own library already carries genres, ratings
  // and years. The storefront shelves need a metadata API key.
  const mine=DISC.mode==='library';
  const libTabs=[['top-rated','Top rated'],['recent','Newest'],
                 ['recently-added','Just added'],['hidden-gems','Hidden gems'],
                 ['multiplayer','Multiplayer'],['anniversary','On this day'],
                 ['by-genre','Genre'],['by-franchise','Franchise'],
                 ['by-company','Company']];
  const webTabs=[['popular','Popular'],['new','New releases'],['upcoming','Upcoming']];
  const tabs=mine?libTabs:webTabs;
  if(mine&&!libTabs.some(t=>t[0]===DISC.shelf)) DISC.shelf='top-rated';
  if(!mine&&!webTabs.some(t=>t[0]===DISC.shelf)) DISC.shelf='popular';
  $('#page').innerHTML=`<div class="row" style="margin-bottom:10px;gap:6px;flex-wrap:wrap">
      <button class="btn ${mine?'':'ghost'}" data-mode="library">My library</button>
      <button class="btn ${mine?'ghost':''}" data-mode="web">New releases (web)</button>
      <span style="flex:1"></span>
      ${tabs.map(([k,l])=>`<button class="btn ${k===DISC.shelf?'':'ghost'}"
        data-shelf="${k}" style="padding:4px 10px;font-size:12px">${l}</button>`).join('')}
    </div>
    <div id="d-genres" style="margin-bottom:10px"></div>
    <div id="d-out"><div class="empty">Browsing…</div></div>`;
  document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{
    DISC.mode=b.dataset.mode; go('discover');});
  document.querySelectorAll('[data-shelf]').forEach(b=>b.onclick=()=>{
    DISC.shelf=b.dataset.shelf; go('discover');});

  if(mine){
    const d=await j(`/api/v1/discover/library?shelf=${encodeURIComponent(DISC.shelf)}`
      +`&value=${encodeURIComponent(DISC.genre)}&limit=60`)
      .catch(()=>({items:[]}));
    // The chip row belongs to the shelf being browsed — genres while you are
    // browsing franchises is a row of buttons that do nothing.
    const facet={'by-genre':['genre','a metadata scan'],
                 'by-franchise':['franchise','a metadata scan'],
                 'by-company':['company','a metadata scan']}[DISC.shelf];
    if(facet){
      $('#d-genres').innerHTML=(d.facet||[]).map(g=>
        `<button class="btn ${DISC.genre===g?'':'ghost'}" data-dgenre="${esc(g)}"
          style="padding:3px 9px;font-size:12px;margin:0 4px 4px 0">${esc(g)}</button>`).join('')
        ||`<span class="help" style="margin:0">No ${facet[0]} known yet — your
            library server fills these in when it identifies a game.</span>`;
      document.querySelectorAll('[data-dgenre]').forEach(b=>b.onclick=()=>{
        DISC.genre=b.dataset.dgenre; go('discover');});
    }
    const items=d.items||[];
    if(!items.length){
      $('#d-out').innerHTML=`<div class="empty">${esc(d.error
        ||(facet&&!DISC.genre?`Pick a ${facet[0]} above.`
           :'Nothing on this shelf yet.'))}</div>`;
      return;
    }
    // Whatever the shelf is sorted on is what the subtitle leads with, so a
    // "Just added" grid is not a wall of tiles with no dates on them.
    const sub=g=>DISC.shelf==='recently-added'?(g.added||'')
      :DISC.shelf==='anniversary'?(g.released||'')
      :(g.year?String(g.year):'');
    $('#d-out').innerHTML=`<p class="help">${fmt(d.total)} game(s) on this
        shelf, from your own library's metadata — no API key involved.</p>
      <div class="grid">${items.map(g=>`<div class="tile">
        <div class="art" style="background-image:url('${esc(g.cover_url||'')}')"></div>
        <div class="nm">${esc(g.title)}</div>
        <div class="pf">${esc(g.platform||'')}${sub(g)?' · '+esc(sub(g)):''}${
          g.rating?' · ★'+g.rating:''}${
          g.players&&g.players!=='1'?' · '+esc(g.players)+'p':''}</div>
        <div class="pf" style="padding-top:0;opacity:.75">${esc(
          (g.franchises||[]).concat(g.genres||[]).slice(0,2).join(' · '))}</div>
        </div>`).join('')}</div>`;
    return;
  }

  const d=await j('/api/v1/discover?shelf='+DISC.shelf).catch(()=>({items:[]}));
  const shelf=DISC.shelf;
  if(!d.items||!d.items.length){
    // Whatever the server says is missing, verbatim. It knows which
    // providers are configured and which credential is blank; a fixed line
    // here naming one provider is how somebody with a working IGDB setup
    // gets told to go and register for RAWG.
    $('#d-out').innerHTML=`<div class="empty">${esc(d.error||'Nothing to show.')}</div>`;
    return;
  }
  const src=d.provider_label||'a metadata provider';
  const requestable=g=>(g.platforms||[]).filter(p=>
    PLATFORMS.some(x=>x.name.toLowerCase()===String(p).toLowerCase()
      ||x.slug===String(p).toLowerCase()));
  // Every tile carries where it came from. These sit one button away from
  // the library grid, which looks identical, and a shelf that lets a
  // catalogue entry pass for a game you own is worse than an empty page.
  $('#d-out').innerHTML=`<p class="help">${fmt(d.items.length)} title(s) from
      ${esc(src)}'s catalogue — not rows in your library. Request adds one to
      Wanted.</p>
    <div class="grid">${d.items.map((g,i)=>{
    const plats=requestable(g);
    return `<div class="tile">
      <div class="art" style="background-image:url('${esc(g.cover_url||'')}')"></div>
      <div class="nm">${esc(g.title)}</div>
      <div class="pf">${esc(g.released||'')}${g.rating?' · ★'+g.rating.toFixed(1):''}</div>
      <div class="pf" style="padding-top:0;opacity:.7">via ${esc(g.source||src)}</div>
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

RENDER.peers=async()=>{
  const d=await j('/api/v1/peer').catch(()=>({peers:[]}));
  const list=d.peers||[];
  const hx=await j('/api/v1/hashes').catch(()=>({count:0}));
  const scopeLabel={none:'Nothing',platforms:'Named platforms only',
    verified:'Verified dumps only',all:'Everything'};
  const accessLabel={catalogue:'See titles only',stream:'Play here',
    fetch:'Download from me'};
  $('#page').innerHTML=`<div class="card"><h3>Friends</h3>
      <p class="help">Share a library with someone running their own server —
        RomM, Gaseous, Retrom, it does not matter, because ROMarr speaks all
        of them. Peering is by invitation and confirmed on both sides: there
        is no directory, and nobody finds you.
        <b>Peering shares nothing by itself</b> — you choose what each friend
        sees, and separately what they may do with it.</p>
      <div class="row" style="flex-wrap:wrap;gap:8px">
        <button class="btn" id="p-invite">Invite a friend</button>
        <button class="btn ghost" id="p-redeem">I have an invitation</button>
        <button class="btn ghost" id="p-romm">Add a RomM server</button>
      </div>
      <p class="help" style="margin-top:10px">Your friend does not have to run
        ROMarr. If they run <b>RomM</b>, ask them for an account on it and use
        <b>Add a RomM server</b> — RomM already publishes a hash for every ROM,
        so browsing and netplay matching both work with nothing installed on
        their side. The trade is honest: an account on their RomM grants
        whatever their RomM grants it, and no setting here can narrow that.</p>
      <div id="p-out" style="margin-top:12px"></div></div>
    <div class="card"><h3>What netplay can prove</h3>
      <p class="help">Netplay is settled on the ROM's SHA1, never its title —
        “Super Mario Kart” is four different ROMs, and two of them desync
        within seconds in a way that reads as lag. ROMarr can only match games
        whose hash it knows.</p>
      ${hx.count
        ? `<div class="panel"><b>${hx.count.toLocaleString()}</b> dump(s) can be
             matched, across ${Object.keys(hx.platforms||{}).length} platform(s).</div>`
        : `<div class="panel panel-warn">No hashes yet, so every netplay offer
             will come back <i>missing</i>. ROMarr reads these from your library
             server automatically — it already stores a hash for every ROM it
             has scanned.</div>`}
      <div id="p-seedlive"></div>
      <div class="row" style="gap:8px;margin-top:10px">
        <button class="btn ghost" id="p-seed">Read them again now</button>
      </div>
      <div id="p-seedout"></div></div>
    ${list.length?`<div class="card"><h3>Your friends</h3>
      <table><thead><tr><th>Name</th><th>They see</th><th>They may</th>
        <th>State</th><th></th></tr></thead><tbody>
      ${list.map((p,i)=>`<tr><td><b>${esc(p.name)}</b>
        ${p.kind==='romm'?' <span class="pill">RomM</span>':''}
        <div style="color:var(--dim);font-size:11.5px">${esc(p.url||'')}</div></td>
        <td>${esc(scopeLabel[p.scope]||p.scope)}${
          p.scope==='platforms'&&(p.platforms||[]).length
            ?`<div style="color:var(--dim);font-size:11.5px">${esc(p.platforms.join(', '))}</div>`:''}</td>
        <td>${esc(accessLabel[p.access]||p.access)}${
          p.delegate_users?' <span class="pill">their users too</span>':''}</td>
        <td>${p.confirmed?'<span class="pill imported">confirmed</span>'
          :'<span class="pill failed">awaiting your confirmation</span>'}</td>
        <td style="text-align:right"><div class="rowact">
          ${p.confirmed?`<button data-pbrowse="${esc(p.peer_id)}"${
             p.url?'':' disabled title="Their invitation carried no address, '
                     +'so there is nowhere to call. Ask them to set their '
                     +'public URL and send a fresh invitation."'}
             >Browse library</button>
            <button data-pedit="${i}">Sharing</button>`
            :`<button data-pconfirm="${esc(p.peer_id)}">Confirm</button>`}
          <button data-prevoke="${esc(p.peer_id)}">Remove</button>
        </div></td></tr>`).join('')}</tbody></table>
      <div id="p-shelf"></div></div>`
      :'<div class="empty">No friends yet. Invite someone, or paste an invitation they sent you.</div>'}`;

  //: Two halves, shown as two halves. The link is safe to paste anywhere a
  //: link goes because it authorises nothing; the code is the credential and
  //: is never in it. Presenting them side by side with the reason written
  //: down is the only thing that stops somebody pasting both into the same
  //: message and wondering why we bothered.
  $('#p-invite').onclick=async()=>{
    const r=await j('/api/v1/peer/invite',{method:'POST',
      headers:{'content-type':'application/json'},body:'{}'});
    $('#p-out').innerHTML=`<div class="card" style="margin:0">
      <h3>Send your friend two things</h3>
      <p class="help">${esc(r.note||'')}</p>
      ${r.warning?`<div class="panel panel-warn">${esc(r.warning)}</div>`:''}
      ${r.link?`<div class="field"><label>1. The link — send it anywhere</label>
        <input type="text" id="p-link" readonly value="${esc(r.link)}"
          style="width:100%;font:12px/1.5 ui-monospace,Menlo,monospace">
        <p class="help" style="margin-top:6px">Carries no secret. Chat, mail,
          a preview card in someone's group thread — none of it matters,
          because on its own this opens nothing.</p></div>`:''}
      <div class="field"><label>2. The claim code — send it separately</label>
        <div id="p-code" style="font:700 26px/1.2 ui-monospace,Menlo,monospace;
          letter-spacing:.12em;padding:12px 0">${esc(r.code||'')}</div>
        <p class="help">This one <b>is</b> the secret. Say it out loud, text
          it, use a different app — anything that is not the same message as
          the link. It works once and expires in
          <b id="p-codeleft">${Math.max(0,Math.round((r.code_expires_in||0)/60))} min</b>.</p></div>
      <div class="row" style="gap:8px">
        <button class="btn ghost" id="p-copy">Copy the link</button>
        <button class="btn ghost" id="p-copycode">Copy the code</button></div>
      <details style="margin-top:14px"><summary class="help"
        style="cursor:pointer">Your friend's ROMarr is older than invitation
        links</summary>
        <p class="help">The one-piece form. It contains the long secret, so
          send it the way you would send a password — and prefer the link and
          code above, which exist because this was easy to mishandle.</p>
        <textarea rows="4" readonly style="width:100%;font:12px/1.5
          ui-monospace,Menlo,monospace">${esc(JSON.stringify(r.invite||{}))}</textarea>
      </details></div>`;
    const copy=async(text,said)=>{
      try{await navigator.clipboard.writeText(text); toast(said);}
      catch{toast('Select it and copy it');}
    };
    $('#p-copy').onclick=()=>copy(r.link||'','Link copied');
    $('#p-copycode').onclick=()=>copy(r.code||'','Code copied');
    //: Counting down rather than showing a timestamp: the operator is deciding
    //: right now whether to resend, and "4 min" answers that where "expires at
    //: 19:42" makes them do arithmetic.
    let left=r.code_expires_in||0;
    const tick=setInterval(()=>{
      const el=$('#p-codeleft');
      if(!el){ clearInterval(tick); return; }
      left-=10;
      if(left<=0){
        clearInterval(tick);
        el.textContent='— expired';
        const code=$('#p-code');
        if(code) code.style.opacity='.35';
      } else el.textContent=Math.max(1,Math.round(left/60))+' min';
    },10000);
  };

  //: Prefilled from the /link landing page, which stashes the invitation in
  //: sessionStorage rather than the URL. Read once and cleared: an invitation
  //: sitting in a tab's storage after it has been used is a thing to explain
  //: later rather than a feature.
  const claimForm=(pre)=>{
    $('#p-out').innerHTML=`<div class="card" style="margin:0">
      <h3>Redeem an invitation</h3>
      <div class="field"><label>The link your friend sent</label>
        <input type="text" id="p-link-in" value="${esc(pre&&pre.link||'')}"
          placeholder="https://their-server.example/link#i=…"
          style="width:100%;font:12px/1.5 ui-monospace,Menlo,monospace"></div>
      <div class="field"><label>The claim code they sent separately</label>
        <input type="text" id="p-code-in" autocomplete="off" maxlength="12"
          placeholder="K7RM-9TFQ" style="max-width:220px;
          font:700 18px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.1em">
        <p class="help" style="margin-top:6px">Eight characters. Case, spaces
          and the dash do not matter.</p></div>
      <div id="p-claim-target"></div>
      <button class="btn" id="p-claim-go">Become friends</button>
      <details style="margin-top:14px"><summary class="help"
        style="cursor:pointer">They sent one long block of JSON instead</summary>
        <p class="help">An older ROMarr, or an invitation minted before this
          version. Paste the whole block.</p>
        <textarea id="p-blob" rows="4" style="width:100%;font:12px/1.5
          ui-monospace,Menlo,monospace"
          placeholder='{"peer_id":"...","secret":"..."}'></textarea>
        <button class="btn ghost" id="p-go" style="margin-top:8px">Redeem the
          pasted invitation</button></details></div>`;
    if(pre&&pre.link) $('#p-code-in').focus();
    //: The address is shown before anything is sent to it. A link is a thing
    //: somebody else wrote, and the one fact worth reading off it is whose
    //: server it points at.
    const showTarget=()=>{
      const el=$('#p-claim-target');
      let host='';
      try{ host=new URL($('#p-link-in').value.trim()).origin; }catch{}
      const frag=($('#p-link-in').value.match(/[#&]u=([^&]*)/)||[])[1];
      if(frag){ try{ host=new URL(decodeURIComponent(frag)).origin; }catch{} }
      el.innerHTML=host?`<div class="panel" style="margin-bottom:12px">ROMarr
        will send this code to <b>${esc(host)}</b>.</div>`:'';
    };
    $('#p-link-in').oninput=showTarget; showTarget();
    $('#p-claim-go').onclick=async()=>{
      const b=$('#p-claim-go');
      b.disabled=true; b.textContent='Talking to their server…';
      const r=await j('/api/v1/peer/claim',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({link:$('#p-link-in').value.trim(),
                             code:$('#p-code-in').value.trim()})});
      b.disabled=false; b.textContent='Become friends';
      if(!r.ok){ toast(r.error||'That invitation was refused'); return; }
      if(r.warning) toast(r.warning);
      toast(r.detail||'Friend added — they confirm on their side');
      go('peers');
    };
    $('#p-go').onclick=async()=>{
      let blob;
      try{ blob=JSON.parse($('#p-blob').value); }
      catch{ toast('That is not a valid invitation'); return; }
      const r=await j('/api/v1/peer/redeem',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({invite:blob})});
      if(r.error){toast(r.error);return;}
      toast('Friend added — they confirm on their side');
      go('peers');
    };
  };
  $('#p-redeem').onclick=()=>claimForm(null);

  let waiting=null;
  try{ waiting=JSON.parse(sessionStorage.getItem('romarr-invite')||'null'); }
  catch{}
  if(waiting&&waiting.peer_id&&waiting.url){
    try{ sessionStorage.removeItem('romarr-invite'); }catch{}
    claimForm({link:waiting.url.replace(/\/+$/,'')+'/link#i='
      +encodeURIComponent(waiting.peer_id)
      +'&u='+encodeURIComponent(waiting.url)});
    $('#p-out').scrollIntoView({block:'nearest'});
  }

  document.querySelectorAll('[data-pconfirm]').forEach(b=>b.onclick=async()=>{
    await j('/api/v1/peer/confirm',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({peer_id:b.dataset.pconfirm})});
    toast('Confirmed'); go('peers');
  });
  document.querySelectorAll('[data-prevoke]').forEach(b=>b.onclick=async()=>{
    if(!confirm('Remove this friend? They lose access immediately and do '
      +'not have to agree.')) return;
    await fetch('/api/v1/peer/'+b.dataset.prevoke,{method:'DELETE'});
    toast('Removed'); go('peers');
  });
  document.querySelectorAll('[data-pedit]').forEach(b=>b.onclick=()=>
    sharingEditor(list[Number(b.dataset.pedit)]));
  document.querySelectorAll('[data-pbrowse]').forEach(b=>b.onclick=()=>
    friendShelf(b.dataset.pbrowse));

  //: Poll while the background read is running, so the page shows it moving
  //: rather than looking stuck. Stops itself when the page changes.
  const seedPoll=async()=>{
    if(location.hash.replace('#','')!=='peers') return;
    const s=await j('/api/v1/hashes').catch(()=>null);
    if(!s) return;
    const st=s.seed||{};
    const live=$('#p-seedlive');
    if(!live) return;
    if(st.running){
      live.innerHTML=`<div class="panel" style="margin-top:10px">
        Reading your library — <b>${(st.seen||0).toLocaleString()}</b> entries
        checked, <b>${(st.added||0).toLocaleString()}</b> with a hash so far.
        You can leave this page.</div>`;
      setTimeout(seedPoll,2000);
    } else if(st.status==='done'&&st.seen){
      live.innerHTML=`<div class="panel" style="margin-top:10px">${
        esc(st.detail||'')}</div>`;
    } else if(st.status==='failed'){
      live.innerHTML=`<div class="panel panel-warn" style="margin-top:10px">${
        esc(st.error||'The library read failed.')}</div>`;
    }
  };
  seedPoll();

  $('#p-seed').onclick=async()=>{
    const b=$('#p-seed');
    b.disabled=true; b.textContent='Starting…';
    const r=await j('/api/v1/hashes/seed',{method:'POST',
      headers:{'content-type':'application/json'},body:'{}'});
    b.disabled=false; b.textContent='Read them again now';
    toast(r.detail||r.error||'');
    seedPoll();
  };

  $('#p-romm').onclick=()=>{
    const m=document.createElement('div');
    m.className='modal';
    m.innerHTML=`<div class="box">
      <h3>Add a friend who runs RomM</h3>
      <div class="sub">They do not need ROMarr, and nothing has to be
        installed on their server. Ask them to make you an account on their
        RomM — read-only is enough.</div>
      <div class="field"><label>Their RomM address</label>
        <input type="text" data-f="url" placeholder="https://romm.their-server.com"></div>
      <div class="field"><label>Username they gave you</label>
        <input type="text" data-f="username" autocomplete="off"></div>
      <div class="field"><label>Password</label>
        <input type="password" data-f="password" autocomplete="off"></div>
      <div class="field"><label>Call them (optional)</label>
        <input type="text" data-f="name" placeholder="Dave's RomM"></div>
      <div class="panel">ROMarr reads their library through RomM's own API,
        including the SHA1 RomM stores for every ROM — which is what lets
        netplay agree on the bytes with a server that has never heard of this
        protocol.</div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">
        <button class="btn ghost" data-close="1">Cancel</button>
        <button class="btn" id="rm-go">Connect</button></div></div>`;
    document.body.appendChild(m);
    const close=()=>m.remove();
    m.querySelector('[data-close]').onclick=close;
    m.onclick=e=>{ if(e.target===m) close(); };
    m.querySelector('#rm-go').onclick=async()=>{
      const val=f=>m.querySelector(`[data-f="${f}"]`).value.trim();
      const btn=m.querySelector('#rm-go');
      btn.disabled=true; btn.textContent='Connecting…';
      const r=await j('/api/v1/peer/romm',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({url:val('url'),username:val('username'),
          password:val('password'),name:val('name')})});
      btn.disabled=false; btn.textContent='Connect';
      if(r.error){ toast(r.error); return; }
      close(); toast(r.detail||'Connected'); go('peers');
    };
  };
};

//: Browsing a friend's library. Their shelf is fetched once and filtered
//: here, so typing in the search box does not bill somebody else's server
//: for every keystroke.
const FSHELF={peer:'',q:'',platform:'',offset:0};

async function friendShelf(peerId, opts){
  Object.assign(FSHELF, {peer:peerId}, opts||{});
  const out=$('#p-shelf');
  if(!out) return;
  out.innerHTML='<div class="empty">Asking your friend’s server…</div>';
  const p=new URLSearchParams({peer_id:FSHELF.peer, q:FSHELF.q,
    platform:FSHELF.platform, offset:FSHELF.offset, limit:100});
  const r=await j('/api/v1/friends/shelf?'+p.toString()).catch(()=>({}));
  if(!r.ok){
    out.innerHTML=`<div class="panel panel-warn" style="margin-top:14px">${
      esc(r.error||'Could not reach that friend.')}</div>`;
    return;
  }
  const rows=r.items||[];
  const pages=Math.ceil((r.total||0)/100);
  const page=Math.floor(FSHELF.offset/100)+1;
  out.innerHTML=`<div style="margin-top:18px;border-top:1px solid var(--line);
      padding-top:16px">
    <h3 style="margin:0 0 4px">${esc(r.friend||'Your friend')}’s library</h3>
    <p class="help">${r.total||0} of ${r.shelf_total||0} shared title(s).
      ${r.access==='catalogue'
        ? 'You can add anything here to your own Wanted list — your '
          +'indexers fetch it, not your friend.'
        : 'They allow: '+esc(r.access||'')}
      ${r.stale?'<b>Showing the last copy — their server did not answer.</b>':''}</p>
    <div class="row" style="flex-wrap:wrap;gap:8px;margin-bottom:10px">
      <input type="text" id="fs-q" placeholder="Search their library"
        value="${esc(FSHELF.q)}" style="flex:1;min-width:200px">
      <select id="fs-plat"><option value="">All platforms</option>
        ${(r.platforms||[]).map(pl=>`<option value="${esc(pl)}"${
          pl===FSHELF.platform?' selected':''}>${esc(pl)}</option>`).join('')}
      </select>
      <button class="btn ghost" id="fs-refresh">Refresh from them</button>
    </div>
    ${rows.length?`<table><thead><tr><th>Title</th><th>Platform</th>
      <th>Year</th><th>Verified</th><th></th></tr></thead><tbody>
      ${rows.map(g=>`<tr>
        <td>${esc(g.title||'')}</td>
        <td>${esc(g.platform||'')}</td>
        <td>${g.year||''}</td>
        <td>${g.verified?'<span class="pill imported">verified</span>':''}</td>
        <td style="text-align:right"><div class="rowact">
          <button data-fwant="${esc(g.title||'')}"
            data-fplat="${esc(g.platform||'')}">Add to Wanted</button>
          <button data-fplay="${esc(g.title||'')}"
            data-fpplat="${esc(g.platform||'')}">Play together</button>
        </div></td></tr>`).join('')}</tbody></table>
      ${pages>1?`<div class="row" style="gap:8px;margin-top:10px">
        <button class="btn ghost" id="fs-prev"${page<=1?' disabled':''}>Previous</button>
        <span style="color:var(--dim);font-size:12px;align-self:center"
          >Page ${page} of ${pages}</span>
        <button class="btn ghost" id="fs-next"${page>=pages?' disabled':''}>Next</button>
      </div>`:''}`
      :'<div class="empty">Nothing matches. They may not share this platform.</div>'}
    <div id="fs-out"></div></div>`;

  const reload=o=>friendShelf(FSHELF.peer,o);
  $('#fs-q').onchange=e=>reload({q:e.target.value,offset:0});
  $('#fs-plat').onchange=e=>reload({platform:e.target.value,offset:0});
  $('#fs-refresh').onclick=async()=>{
    const rp=new URLSearchParams({peer_id:FSHELF.peer,refresh:'1',limit:1});
    await j('/api/v1/friends/shelf?'+rp.toString()).catch(()=>({}));
    reload({});
  };
  if($('#fs-prev')) $('#fs-prev').onclick=()=>reload({offset:Math.max(0,FSHELF.offset-100)});
  if($('#fs-next')) $('#fs-next').onclick=()=>reload({offset:FSHELF.offset+100});

  document.querySelectorAll('[data-fwant]').forEach(b=>b.onclick=async()=>{
    const r=await j('/api/v1/friends/want',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({peer_id:FSHELF.peer,title:b.dataset.fwant,
        platform:b.dataset.fplat})});
    toast(r.ok?('Added — '+(r.detail||'')):(r.error||'Could not add'));
  });
  document.querySelectorAll('[data-fplay]').forEach(b=>b.onclick=async()=>{
    $('#fs-out').innerHTML='<div class="empty">Comparing dumps…</div>';
    const r=await j('/api/v1/friends/netplay',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({peer_id:FSHELF.peer,title:b.dataset.fplay,
        platform:b.dataset.fpplat})});
    $('#fs-out').innerHTML=netplayVerdict(r);
  });
}

//: Netplay never reports a bare success. Each verdict says what was compared
//: and what it means, because "mismatch" is the one people otherwise spend an
//: evening blaming on their connection.
function netplayVerdict(r){
  const cls=r.ok?'panel':'panel panel-warn';
  const head={
    ready:'Ready — you both have the same dump',
    unverified:'Playable, with a caveat',
    mismatch:'Same game, different dumps',
    missing:'They do not have this dump',
    unhashed:'ROMarr has not hashed your copy yet',
    error:'Could not ask'
  }[r.status]||r.status||'No answer';
  let extra='';
  if(r.room){
    extra=`<div style="margin-top:8px">Room <code>${esc(r.room)}</code>
      — both servers derive this independently, so you and
      ${esc(r.friend||'your friend')} can join it without a lobby.</div>`;
  }
  if(r.sha1){
    extra+=`<div style="margin-top:6px;color:var(--dim);font-size:11.5px">
      Compared on SHA1 <code>${esc(r.sha1)}</code>, not the title.</div>`;
  }
  return `<div class="${cls}" style="margin-top:12px"><b>${esc(head)}</b>
    ${r.detail?`<div style="margin-top:6px">${esc(r.detail)}</div>`:''}
    ${extra}</div>`;
}

function sharingEditor(peer){
  const m=document.createElement('div');
  m.className='modal';
  const opt=(v,cur,label)=>`<option value="${v}"${v===cur?' selected':''}>${label}</option>`;
  m.innerHTML=`<div class="box">
    <h3>What ${esc(peer.name)} can see</h3>
    <div class="sub">Seeing and fetching are separate on purpose.</div>
    <div class="field"><label>They see</label>
      <select data-f="scope">
        ${opt('none',peer.scope,'Nothing (peered for netplay only)')}
        ${opt('platforms',peer.scope,'Only the platforms I name')}
        ${opt('verified',peer.scope,'Only DAT-verified dumps')}
        ${opt('all',peer.scope,'Everything in my library')}
      </select></div>
    <div class="field"><label>Platforms (comma separated, for the option above)</label>
      <input type="text" data-f="platforms" data-list="1"
        value="${esc((peer.platforms||[]).join(', '))}" placeholder="snes, n64"></div>
    <div class="field"><label>They may</label>
      <select data-f="access">
        ${opt('catalogue',peer.access,'See titles only — they acquire it themselves')}
        ${opt('stream',peer.access,'Play it here, no file transfer')}
        ${opt('fetch',peer.access,'Download the file from me')}
      </select>
      <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
        Download makes your server a source for this person. Choose it
        deliberately.</div></div>
    <label class="check"><input type="checkbox" data-f="delegate_users"
      ${peer.delegate_users?'checked':''}><span>Their users get this too,
      not just them</span></label>
    <div class="foot">
      <button class="btn ghost sp" data-close>Cancel</button>
      <button class="btn" id="p-save">Save</button></div></div>`;
  document.body.append(m);
  m.onclick=e=>{ if(e.target===m||e.target.dataset.close!==undefined) closeModal(); };
  m.querySelector('#p-save').onclick=async()=>{
    const f=readForm();
    const r=await j('/api/v1/peer/policy',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({peer_id:peer.peer_id,scope:f.scope,
        access:f.access,platforms:f.platforms,
        delegate_users:f.delegate_users})});
    closeModal(); toast(r.error||'Saved'); go('peers');
  };
}

RENDER.ecosystem=async()=>{
  const d=await j('/api/v1/ecosystem').catch(()=>({categories:{}}));
  const cats=d.categories||{};
  const card=p=>`<div class="pcard"${p.is_self?' style="border-color:var(--accent)"':''}>
    <h4>${esc(p.name)}${p.is_self?' <span class="pill">you are here</span>':''}</h4>
    <div class="desc">${esc(p.blurb)}</div>
    <div class="rowact" style="margin-top:8px;flex-wrap:wrap;gap:6px">
      ${p.repo?`<a class="btn ghost" href="${esc(p.repo)}" target="_blank"
        rel="noopener noreferrer" style="text-decoration:none">GitHub</a>`:''}
      ${p.site?`<a class="btn ghost" href="${esc(p.site)}" target="_blank"
        rel="noopener noreferrer" style="text-decoration:none">Website</a>`:''}
      ${p.install?`<button class="btn ghost" data-copy="${esc(p.install)}"
        title="${esc(p.install)}">Copy install</button>`:''}
    </div></div>`;
  $('#page').innerHTML=`<div class="card"><h3>The ecosystem ROMarr stands on</h3>
      <p class="help">ROMarr acquires ROMs and files them — nothing more. It
        stores no library, serves no player, publishes no DAT, indexes no
        tracker. Every one of those is somebody else's work, and without them
        there is nothing here to automate. These are the projects that make
        ROMarr possible; each links to its own home, and installable ones
        carry their command. Go support them.</p></div>`
    +Object.entries(cats).map(([name,projects])=>
      `<div class="card"><h3>${esc(name)}</h3>
        <div class="pgrid">${projects.map(card).join('')}</div></div>`).join('');
  document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{
    try{ await navigator.clipboard.writeText(b.dataset.copy);
      toast('Copied: '+b.dataset.copy); }
    catch{ toast(b.dataset.copy); }
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
          background:var(--accent);border-radius:3px"></div></div>
      <b style="width:48px">${v}</b></div>`).join('');
  };
  $('#page').innerHTML=`
    ${s.update_available?`<div class="card" style="border-color:var(--warn)">
      <h3>Update available</h3><p class="help">ROMarr ${esc(s.latest_version)} is out;
      this install is running ${esc(s.version)}. ${SETTINGS.auto_update
        ?'Auto-update is ON — it applies on the next daily check once nothing is downloading.'
        :'Turn on auto-update under Settings → General, or update by hand when it suits you.'}</p></div>`:''}
    <div class="card"><h3>This install</h3><div class="st">
      <div><b>${esc(s.version)}</b><span>Version</span></div>
      <div><b>${num(s.library_on_disk)}</b><span>On disk here</span></div>
      <div><b>${num(s.library_catalogued)}</b><span>Catalogued, streams</span></div>
      <div><b>${num(s.library_games)}</b><span>Total known</span></div>
      <div><b>${s.wanted}</b><span>Wanted</span></div>
      <div><b>${hours}h</b><span>Uptime</span></div>
      <div><b>${(s.events||{}).grabbed||0}</b><span>Grabbed, ever</span></div>
      <div><b>${(s.events||{}).imported||0}</b><span>Imported, ever</span></div>
      <div><b>${(s.events||{}).failed||0}</b><span>Failures</span></div>
      <div><b>${s.average_rating??'—'}</b><span>Avg rating (${s.rated||0} rated)</span></div>
    </div>
    <p class="help" style="margin:10px 0 0">“Total known” is the other two
      added together — every row your library server holds. Only the first
      number is bytes you have; the second is a catalogue of things that
      would have to stream or be fetched first.</p></div>
    ${Object.keys(s.library_sources||{}).length>1?`<div class="card">
      <h3>Where the library comes from</h3>
      ${bar(Object.fromEntries(Object.entries(s.library_sources)
        .sort((a,b)=>b[1]-a[1])
        .map(([k,v])=>[SOURCE_LABEL[k]||k,v])))}
      <p class="help">Counted from the rows ROMarr has read. Each catalogue is
        identified by the filename its indexer writes; anything that matches
        neither is left as “source unknown” rather than assigned to whichever
        catalogue is larger.</p></div>`:''}
    <div class="card"><h3>Imports by platform</h3>${bar(s.imported_by_platform)}</div>
    <div class="card"><h3>Grabs by indexer</h3>${bar(s.grabbed_by_indexer)}</div>
    <div class="card"><h3>Library audit</h3>
      <p class="help">Verify what is already on the shelf: every file hashed
        against your DATs — <b>bad dump</b> means right size, wrong bytes
        (bit rot or tampering) — and byte-identical duplicates found by
        hash. One platform per run; it works in the background.</p>
      <div class="row">
        <select id="au-plat">${PLATFORMS.map(p=>
          `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('')}</select>
        <button class="btn" id="au-go">Audit</button></div>
      <div id="au-out" style="margin-top:10px"></div>
      <div id="au-quarantine" style="margin-top:10px"></div></div>
    <div class="card"><h3>Fix existing filenames</h3>
      <p class="help">ROMarr suggests canonical names only for files it verifies
        against an exact, single-file DAT entry. Review the old and new path,
        select what you want, then confirm. Disc sets and ambiguous matches stay put.
        Files in a subfolder move to the platform folder; empty folders are left alone.</p>
      <div class="row"><select id="rn-plat">${PLATFORMS.map(p=>
        '<option value="'+esc(p.slug)+'">'+esc(p.name)+'</option>').join('')}</select>
        <input id="rn-scope" placeholder="Optional subfolder (leave blank for all)" aria-label="Optional folder inside the platform">
        <button class="btn" id="rn-preview">Preview safe renames</button></div>
      <div id="rn-out" style="margin-top:10px"></div>
      <div id="rn-journals" style="margin-top:10px"></div></div>
    <div class="card"><h3>Shelf</h3><div class="st">
      <div><b>${(s.statuses||{}).playing||0}</b><span>Playing</span></div>
      <div><b>${(s.statuses||{}).completed||0}</b><span>Completed</span></div>
      <div><b>${(s.statuses||{}).shelved||0}</b><span>Shelved</span></div>
    </div><p class="help">Set from any game tile in the Library.</p>
      <div class="row"><select id="rl-st"><option value="shelved">From Shelved</option>
        <option value="playing">From Playing</option>
        <option value="shelved,playing">From both</option></select>
        <button class="btn" id="rl-go">Pick my next game</button></div>
      <div id="rl-out" style="margin-top:8px"></div></div>`;
  const rl=document.getElementById('rl-go');
  if(rl)rl.onclick=async()=>{
    const out=document.getElementById('rl-out');
    const st=document.getElementById('rl-st').value;
    const r=await fetch('/api/v1/game/roulette?status='+encodeURIComponent(st),
      {headers:typeof authHeaders==='function'?authHeaders():{}});
    const b=await r.json().catch(()=>({}));
    if(out)out.innerHTML=r.ok
      ?`<b>${esc(b.game)}</b> <span class="help">(${esc(b.platform)})</span>`
      :`<span class="help">${esc(b.error||'Nothing to pick from yet.')}</span>`;};

  const refreshQuarantine=async()=>{
    const node=$('#au-quarantine'); if(!node) return;
    const q=await j('/api/v1/audit/quarantine').catch(()=>({manifests:[]}));
    const pending=(q.manifests||[]).flatMap(m=>
      (m.entries||[]).filter(e=>e.status!=='restored').map(e=>({manifest:m,entry:e})));
    node.innerHTML=pending.length?'<h4>Quarantined files</h4>'+pending.map(x=>
      '<div class="row" style="justify-content:space-between"><span class="help">'
      +esc(x.entry.original)+' · '+esc(x.entry.sha1||'')+'</span><button class="btn ghost au-restore" '
      +'data-manifest="'+esc(x.manifest.manifest_id)+'" data-entry="'+esc(x.entry.id)+'">Restore</button></div>'
    ).join(''):'';
    node.querySelectorAll('.au-restore').forEach(btn=>btn.onclick=async()=>{
      btn.disabled=true;
      const result=await j('/api/v1/audit/restore',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({manifest_id:btn.dataset.manifest,entry_id:btn.dataset.entry})});
      if(result.error){toast(result.error);btn.disabled=false;return;}
      toast('Restored '+result.file); refreshQuarantine();
    });
  };
  let auditDuplicateOffset=0;
  const selectedDuplicateFiles=new Set();
  const paintAudit=a=>{
    const out=$('#au-out'); if(!out) return;
    if(!a||!a.status||a.status==='never run'){out.innerHTML='';refreshQuarantine();return;}
    const allDupes=a.duplicates||[];
    const pageSize=200;
    if(auditDuplicateOffset>=allDupes.length)auditDuplicateOffset=Math.max(0,allDupes.length-pageSize);
    const dupes=allDupes.slice(auditDuplicateOffset,auditDuplicateOffset+pageSize);
    out.innerHTML=`<p class="help">
      <b>${esc(a.platform||'')}</b> — ${esc(a.status)} ·
      ${a.scanned||0} scanned · ${a.verified||0} verified ·
      <b style="color:${a.bad?'var(--bad)':'inherit'}">${a.bad||0} bad</b> ·
      ${a.unknown||0} unknown · ${allDupes.length} duplicate(s)</p>
      ${(a.bad_files||[]).length?`<details><summary class="help"
        style="cursor:pointer;margin:0">Bad dumps</summary>
        <div style="color:var(--dim);font-size:12px">${a.bad_files.map(f=>
          esc(f.file)+' — '+esc(f.detail)).join('<br>')}</div></details>`:''}
      ${dupes.length?`<details open><summary class="help"
        style="cursor:pointer;margin:0">Exact duplicate preview — ${fmtBytes(allDupes.reduce((n,d)=>n+(d.size||0),0))} recoverable · showing ${auditDuplicateOffset+1}–${Math.min(auditDuplicateOffset+dupes.length,allDupes.length)} of ${allDupes.length}</summary>
        <div style="color:var(--dim);font-size:12px">${dupes.map(d=>
          '<label style="display:block"><input type="checkbox" class="au-dup" data-file="'+esc(d.file)+'" '
          +(selectedDuplicateFiles.has(d.file)?'checked':'')+'> '
          +esc(d.file)+' = '+esc(d.same_as)+' · '+fmtBytes(d.size||0)+'</label>').join('')}</div>
        <div class="row"><button class="btn ghost" id="au-prev" ${auditDuplicateOffset===0?'disabled':''}>Previous</button>
        <button class="btn ghost" id="au-next" ${auditDuplicateOffset+pageSize>=allDupes.length?'disabled':''}>Next</button>
        <button class="btn ghost" id="au-quarantine-go" disabled>Select duplicates below to quarantine</button></div></details>`:''}`;
    if(dupes.length){
      const checks=[...out.querySelectorAll('.au-dup')], go=$('#au-quarantine-go');
      const update=()=>{const selected=selectedDuplicateFiles.size;
        if(go){go.disabled=a.status!=='done'||!selected;
          go.textContent=selected?'Quarantine '+selected+' selected':'Select duplicates below to quarantine';}};
      checks.forEach(c=>c.onchange=()=>{if(c.checked)selectedDuplicateFiles.add(c.dataset.file);
        else selectedDuplicateFiles.delete(c.dataset.file);update();});
      update();
      const prev=$('#au-prev'), next=$('#au-next');
      if(prev)prev.onclick=()=>{auditDuplicateOffset=Math.max(0,auditDuplicateOffset-pageSize);paintAudit(a);};
      if(next)next.onclick=()=>{auditDuplicateOffset=Math.min(allDupes.length-1,auditDuplicateOffset+pageSize);paintAudit(a);};
      if(go)go.onclick=async()=>{
        const chosen=[...selectedDuplicateFiles].map(file=>({file}));
        if(!confirm('Move '+chosen.length+' exact duplicate file(s) into quarantine? They can be restored later.'))return;
        go.disabled=true;
        const result=await j('/api/v1/audit/quarantine/move',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({platform:a.platform,files:chosen})});
        if(result.error){toast(result.error);update();return;}
        toast('Quarantined '+result.entries.length+' duplicate file(s)');
        selectedDuplicateFiles.clear();
        await refreshQuarantine();
        out.innerHTML='<p class="help">Quarantine complete. Run Audit again to refresh the duplicate preview.</p>';
      };
    }
    refreshQuarantine();
  };
  j('/api/v1/audit').then(paintAudit).catch(()=>{});
  let auditTimer=null;
  $('#au-go').onclick=async()=>{
    selectedDuplicateFiles.clear(); auditDuplicateOffset=0;
    const a=await j('/api/v1/audit',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({platform:$('#au-plat').value})});
    if(a.error){toast(a.error);return;}
    paintAudit(a);
    clearInterval(auditTimer);
    auditTimer=setInterval(async()=>{
      if(!$('#au-out')){clearInterval(auditTimer);return;}
      const s=await j('/api/v1/audit').catch(()=>null);
      if(s) paintAudit(s);
      if(s&&s.status!=='running') clearInterval(auditTimer);
    },2000);
  };

  const rnOut=$('#rn-out'), rnJournals=$('#rn-journals');
  let rnTimer=null;
  const paintRenameState=data=>{
    if(!rnOut||!rnJournals)return;
    const a=data.preview||{status:'never run'};
    if(a.status==='running'){
      rnOut.innerHTML='<p class="help">Checking files against the loaded DATs. This can take a while on a large platform.</p>';
    }else if(a.status==='failed'||a.error){
      rnOut.innerHTML='<p class="help">'+esc(a.error||'Preview failed')+'</p>';
    }else if(a.status==='applied'){
      rnOut.innerHTML='<p class="help">Selected names updated. Check the rename history below to restore the originals if needed. Run another preview to continue.</p>';
    }else if(a.status==='rolled_back'){
      rnOut.innerHTML='<p class="help">Original names restored. Run a new preview to check the library again.</p>';
    }else if(a.status==='done'){
      const rows=a.entries||[], eligible=rows.filter(x=>x.eligible);
      const blocked=rows.filter(x=>!x.eligible);
      rnOut.innerHTML='<p class="help">'+(a.candidate_count||a.scanned||0)+' candidate file(s) in this scope; '+
        'showing '+(a.scanned?(a.offset+1):0)+'-'+(a.offset+a.scanned)+'; '+
        (a.eligible||0)+' safe suggestion(s), '+(a.blocked||0)+' left untouched.</p>'+
        (a.offset>0||a.has_more?'<div class="row">'+
          '<button class="btn ghost" id="rn-prev-page" '+(a.offset===0?'disabled':'')+'>Previous batch</button>'+
          '<button class="btn ghost" id="rn-next-page" '+(!a.has_more||(a.truncated&&a.next_offset>=a.candidate_count)?'disabled':'')+'>Next batch</button></div>':'')+
        (a.truncated?'<p class="help">This folder reached the 50,000-file cap. Enter a subfolder above to scan the remaining files.</p>':'')+
        (a.message?'<p class="help">'+esc(a.message)+'</p>':'')+
        (eligible.length?'<h4>Suggested changes</h4><div style="max-height:320px;overflow:auto">'+eligible.slice(0,200).map(x=>
          '<label style="display:block"><input type="checkbox" class="rn-file" value="'+esc(x.file)+'"> <code>'+
          esc(x.file)+'</code> &rarr; <code>'+esc(x.target)+'</code></label>').join('')+'</div>':'')+
        (eligible.length>200?'<p class="help">Only the first 200 suggestions are shown. Narrow the platform folder to select more.</p>':'')+
        (blocked.length?'<details><summary class="help" style="cursor:pointer">Why '+blocked.length+
          ' file(s) were skipped</summary><div style="max-height:260px;overflow:auto;font-size:12px;color:var(--dim)">'+
          blocked.slice(0,300).map(x=>'<div><code>'+esc(x.file)+'</code> - '+esc(x.reason||'Not safe to rename')+'</div>').join('')+
          '</div></details>':'')+
        (eligible.length?'<p><button class="btn" id="rn-apply">Rename selected files</button></p>':'');
      const prevPage=$('#rn-prev-page'), nextPage=$('#rn-next-page');
      if(prevPage)prevPage.onclick=()=>startRenamePreview(Math.max(0,a.offset-500));
      if(nextPage)nextPage.onclick=()=>startRenamePreview(a.next_offset||0);
      const applyButton=$('#rn-apply');
      if(applyButton)applyButton.onclick=async()=>{
        const files=[...rnOut.querySelectorAll('.rn-file:checked')].map(x=>x.value);
        if(!files.length){toast('Select at least one file to rename.');return;}
        if(!confirm('Rename '+files.length+' verified file(s) to the paths shown? ROMarr will keep a journal so you can roll this back.'))return;
        applyButton.disabled=true;
        const result=await j('/api/v1/audit/renames/apply',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({platform:a.platform,files})});
        if(result.error){toast(result.error);applyButton.disabled=false;return;}
        toast('Renamed '+result.entries.length+' file(s).');
        paintRenameState(await j('/api/v1/audit/renames').catch(()=>data));
      };
    }else{
      rnOut.innerHTML='<p class="help">No preview yet. Choose a platform with DATs loaded under Settings, then preview.</p>';
    }
    const journals=(data.manifests||[]).filter(m=>m.status!=='rolled_back').slice(-10).reverse();
    rnJournals.innerHTML=journals.length?'<h4>Rename history</h4>'+journals.map(m=>
      '<div class="row" style="justify-content:space-between"><span class="help">'+
      esc(m.status)+' - '+(m.entries||[]).length+' file(s) - <code>'+esc(m.manifest_id||'')+
      '</code></span><button class="btn ghost rn-rollback" data-id="'+esc(m.manifest_id||'')+'">Restore original names</button></div>'
    ).join(''):'';
    rnJournals.querySelectorAll('.rn-rollback').forEach(button=>button.onclick=async()=>{
      if(!confirm('Restore the original names? ROMarr will refuse to overwrite a file that appeared in the meantime.'))return;
      button.disabled=true;
      const result=await j('/api/v1/audit/renames/rollback',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({manifest_id:button.dataset.id})});
      if(result.error){toast(result.error);button.disabled=false;return;}
      toast('Original names restored.');
      paintRenameState(await j('/api/v1/audit/renames').catch(()=>data));
    });
  };
  j('/api/v1/audit/renames').then(paintRenameState).catch(()=>{});
  const startRenamePreview=async(offset=0)=>{
    clearInterval(rnTimer);
    const started=await j('/api/v1/audit/renames/preview',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({platform:$('#rn-plat').value,
        scope:$('#rn-scope').value,offset})});
    if(started.error){toast(started.error);return;}
    paintRenameState({preview:started,manifests:[]});
    rnTimer=setInterval(async()=>{
      if(!$('#rn-out')){clearInterval(rnTimer);return;}
      const current=await j('/api/v1/audit/renames').catch(()=>null);
      if(current)paintRenameState(current);
      if(current&&current.preview&&current.preview.status!=='running')clearInterval(rnTimer);
    },2000);
  };
  $('#rn-preview').onclick=()=>startRenamePreview(0);
};

RENDER.queue=async()=>{
  const d=await j('/api/v1/queue');
  const failed=(d.items||[]).filter(i=>i.state==='failed').length;
  $('#page').innerHTML=`<div class="row" style="margin-bottom:12px">
      <button class="btn ghost" id="q-import">Import completed now</button>
      ${failed?`<button class="btn ghost" id="q-clearfail">Clear ${failed} failed</button>`:''}
      <span class="help" style="margin:0">${(d.items||[]).length} in flight</span>
    </div>`+(d.items.length?`<table><thead><tr><th>Game</th>
    <th>Platform</th><th>Release</th><th>Seeders</th><th>State</th><th></th></tr></thead><tbody>
    ${d.items.map((i,n)=>`<tr><td>${esc(i.game)}</td><td>${esc(i.platform)}</td>
      <td>${esc(i.release||'—')}</td><td>${i.seeders||'—'}</td>
      <td><span class="pill ${esc(i.state)}">${esc(i.state)}</span>
      ${i.detail?`<div style="color:var(--dim);font-size:11.5px">${esc(i.detail)}</div>`:''}
      </td><td style="text-align:right"><div class="rowact">
        ${i.state==='failed'?`<button data-qretry="${n}">Retry</button>`:''}
        <button data-qremove="${n}">Remove</button></div></td></tr>`).join('')}
      </tbody></table>`
    :'<div class="empty">Queue is empty. Requests in flight show up here.</div>');

  $('#q-import').onclick=async e=>{
    e.target.disabled=true; e.target.textContent='Importing…';
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({name:'ImportCompleted'})});
    toast(r.message||'Done'); go('queue'); refreshCounts();
  };
  const cf=$('#q-clearfail');
  if(cf) cf.onclick=async()=>{
    await j('/api/v1/queue/clear',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({state:'failed'})});
    go('queue'); refreshCounts();
  };
  const act=async(index,action)=>{
    const r=await j('/api/v1/queue/action',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({index:Number(index),action})});
    toast(r.error||(action==='retry'?(r.ok?'Grabbed':'Still no luck'):'Removed'));
    go('queue'); refreshCounts();
  };
  document.querySelectorAll('[data-qretry]').forEach(b=>b.onclick=()=>act(b.dataset.qretry,'retry'));
  document.querySelectorAll('[data-qremove]').forEach(b=>b.onclick=()=>act(b.dataset.qremove,'remove'));
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
let SCHEMA = { downloadclient: {}, indexer: {}, library: {},
               metadataprovider: {}, connection: {} };

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
  metadataprovider: { label: 'Metadata Provider', page: 'metadata' },
  connection: { label: 'Connection', page: 'connections' },
};

/** Choose a type, then edit it. */
async function addItem(kind, afterSave){
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
    editItem(kind, { type: b.dataset.t }, afterSave);
  });
}

async function editItem(kind, item, afterSave){
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
    const saved=await j(`/api/v1/${kind}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload()) });
    if(saved.error){line(false,saved.error);e.target.disabled=false;return;}
    closeModal(); toast('Saved');
    if(afterSave)await afterSave();
    else go((KINDS[kind] || {}).page || 'clients');
  };

  const del = m.querySelector('#m-del');
  if(del) del.onclick = async () => {
    if(!confirm(`Remove ${item.name || spec.label}?`)) return;
    await fetch(`/api/v1/${kind}/${item.id}`, { method: 'DELETE' });
    closeModal(); toast('Removed'); go((KINDS[kind] || {}).page || 'clients');
  };
}

/* ---------------- settings ---------------- */
function settingsPage(title, help, body, after){
  $('#page').innerHTML=`<div class="card"><h3>${title}</h3>
    <p class="help">${help}</p>${body}
    <button class="btn" id="s-save">Save</button></div>`;
  if(after) after();
  $('#s-save').onclick=async()=>{
    const patch={};
    document.querySelectorAll('[data-k]').forEach(el=>{
      patch[el.dataset.k]= el.type==='checkbox' ? el.checked
        : el.type==='number' ? Number(el.value)
        : el.dataset.mappings ? el.value.split('\n').map(line=>{
            const [remote,local]=line.split('=').map(x=>(x||'').trim());
            return remote&&local?{remote,local}:null;
          }).filter(Boolean)
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

const mapLines=m=>(m||[]).map(x=>(x.remote||'')+' = '+(x.local||'')).join('\n');
RENDER.media=()=>settingsPage('Media Management',
  'Where imported ROMs are filed. This must be the same path your library server scans.',
  fld('library_path','ROM library root',SETTINGS.library_path)
  +fld('dat_path','DAT directory',SETTINGS.dat_path||'')
  +`<div class="field"><label>Remote path mappings</label>
     <textarea data-k="remote_path_mappings" data-mappings="1" rows="3"
       style="width:100%;resize:vertical;font:12px/1.5 ui-monospace,Menlo,monospace"
       placeholder="/downloads = /mnt/downloads">${esc(mapLines(SETTINGS.remote_path_mappings))}</textarea>
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       One per line: what the download client says = what ROMarr sees.
       Longest matching prefix wins.</div></div>`
  +sel('library_layout','Folder structure',
     [['flat','Structure A — platform/rom'],['nested','Structure B — platform/roms/rom']],
     SETTINGS.library_layout||'flat',
     'Match your library server. RomM Structure A files as '
     +'&lt;root&gt;/&lt;platform&gt;/&lt;rom&gt;; Structure B adds a roms/ level. '
     +'Per-library overrides live on the Libraries page.')
  +chk('rename_on_import','Use DAT names for verified single-file imports (disc-set filenames stay intact)',SETTINGS.rename_on_import)
  +chk('overwrite_existing','Overwrite an existing file',SETTINGS.overwrite_existing)
  +chk('rescan_after_import','Tell your library server to rescan after an import',SETTINGS.rescan_after_import)
  +chk('clamav_enabled','Scan downloads with ClamAV before importing them',SETTINGS.clamav_enabled)
  +fld('clamav_address','ClamAV address',SETTINGS.clamav_address||'','text',
     'placeholder="tcp://clamav:3310 or /run/clamav/clamd.sock"')
  +`<div style="color:var(--dim);font-size:11.5px;margin:-6px 0 12px">
     An infected download is refused and blocklisted; while ClamAV cannot be
     reached, finished downloads wait instead of going in unscanned. System &rarr;
     Status shows whether it answers.</div>`
  +sel('translation_policy','Fan translations in 1G1R sets',
     [['exclude','Exclude — published dumps only'],
      ['fill','Fill gaps — use a translation only when no preferred-region dump exists'],
      ['prefer','Prefer — take a translation over the region winner when one exists'],
      ['keep_both','Keep both — original plus the translation in a Translations subfolder']],
     SETTINGS.translation_policy||'exclude',
     'What Collections does with a fan translation in a language you selected '
     +'below. The Digimon-only-in-Japan case: "Fill" downloads the translation '
     +'instead of leaving the game Japanese-only; "Keep both" files it under '
     +'Translations/ so your library shows it as a variant.')
  +`<div class="field"><label>Fan-translation languages</label>
     <input type="text" data-k="translation_langs" data-list="1"
       value="${esc((SETTINGS.translation_langs||['en']).join(', '))}">
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       Comma-separated codes, e.g. <code>de, en</code>. Matches scene markers
       like <code>[T-De]</code> / <code>[T+Eng]</code>; an unmarked
       <code>(Translation)</code> counts as English.</div></div>`
  +sel('region_mode','Region list in 1G1R sets',
     [['prefer','Prefer selected regions — keep a title even when no dump matches'],
      ['strict','Only selected regions — omit a title when no dump matches']],
     SETTINGS.region_mode||'prefer',
     '"Prefer" is the historical fallback: a game with only Japan dumps stays '
     +'in the set rather than being lost. "Strict" treats the region list as '
     +'an allowlist — the plan counts every title omitted for having no dump '
     +'in your regions.'));

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
  // Reported separately because "not installed" is a legitimate state here,
  // not a fault: an install that only fetches plain URLs is finished. Without
  // this line a site plugin needing a click fails with nothing on screen
  // saying the browser lane was never set up.
  const br=await j('/api/v1/downloadclient/browser').catch(()=>null);
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
    ${br?`<p class="help">ROM sites are fetched over plain HTTP by the
      <b>Direct HTTP</b> client. A handful put the file behind a form the
      site's own page submits &mdash; those need the <b>Headless Browser</b>
      client, which loads the real page and clicks the real control.
      ${br.available
        ?`<span style="color:var(--ok)">Browser mode is available (${esc(br.reason)}, ${esc(br.where)}).</span>`
        :`<span style="color:var(--warn)">Browser mode is not configured:
           ${esc(br.reason)}.</span> Direct downloads are unaffected.`}
      A site that can only be downloaded from by answering a CAPTCHA or a
      bot-detection challenge is reported as unavailable rather than worked
      around.</p>`:''}
    ${d.items.length?`<table><thead><tr><th>Client</th><th>Protocol</th><th>Address</th>
      <th>Category</th><th>Status</th><th></th></tr></thead><tbody>
      ${d.items.map((c,i)=>`<tr><td>${esc(c.name)}
        ${c.detail?`<div class="help" style="margin:2px 0 0">${esc(c.detail)}</div>`:''}</td>
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
  'Connections to the rest of the stack. Download-client and indexer credentials '
  +'live in the environment file; the OIDC client secret below is the one '
  +'secret this page stores, write-only — it is never read back.',
  `<div class="field"><label>Prowlarr</label>
     <input type="text" value="${esc(SETTINGS._prowlarr_url||'')}" disabled></div>
   <div class="field"><label>qBittorrent</label>
     <input type="text" value="${esc(SETTINGS._qbit_url||'')}" disabled></div>
   <div class="field"><label>RomM</label>
     <input type="text" value="${esc(SETTINGS._romm_url||'')}" disabled></div>
   <div class="field"><label>Your public URL (for Friends)</label>
     <input type="text" data-k="public_url"
       value="${esc(SETTINGS.public_url||'')}"
       placeholder="https://romarr.example.com">
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       How a friend’s server reaches yours. An invitation carries this — without
       it, whoever redeems your invitation has nowhere to call back to. Peering
       is the only feature that needs ROMarr to know its own address.</div></div>`
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
  +chk('update_check','Check github.com daily for a newer ROMarr',
       SETTINGS.update_check)
  +chk('auto_update','Apply updates automatically (git installs only; waits until nothing is downloading, then restarts ROMarr)',
       SETTINGS.auto_update)
  +sel('update_channel','Update channel',
     [['stable','Stable — latest GitHub release tag'],
      ['nightly','Nightly — latest commit on main']],
     SETTINGS.update_channel||'stable',
     'Stable follows tagged releases. Nightly follows the main branch and '
     +'restarts on any new commit — useful for a dev install, too eager for '
     +'a living-room one.')
  +chk('auto_update_plugins','Update ROM Hub plugins daily from their sources',
       SETTINGS.auto_update_plugins)
  +`<h3 style="margin-top:18px">Security</h3>
    <div class="row" style="gap:8px;align-items:center;margin:8px 0">
      <button class="btn ghost" id="g-key" type="button">Reveal API key</button>
      <code id="g-keyout" style="font-size:12px;color:var(--dim)"></code></div>
    <div class="row" style="gap:8px;align-items:center;margin:8px 0">
      <button class="btn ghost" id="g-totp" type="button">Enable two-factor (TOTP)</button>
      <button class="btn ghost" id="g-totpoff" type="button">Disable</button>
      <span class="help" style="margin:0">Gates sign-in; the API key is
        already a high-entropy secret and is not gated.</span></div>
    <pre id="g-totpout" style="display:none;font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px"></pre>
    <h3 style="margin-top:18px">Sign in with an identity provider (OIDC)</h3>
    <p class="help">Let people sign in through Authentik, Keycloak, Zitadel,
      Google or any OpenID Connect provider. ROMarr acts as the relying party:
      a "Sign in" button on the login page runs the authorization-code + PKCE
      flow, verifies the returned id_token, and starts a ROMarr session. Leave
      the issuer blank to keep it off. Set your public URL above first — the
      callback is <code>&lt;public-url&gt;/oidc/callback</code>, which is the
      redirect URI to register with your provider.</p>`
  +chk('oidc_enabled','Enable OIDC sign-in',SETTINGS.oidc_enabled)
  +fld('oidc_display_name','Button label',SETTINGS.oidc_display_name||'SSO',
       'text','placeholder="Authentik"')
  +`<div class="field"><label>Issuer URL</label>
     <input type="text" data-k="oidc_issuer" value="${esc(SETTINGS.oidc_issuer||'')}"
       placeholder="https://authentik.example.com/application/o/romarr/">
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       Authentik: the provider's "OpenID Configuration" issuer, shown on the
       provider page. ROMarr reads <code>/.well-known/openid-configuration</code>
       from it to find the endpoints.</div></div>`
  +fld('oidc_client_id','Client ID',SETTINGS.oidc_client_id)
  +`<div class="field"><label>Client secret (confidential clients only)</label>
     <input type="password" data-k="_oidc_client_secret" autocomplete="new-password"
       placeholder="${SETTINGS.oidc_client_id?'stored — leave blank to keep':''}">
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       A public/PKCE client (Authentik's usual choice for this) needs no secret.
       Stored write-only; never shown again.</div></div>`
  +fld('oidc_scopes','Scopes',SETTINGS.oidc_scopes||'openid profile email',
       'text','placeholder="openid profile email"')
  +fld('oidc_required_group','Required group (optional)',
       SETTINGS.oidc_required_group||'',
       'text','placeholder="romarr-users"')
  +`<div class="field"><label>Allowed emails (optional)</label>
     <input type="text" data-k="oidc_allowed_emails" data-list="1"
       value="${esc((SETTINGS.oidc_allowed_emails||[]).join(', '))}"
       placeholder="alice@example.com, bob@example.com">
     <div style="color:var(--dim);font-size:11.5px;margin-top:4px">
       Blank = any user your provider authenticates (and in the required group,
       if set). The provider must send a <code>groups</code> claim for a group
       restriction to work — Authentik needs it in the scope mapping.</div></div>`,
  ()=>{
    $('#g-key').onclick=async()=>{
      const d=await j('/api/v1/system/apikey').catch(()=>({}));
      $('#g-keyout').textContent=d.api_key||'could not read it';
    };
    $('#g-totp').onclick=async()=>{
      if(!confirm('Enable two-factor sign-in? You will need a TOTP app '
        +'(Aegis, Google Authenticator…) from the next sign-in on.')) return;
      const d=await j('/api/v1/totp/enroll',{method:'POST',
        headers:{'content-type':'application/json'},body:'{}'}).catch(()=>({}));
      const out=$('#g-totpout'); out.style.display='block';
      out.textContent='Secret:  '+(d.secret||'?')
        +'\nURI:     '+(d.uri||'?')
        +'\n\nBackup codes (single use, save them now):\n  '
        +((d.backup_codes||[]).join('\n  '))
        +'\n\n'+(d.note||'');
    };
    $('#g-totpoff').onclick=async()=>{
      const d=await j('/api/v1/totp/disable',{method:'POST',
        headers:{'content-type':'application/json'},body:'{}'}).catch(()=>({}));
      toast(d.ok?'Two-factor disabled':'Failed');
      $('#g-totpout').style.display='none';
    };
  });

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
      ${g.configured?row('GG Requestz page',g.ok,
          g.url+' (reachability only; request delivery is configured below)')
        :`<tr><td>GG Requestz page</td><td><span class="dot"></span>not configured</td>
          <td style="color:var(--dim)">GGREQUESTZ_URL only adds and checks the
          front-end link; it does not deliver requests</td></tr>`}
      ${h.stream_url
        ? row('Stream server',(h.play_routes||{}).stream>0,h.stream_url)
        : `<tr><td>Stream server</td><td><span class="dot"></span>not configured</td>
           <td style="color:var(--dim)">set STREAM_SERVER_URL to play PS2, GameCube,
           Wii, Dreamcast and 3DS here</td></tr>`}
      ${(h.clamav||{}).configured
        ? row('ClamAV (scans downloads before import)',(h.clamav||{}).ok,
              (h.clamav||{}).ok?`${(h.clamav||{}).address} — ${(h.clamav||{}).version}`
                :((h.clamav||{}).problem||''))
        : `<tr><td>ClamAV</td><td><span class="dot"></span>not configured</td>
           <td style="color:var(--dim)">turn it on under Media Management to scan
           downloads before they reach the library</td></tr>`}
      ${(h.retroarch_nci||{}).configured
        ? row('RetroArch (network commands)',(h.retroarch_nci||{}).ok,
              (h.retroarch_nci||{}).ok
                ?`${esc((h.retroarch_nci||{}).label)} — ${esc((h.retroarch_nci||{}).engine)}${
                    (h.retroarch_nci||{}).state==='playing'||(h.retroarch_nci||{}).state==='paused'
                    ?`, playing ${esc((h.retroarch_nci||{}).game||'content')} (${esc((h.retroarch_nci||{}).system_id||'')})`
                    :', nothing loaded'} — reports what is playing, never which cores are installed`
                :((h.retroarch_nci||{}).problem||''))
        : `<tr><td>RetroArch (network commands)</td><td><span class="dot"></span>not configured</td>
           <td style="color:var(--dim)">set RETROARCH_NCI_HOST to a RetroArch with
           network_cmd_enable = "true" (UDP 55355); ROMarr only reads from it</td></tr>`}
      ${(h.moonlight||{}).configured
        ? row(esc((h.moonlight||{}).kind_label||'Moonlight host'),
              (h.moonlight||{}).ok,
              (h.moonlight||{}).ok?(h.moonlight||{}).label
                                  :((h.moonlight||{}).problem||''))
        : `<tr><td>Moonlight host</td><td><span class="dot"></span>not configured</td>
           <td style="color:var(--dim)">set MOONLIGHT_HOST to a Wolf, Sunshine or
           Steam Headless machine</td></tr>`}
    </tbody></table></div>
    ${ggrequestzCard(g)}
    ${playCard(h.play_routes||{})}
    ${moonlightCard(h.moonlight||{})}
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

  wireGGRequestz();
  wireMoonlight(h.moonlight||{});

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
  const feRow=$('#fe-row'); if(!feRow) return;   // navigated away mid-fetch
  feRow.innerHTML=(fe.formats||[]).map(f=>
    '<button class="btn ghost fe-btn" data-f="'+esc(f.name||f)+'">'
    +esc(f.label||f.name||f)+'</button>').join('')
    ||'<span class="help" style="margin:0">No frontend formats available.</span>';
  feRow.querySelectorAll('.fe-btn').forEach(b=>b.onclick=()=>
    save('/api/v1/frontend/export?format='+b.dataset.f,
         'romarr-'+b.dataset.f+'.export'));
};

// GG Requestz pushes to ROMarr; the reachability probe above is deliberately
// labelled as the opposite direction. The API key is revealed only after a
// deliberate click, just like Settings -> General, because the finished URL
// is itself a credential and should not sit in the page by default.
const ggrequestzCard=g=>`<div class="card"><h3>GG Requestz requests</h3>
  <p class="help"><b>Configure this in GG Requestz, not ROM Hub.</b>
  Set <code>REQUEST_WEBHOOK_URL</code> in the GG Requestz container to ROMarr's
  authenticated receiver. <code>GGREQUESTZ_URL</code> in ROMarr only creates
  the page link and reachability check above.</p>
  <label>ROMarr URL as the GG Requestz container reaches it</label>
  <input id="ggr-base" placeholder="http://romarr:6868">
  <p class="help">On the same Docker network this is normally
  <code>http://romarr:6868</code>. On Unraid or separate networks, use ROMarr's
  LAN or HTTPS URL. The value must be reachable from inside GG Requestz.</p>
  <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
    <button class="btn" id="ggr-build" type="button">Reveal setup value</button>
    <button class="btn ghost" id="ggr-copy" type="button" disabled>Copy</button>
  </div>
  <pre id="ggr-value" style="display:none;font:12px/1.6 ui-monospace,Menlo,monospace;
    color:var(--dim);white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);
    border:1px solid var(--line);border-radius:6px;padding:10px"></pre>
  <p class="help">Restart GG Requestz after changing its environment. In GG
  Requestz 1.5+, the webhook fires when a request becomes <b>approved</b>;
  enable <code>request.auto_approve</code> or approve it manually. A successful
  delivery now receives HTTP 202. Rejected payloads receive HTTP 422 and are
  named in both applications' logs.</p>
  <p class="help">Treat the finished URL like a password: it contains ROMarr's
  API key. Keep it on a trusted Docker network or HTTPS and do not paste it in
  public logs.</p></div>`;

const wireGGRequestz=()=>{
  const base=$('#ggr-base'), build=$('#ggr-build'), copy=$('#ggr-copy');
  const value=$('#ggr-value'); if(!base||!build||!copy||!value) return;
  // Do not assume the browser-facing origin works from another container.
  // `localhost` here would mean the GG Requestz container itself there.
  base.value='';
  base.oninput=()=>{
    value.textContent=''; value.style.display='none'; copy.disabled=true;
  };
  build.onclick=async()=>{
    build.disabled=true;
    try{
      if(!base.value.trim()) throw new Error(
        'Enter the ROMarr URL reachable from inside GG Requestz');
      const d=await j('/api/v1/system/apikey');
      if(!d.api_key) throw new Error('ROMarr did not return an API key');
      const target=new URL('/api/v1/webhook/ggrequestz',base.value.trim());
      target.searchParams.set('apikey',d.api_key);
      value.textContent='REQUEST_WEBHOOK_URL='+target.href;
      value.style.display='block'; copy.disabled=false;
    }catch(e){
      value.textContent='Could not build the URL: '+(e.message||e);
      value.style.display='block'; copy.disabled=true;
    }finally{build.disabled=false;}
  };
  copy.onclick=async()=>{
    try{await navigator.clipboard.writeText(value.textContent); toast('Copied');}
    catch(_){toast('Copy failed — select the value manually');}
  };
};

// The Moonlight host: what it is, what it proves, and the one step that is
// still a human's.
//
// The card is written to be readable when the answer is "nothing", because
// that is the common case and the interesting one. A host that answers
// /serverinfo but whose app list ROMarr cannot read is working perfectly and
// grants no platform anything -- if this card rendered a green dot and
// stopped, an operator would reasonably conclude their PS2 games were now
// playable, and they would not be.
//
// The pairing section never says "Pair" on its own. Moonlight's PIN is
// generated by the client on the user's device; ROMarr can only be the box it
// is typed into, and the copy says so before the input rather than after a
// failure.
const moonlightCard=m=>{
  if(!m.configured) return `<div class="card"><h3>Game streaming host</h3>
    <p class="help">${esc(m.hint||'')}. A Moonlight host renders a real
    desktop emulator on the host's GPU and sends video &mdash; which is how
    machines with no browser core get played. ROMarr reports what it can
    verify about one and never guesses the rest.</p>
    <p class="help">${esc(m.manual||'')}</p></div>`;

  if(!m.ok) return `<div class="card"><h3>${esc(m.kind_label||'Moonlight')}</h3>
    <p class="testline bad">No answer from ${esc(m.host||'')}:${esc(String(m.port||''))}
    &mdash; ${esc(m.problem||'unreachable')}</p>
    <p class="help">ROMarr probes <code>/serverinfo</code> on the Moonlight
    port, which needs no credential and no pairing. If that does not answer,
    the host is down, firewalled, or on another port.</p></div>`;

  const slugs=Object.keys(m.platforms||{});
  const pend=(m.pairing||{}).pending||[];
  const canList=(m.pairing||{}).can_list_pending;

  return `<div class="card"><h3>${esc(m.kind_label||'Moonlight')}</h3>
    <div class="st">
      <div><b>${esc(m.hostname||m.host||'?')}</b><span>Host</span></div>
      <div><b>${esc(m.appversion||'?')}</b><span>Version</span></div>
      <div><b>${m.busy?'In a game':'Idle'}</b><span>State</span></div>
      <div><b>${(m.paired||[]).length}</b><span>Paired clients</span></div>
    </div>

    <h4 style="margin:18px 0 6px">What this host adds</h4>
    ${m.apps_read
      ? (slugs.length
          ? `<p class="help">${slugs.length} platform${slugs.length===1?'':'s'}
             gain a stream route here, each because a named single-machine
             emulator is on this host's app list:</p>
             <table><tbody>${slugs.map(s=>`<tr><td>${esc(s)}</td>
               <td style="color:var(--dim)">${esc(m.platforms[s])}</td></tr>`).join('')}
             </tbody></table>`
          : `<p class="help">Its app list was read (${(m.apps||[]).length}
             app${(m.apps||[]).length===1?'':'s'}) and <b>no platform gains a
             route from it</b>. ROMarr only counts an app whose name is an
             emulator for exactly one machine &mdash; PCSX2, Dolphin, flycast.
             A RetroArch or a Steam proves nothing, because nothing in the API
             says which cores or games are inside it.</p>`)
      : `<p class="help"><b>Reachable, but its app list could not be read</b>,
         so ROMarr cannot say which platforms it plays and does not guess.
         ${esc(m.apps_problem||'')}</p>`}
    ${(m.apps||[]).length?`<p class="help">Apps: ${
      (m.apps||[]).map(a=>'<span class="pill">'+esc(a)+'</span>').join(' ')}</p>`:''}

    <h4 style="margin:18px 0 6px">Pairing</h4>
    <p class="help">${esc((m.pairing||{}).manual||'')}</p>
    ${canList
      ? (pend.length
          ? `<p class="help">${pend.length} client${pend.length===1?'':'s'}
             waiting:</p>`
          : `<p class="help">No client is waiting to pair right now. Open
             Moonlight, add <code>${esc(m.host||'')}</code>, and select it
             &mdash; then come back here with the PIN it shows you.</p>`)
      : `<p class="help">${esc((m.pairing||{}).detail||'')}</p>`}
    <div class="row" style="flex-wrap:wrap;gap:8px;align-items:center">
      ${canList&&pend.length
        ? `<select id="ml-secret">${pend.map(p=>
            `<option value="${esc(p.pair_secret)}">${esc(p.client_ip||p.pair_secret)}</option>`
          ).join('')}</select>`
        : ''}
      <input id="ml-pin" placeholder="PIN from your Moonlight client"
             maxlength="8" style="max-width:230px" ${canList&&!pend.length?'disabled':''}>
      <button class="btn" id="ml-send" ${canList&&!pend.length?'disabled':''}>Send PIN to host</button>
    </div>
    <div id="ml-msg" class="testline"></div>

    <h4 style="margin:18px 0 6px">Connecting</h4>
    <p class="help">ROMarr cannot start the stream for you. Launching is
    behind the same paired client certificate as the app list, on the host's
    HTTPS port, and there is no <code>moonlight://</code> link a browser can
    hand off &mdash; Moonlight registers no URL scheme. What does work:</p>
    <p class="help">Add <code>${esc(m.host||'')}</code> in the Moonlight app,
    or run <code>${esc(m.connect_hint||'')}</code> on a desktop.</p>
    ${m.desktop_url?`<p class="help">This host also serves a browser desktop:
      <a href="${esc(m.desktop_url)}" target="_blank" rel="noreferrer noopener"
      >${esc(m.desktop_url)}</a></p>`:''}
    </div>`;
};

// Wired after every status render, and defensive about the card being absent:
// it is not drawn at all when no host is configured, and this runs regardless.
const wireMoonlight=m=>{
  const send=$('#ml-send'); if(!send) return;
  send.onclick=async()=>{
    const out=$('#ml-msg');
    const pin=($('#ml-pin')||{}).value||'';
    const sec=($('#ml-secret')||{}).value||'';
    out.className='testline'; out.textContent='Sending…';
    const r=await j('/api/v1/moonlight/pin',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pin:pin,pair_secret:sec})}).catch(()=>({ok:false,
        detail:'ROMarr could not reach the host'}));
    // Never "Paired". Neither Wolf nor Sunshine reports whether the PIN was
    // right on this call -- Sunshine returns true for a wrong PIN whenever a
    // request is outstanding (LizardByte/Sunshine#3944) -- so the strongest
    // honest word is "accepted", and the check is the paired-client count.
    out.className='testline '+(r.ok?'ok':'bad');
    out.textContent=r.detail||(r.ok?'Accepted.':'Refused.');
    if(r.ok) setTimeout(()=>RENDER.status(),1500);
  };
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
    <div><b>${r.stream||0}</b><span>Streamed server-side</span></div>
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
  const conns=await j('/api/v1/connection').catch(()=>({items:[]}));
  const have=conns.items||[];
  const types=schema.list||schema.types||[];
  $('#page').innerHTML='<div class="card"><h3>Connections</h3>'
    +'<p class="help">Where ROMarr tells you what it did. A grab notification '
    +'carries the reasons the release was chosen, not just its name.</p>'
    +(have.length
      ?'<table><thead><tr><th>Name</th><th>Type</th><th>Events</th><th></th></tr></thead><tbody>'
       +have.map((c,i)=>'<tr><td><b>'+esc(c.name||c.type)+'</b></td>'
         +'<td><span class="pill">'+esc(c.type)+'</span></td>'
         +'<td>'+esc((c.events||['all']).join(', '))+'</td>'
         +'<td style="text-align:right"><div class="rowact">'
         +'<button data-cedit="'+i+'">Edit</button></div></td></tr>').join('')
       +'</tbody></table>'
      :'<div class="empty-cat"><b>No connections yet</b>Add one — this is '
       +'where the Discord webhook goes.</div>')
    +'<div class="row" style="margin-top:14px">'
    +'<button class="btn" id="c-add">Add Connection</button>'
    +'<button class="btn ghost" id="ctest">Send a test notification</button>'
    +'<span id="ctestmsg" class="help" style="margin:0"></span></div></div>'

    +'<div class="card"><h3>Available providers</h3>'
    +'<div class="pgrid">'+types.map(t=>
      '<div class="pcard"><h4>'+esc(t.label)+'</h4>'
      +'<div class="desc">'+esc(t.help)+'</div>'
      +'<div class="meta">'+(t.fields||[]).map(f=>'<span class="pill">'+esc(f)+'</span>').join(' ')+'</div>'
      +'</div>').join('')+'</div></div>';

  $('#c-add').onclick=()=>addItem('connection');
  document.querySelectorAll('[data-cedit]').forEach(b=>b.onclick=()=>
    editItem('connection', have[Number(b.dataset.cedit)]));
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
  const mine=await j('/api/v1/metadataprovider').catch(()=>({items:[]}));
  const schema=await j('/api/v1/metadata/schema').catch(()=>({providers:[]}));
  const probe=await j('/api/v1/metadata/lookup?filename='
    +encodeURIComponent('Chrono.Trigger.USA.v1.1.smc')).catch(()=>({}));
  $('#page').innerHTML='<div class="card"><h3>Configured providers</h3>'
    +'<p class="help"><b>This is where the keys go.</b> Add RAWG (a free key) '
    +'or IGDB (a Twitch client id + bearer token) and lookups, Discover and '
    +'the Calendar light up.</p>'
    +'<div class="row" style="margin-bottom:10px">'
    +'<button class="btn" id="mp-add">Add Provider</button></div>'
    +((mine.items||[]).length
      ?'<table><thead><tr><th>Name</th><th>Type</th><th>Enabled</th><th></th></tr></thead><tbody>'
        +mine.items.map((m,i)=>'<tr><td><b>'+esc(m.name||m.type)+'</b></td>'
          +'<td>'+esc(m.type)+'</td>'
          +'<td><span class="dot '+(m.enable!==false?'up':'down')+'"></span></td>'
          +'<td style="text-align:right"><div class="rowact">'
          +'<button data-mpedit="'+i+'">Edit</button></div></td></tr>').join('')
        +'</tbody></table>'
      :'<div class="empty">No provider configured yet — covers, Discover and '
       +'the Calendar stay dark until one is.</div>')
    +'</div>'
    +'<div class="card"><h3>What each provider needs</h3>'
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
  $('#mp-add').onclick=()=>addItem('metadataprovider');
  document.querySelectorAll('[data-mpedit]').forEach(b=>b.onclick=()=>
    editItem('metadataprovider', mine.items[Number(b.dataset.mpedit)]));
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
// The Calendar reads YOUR library. It used to ask a metadata provider for a
// release schedule, which meant a fully-catalogued 166k-game install rendered
// "add a metadata provider with an API key" and nothing else, forever — while
// the library server sat there holding a release date, an added date and an
// updated date for every row. The provider calendar is still here, below the
// real one, for whoever has a key.
let CAL={view:'releases',month:'',day:''};

RENDER.calendar=async()=>{
  const q=new URLSearchParams({view:CAL.view,limit:'120'});
  if(CAL.month) q.set('month',CAL.month);
  if(CAL.day) q.set('day',CAL.day);
  const d=await j('/api/v1/calendar/library?'+q)
    .catch(()=>({items:[],days:[],error:'the library could not be read'}));
  const views=[['releases','Anniversaries'],['added','Added'],
               ['updated','Updated'],['upcoming','Upcoming']];
  const items=d.items||[], days=d.days||[];
  const cov=d.coverage||{};
  // The share of the shelf this view can place at all. A calendar drawn over
  // 39% of a library that presents itself as the whole library is how
  // somebody concludes they own nothing from 1993.
  const covLine=cov.total
    ? fmt(cov.dated)+' of '+fmt(cov.total)+' games carry a '
      +({released:'release date',added:'date added',
         updated:'date updated'}[cov.field]||'date')
      +' — the rest were never identified, so they cannot be placed on a day.'
    :'';
  const step=n=>{
    const [y,m]=(d.month||'').split('-').map(Number);
    if(!y) return '';
    const t=new Date(Date.UTC(y,m-1+n,1));
    return t.getUTCFullYear()+'-'+String(t.getUTCMonth()+1).padStart(2,'0');
  };
  const busiest=days.reduce((a,x)=>Math.max(a,x.count),0);

  $('#page').innerHTML='<div class="card">'
    +'<div class="row" style="margin-bottom:10px;gap:6px;flex-wrap:wrap">'
    +views.map(([k,l])=>'<button class="btn '+(k===d.view?'':'ghost')+'"'
      +' data-calview="'+k+'" style="padding:4px 10px;font-size:12px">'
      +l+'</button>').join('')
    +'</div>'
    +(d.error?'<div class="panel-note panel-warn">'+esc(d.error)+'</div>':'')
    +(d.note?'<p class="help">'+esc(d.note)+'</p>':'')
    +(d.view!=='upcoming'&&d.month
      ?'<div class="row" style="align-items:center;gap:8px;margin:4px 0 8px">'
        +'<button class="btn ghost" data-calmonth="'+step(-1)+'">‹</button>'
        +'<b style="font-size:14px">'+esc(d.month_name||'')+' '
        +esc((d.month||'').slice(0,4))+'</b>'
        +'<button class="btn ghost" data-calmonth="'+step(1)+'">›</button>'
        +'<span style="flex:1"></span>'
        +'<span class="help" style="margin:0">'+esc(covLine)+'</span></div>'
      +'<div class="cal">'
      +['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
        .map(n=>'<div class="dow">'+n+'</div>').join('')
      +Array.from({length:d.first_weekday||0},
                  ()=>'<button class="pad" disabled></button>').join('')
      +days.map(x=>'<button data-calday="'+x.day+'" '
        +(x.count?'':'disabled ')
        +'class="'+(x.count?'':'none ')
        +(x.day===d.selected_day?'on':'')+'">'
        +'<span>'+x.day+'</span>'
        +(x.count?'<b>'+fmt(x.count)+'</b>':'')+'</button>').join('')
      +'</div>'
      +(busiest?'':'<p class="help">Nothing in this month. Page back — a '
        +'library imported in one batch has its dates where the imports were.</p>')
      :'')
    +'<h3 style="margin-top:14px">'
    +esc(d.view==='upcoming'?'Releasing soon'
        :d.view==='releases'?('Released on '+(d.day||''))
        :((d.view==='added'?'Added ':'Updated ')+(d.day||'')))
    +'</h3>'
    +(items.length
      ?'<p class="help">'+fmt(d.items.length)+' from your own library.</p>'
       +'<div class="grid">'+items.map(g=>'<div class="tile">'
        +'<div class="art" style="background-image:url(\''+esc(g.cover_url||'')+'\')"></div>'
        +'<div class="nm">'+esc(g.title)+'</div>'
        +'<div class="pf">'+esc(g.platform||'')
        +(g.year?' · '+g.year:'')+(g.rating?' · ★'+g.rating:'')
        +'</div></div>').join('')+'</div>'
      :'<div class="empty">'+esc(d.view==='upcoming'
        ?'Nothing dated in the future.'
        :'Nothing on this day.')+'</div>')
    +'</div>'
    +'<div class="card" style="margin-top:14px"><h3>Elsewhere</h3>'
    +'<p class="help" id="cal-web-note">A metadata provider can answer about '
    +'games nobody here owns — everything above needs nothing at all.</p>'
    +'<div id="cal-web"><div class="empty">Checking…</div></div>'
    +'</div>';

  document.querySelectorAll('[data-calview]').forEach(b=>b.onclick=()=>{
    CAL.view=b.dataset.calview; CAL.month=''; CAL.day=''; go('calendar');});
  document.querySelectorAll('[data-calmonth]').forEach(b=>b.onclick=()=>{
    CAL.month=b.dataset.calmonth; CAL.day=''; go('calendar');});
  document.querySelectorAll('[data-calday]').forEach(b=>b.onclick=()=>{
    CAL.day=b.dataset.calday; go('calendar');});

  // Second, and separately: a provider calendar shows a request button
  // because those are games you do not have. Nothing above does — you
  // already own everything on it.
  //
  // On the Upcoming view the window loses its backward half. The library
  // half of that view can only ever say "you own nothing unreleased", which
  // is true and is the point; pairing it with a provider list that is mostly
  // last month's releases answers a question nobody asked.
  //
  // back=-1 rather than back=0, because a window that opens today opens on
  // today's releases — forty of them on a busy day, every one of them
  // already out, filling a shelf headed "still to come".
  const w=await j('/api/v1/calendar'+(d.view==='upcoming'?'?back=-1':''))
    .catch(()=>({items:[],error:'unavailable'}));
  const web=w.items||[];
  const wsrc=w.provider_label||'a metadata provider';
  if(web.length) $('#cal-web-note').textContent=fmt(web.length)+' title(s) from '
    +wsrc+"'s catalogue, "+(d.view==='upcoming'?'still to come':'either side of today')
    +' — none of these are rows in your library, which is why they have a '
    +'Request button and nothing above does.';
  $('#cal-web').innerHTML=web.length
    ?'<div class="pgrid">'+web.map((g,n)=>'<div class="pcard">'
      +'<h4>'+esc(g.title)+'</h4>'
      +'<div class="meta"><span class="pill">'+esc(g.released||'TBA')+'</span>'
      +(g.upcoming?'<span class="pill" style="background:var(--info);color:#06131f">upcoming</span>':'')
      +'<span class="pill">'+esc(g.source||wsrc)+'</span>'
      +'</div>'
      +'<div class="desc">'+esc((g.platforms||[]).slice(0,4).join(', '))+'</div>'
      +'<div class="rowact" style="margin-top:6px"><button data-calreq="'+n+'">Request</button></div>'
      +'</div>').join('')+'</div>'
    :'<div class="empty">'+esc(w.error||'No provider configured.')+'</div>';
  document.querySelectorAll('[data-calreq]').forEach(b=>b.onclick=()=>
    requestGame(web[Number(b.dataset.calreq)]));
};

// Shared by Calendar and Discover: turn a metadata title into a Wanted
// request, resolving the store's platform names to a ROMarr platform.
async function requestGame(g){
  const names=(g.platforms||[]);
  const match=PLATFORMS.find(p=>names.some(n=>
    p.name.toLowerCase()===String(n).toLowerCase()||p.slug===String(n).toLowerCase()));
  let slug=match?match.slug:'';
  if(!slug){
    const pick=prompt('Platform for "'+g.title+'"?\n'
      +PLATFORMS.map(p=>p.slug).join(', '),'');
    const m=PLATFORMS.find(p=>p.slug===(pick||'').toLowerCase()
      ||p.name.toLowerCase()===(pick||'').toLowerCase());
    if(!m){toast('Need a known platform');return;}
    slug=m.slug;
  }
  const r=await j('/api/request',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({game:g.title,platform:slug})});
  toast(r.ok?('Grabbed '+(r.release||g.title)):(r.error||'Added to Wanted'));
  refreshCounts();
}

// --- Manual Import ---------------------------------------------------------
// Radarr calls this Manual Import and it exists for the same reason: somebody
// arrives with a library already on disk, and telling them to re-download
// everything ROMarr could have adopted is absurd.
// The optional guide uses the same editors and connection checks as Settings.
const SETUP={step:0,mode:'organize',initialized:false};
RENDER.setup=async()=>{
  const state=await j('/api/v1/setup/status').catch(()=>null);
  if(!state||state.error){
    $('#page').innerHTML='<div class="card"><h3>Setup Guide</h3><p class="help">We could not read your setup. Refresh this page to try again.</p></div>';
    return;
  }
  if(!SETUP.initialized){SETUP.mode=state.mode||'organize';SETUP.initialized=true;}
  const titles=['Start here','Your library','Downloads','Search sources','Optional extras','Your next step'];
  const order=SETUP.mode==='organize'?[0,1,4,5]:[0,1,2,3,4,5];
  const active=SETUP.step;
  const added=rows=>rows.length?'<ul style="padding-left:20px;margin:12px 0">'
    +rows.map(row=>'<li>'+esc(row.name)+(row.enabled?'':' <span class="help">(disabled)</span>')+'</li>').join('')+'</ul>':'';
  const action=(id,label)=>'<button class="btn ghost" id="'+id+'">'+label+'</button>';
  let content='';
  if(active===0){
    content='<h3>Welcome to ROMarr</h3><p class="help">Choose what you want to do first. You can change your mind and add more connections later.</p>'
      +'<div class="pick"><button data-setup-mode="organize" aria-pressed="'+(SETUP.mode==='organize')+'">I already have ROMs<small>Connect a folder or library, preview existing files, and check what is missing. No indexer or download client needed.</small></button>'
      +'<button data-setup-mode="download" aria-pressed="'+(SETUP.mode==='download')+'">Find and download games<small>Connect your library, a download client, and a search source.</small></button></div>'
      +'<p class="help" style="margin-top:16px">ROMarr organizes and acquires files. You play them in a library frontend or emulator of your choice.</p>';
  }else if(active===1){
    content='<h3>Where should your ROMs live?</h3><p class="help">Use a plain folder, or connect RomM, Gaseous, Retrom, or another supported library. Your existing files stay in place when you add a library.</p>'
      +added(state.libraries)
      +'<div class="row">'+action('setup-folder','Use a folder')+action('setup-library','Connect a library server')+'</div>'
      +'<p class="help">Choose the path ROMarr can see, such as <code>/roms</code>. If you use Docker, mount your ROM folder into the container first. Use <b>Test</b> in the form to check the path before saving.</p>'
      +'<p class="help">You can preview files in <a href="#manualimport">Manual Import</a> before copying them into your library. Review the platform and destination first. DAT-verified single files can use the canonical name; multi-file disc sets keep their referenced filenames.</p>';
  }else if(active===2){
    content='<h3>Choose what downloads your files</h3><p class="help">Use qBittorrent for torrents, or SABnzbd/NZBGet for Usenet. ROMarr hands your chosen release to that app, then imports it when the download finishes.</p>'
      +added(state.download_clients)+action('setup-client','Add a download client')
      +'<p class="help">Enter the address ROMarr can reach. In separate Docker containers, <code>localhost</code> points back to ROMarr; use your download client’s container name or server address. Press <b>Test</b> before saving.</p>'
      +'<p class="help">Your finished-download folder must also be readable by ROMarr. Different paths on the two apps can be connected later under <a href="#media">Media Management → Remote path mappings</a>.</p>';
  }else if(active===3){
    content='<h3>Where should ROMarr search?</h3><p class="help">An indexer lists available releases. Connect an existing Prowlarr instance, or add a Torznab/Newznab source directly. A download client alone does not provide search results.</p>'
      +added(state.indexers)+'<div class="row">'+action('setup-prowlarr','Connect Prowlarr')+action('setup-indexer','Add another indexer')+'</div>'
      +'<p class="help">Use the source’s address and API key, then press <b>Test</b>. ROM Hub plugins are another optional way to add supported sources; you can explore those next.</p>';
  }else if(active===4){
    content='<h3>Add confidence and more sources</h3><p class="help">Both of these are optional. You can start with your existing files and return later.</p>'
      +'<h4>Verify with DATs</h4><p class="help">A No-Intro or Redump DAT is a checklist of known file hashes. It lets ROMarr verify a dump and compare your collection with a complete set.</p>'
      +(state.dat_loaded?'<p class="help"><b>'+fmt(state.dat_loaded)+' DAT(s) loaded</b> · '+fmt(state.dat_games)+' games</p>':'')
      +'<div class="field"><label for="setup-dat-path">Folder containing your DAT files</label><input id="setup-dat-path" value="'+esc(state.dat_path)+'" placeholder="/dats"></div>'
      +action('setup-dats','Check and load DATs')+'<div id="setup-dat-result" role="status" class="testline"></div>'
      +'<h4 style="margin-top:22px">Explore ROM Hub</h4><p class="help">ROM Hub is the plugin catalogue. Plugins can add supported search sources and download tools. Open a plugin’s details to check what it needs before installing.</p>'
      +'<a class="btn ghost" href="#hub">Browse ROM Hub plugins</a>'
      +(!state.hub_available?'<p class="help">ROM Hub is not available in this installation. The Plugins page explains how to add it; your folder and indexer setup can still work.</p>':'');
  }else{
    content='<h3>Choose your first small task</h3><p class="help">Start with one game so you can see each step. You can reopen this guide from <b>Settings → Setup Guide</b> whenever you need it.</p>'
      +(SETUP.mode==='organize'
        ?'<p><a class="btn" href="#manualimport">Preview existing ROMs</a></p><p class="help">Manual Import shows what ROMarr recognizes before you import. For missing games and DAT comparison, open <a href="#collections">Collections</a>. Exact duplicate cleanup starts with an audit under <a href="#stats">Stats</a>.</p>'
        :'<p><a class="btn" href="#search">Preview a game search</a></p><p class="help">Interactive Search shows the available releases before you choose one. Follow its progress in <a href="#queue">Queue</a>. If you prefer to start with files you already have, use <a href="#manualimport">Manual Import</a>.</p>')
      +'<h4 style="margin-top:22px">Want a dedicated player?</h4><p class="help">Cartridge is an optional companion for a RomM library. Choose the version for your device; ROMarr works without it.</p>'
      +'<div class="row" style="flex-wrap:wrap"><a class="btn ghost" href="https://github.com/BlizzHacker/romarr/releases/download/v1.8.0/Cartridge-Windows-Portable-0.2.0-x64.exe" target="_blank" rel="noopener">Cartridge for Windows</a><a class="btn ghost" href="https://apps.microsoft.com/detail/9MXC51W17LH4" target="_blank" rel="noopener">Cartridge for Xbox</a></div>'
      +'<p class="help">Need help? <a href="https://github.com/BlizzHacker/romarr/issues" target="_blank" rel="noopener">Open a GitHub issue</a> with your ROMarr version, what you expected, and the relevant error. Remove passwords and API keys from logs.</p>';
  }
  $('#page').innerHTML='<div class="card" style="max-width:900px"><div class="row" style="justify-content:space-between"><span class="help">Setup Guide</span><button class="btn ghost" id="setup-dismiss">Set up later</button></div>'
    +'<div class="row" style="flex-wrap:wrap;gap:6px;margin:12px 0 24px">'+order.map((step,n)=>
      '<button class="chip '+(step===active?'on':'')+'" data-setup-step="'+step+'">'+(n+1)+'. '+titles[step]+'</button>').join('')+'</div>'
    +content+'<div class="row" style="margin-top:24px;justify-content:space-between">'
    +'<button class="btn ghost" id="setup-back" '+(active===0?'disabled':'')+'>Back</button>'
    +'<span class="row">'+(active>0&&active<5?'<button class="btn ghost" id="setup-skip">Skip this step</button>':'')
    +'<button class="btn" id="setup-next">'+(active===5?'Finish guide':'Continue')+'</button></span></div></div>';
  const finish=async()=>{
    const result=await j('/api/v1/setup/complete',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:SETUP.mode})}).catch(()=>({error:'Could not save. Please try again.'}));
    if(result.error){toast(result.error);return;}
    go('library');
  };
  $('#setup-dismiss').onclick=finish;
  $('#setup-back').onclick=()=>{SETUP.step=order[Math.max(0,order.indexOf(active)-1)];RENDER.setup();};
  const next=()=>{if(active===5){finish();return;}SETUP.step=order[order.indexOf(active)+1];RENDER.setup();};
  $('#setup-next').onclick=next;
  const skip=$('#setup-skip');if(skip)skip.onclick=next;
  document.querySelectorAll('[data-setup-step]').forEach(btn=>btn.onclick=()=>{
    SETUP.step=Number(btn.dataset.setupStep);RENDER.setup();});
  document.querySelectorAll('[data-setup-mode]').forEach(btn=>btn.onclick=()=>{
    SETUP.mode=btn.dataset.setupMode;RENDER.setup();});
  const bind=(id,fn)=>{const button=$('#'+id);if(button)button.onclick=fn;};
  const saved=()=>RENDER.setup();
  bind('setup-folder',()=>editItem('library',{type:'folder',name:'My ROMs',path:state.library_path,is_default:true},saved));
  bind('setup-library',()=>addItem('library',saved));
  bind('setup-client',()=>addItem('downloadclient',saved));
  bind('setup-prowlarr',()=>editItem('indexer',{type:'prowlarr'},saved));
  bind('setup-indexer',()=>addItem('indexer',saved));
  bind('setup-dats',async()=>{
    const button=$('#setup-dats'),out=$('#setup-dat-result');
    button.disabled=true;out.textContent='Checking your DAT folder…';out.className='testline';
    const result=await j('/api/v1/setup/dats',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({directory:$('#setup-dat-path').value})})
      .catch(()=>({error:'Could not check that folder. Please try again.'}));
    out.className='testline '+(result.ok?'ok':'bad');out.textContent=result.message||result.error;
    button.disabled=false;
  });
};

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
    '<div class="card"><h3>What ROMarr is</h3><p><a class="btn" href="#setup">Open the Setup Guide</a></p>'
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
  const sourceState=await j('/api/v1/collection/source').catch(()=>({torrents:[],jobs:[]}));
  const sourceTorrents=sourceState.torrents||[];
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
       +'<label class="help" style="margin:0" title="Treat the region list as an allowlist: omit a title when no dump matches one of your regions">'
       +'<input type="checkbox" id="cstrict"'+((SETTINGS.region_mode||'prefer')==='strict'?' checked':'')+'> '
       +'Only selected regions</label>'
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
         ['fill','fill gaps (a selected-language translation only where no preferred region exists)'],
         ['prefer','prefer (a selected-language translation over the region winner)'],
         ['keep_both','keep both (original + translation under Translations/)']].map(o=>
          '<option value="'+o[0]+'"'+((SETTINGS.translation_policy||'exclude')===o[0]?' selected':'')
          +'>'+o[1]+'</option>').join('')
       +'</select>'
       +'<label class="help" style="margin:0">languages</label>'
       +'<input id="ctranslangs" placeholder="'+esc((SETTINGS.translation_langs||['en']).join(', '))+'" '
       +'value="'+esc((SETTINGS.translation_langs||['en']).join(', '))+'" '
       +'style="width:110px;padding:6px 8px;background:var(--bg);color:var(--fg);'
       +'border:1px solid var(--line);border-radius:6px" title="Comma-separated codes, e.g. de, en">'
       +'</div>'
      :'<div class="empty-cat"><b>No DAT loaded</b>'
       +'Point DAT_PATH at a directory of No-Intro or Redump DATs. Without one '
       +'there is no list of what a complete set contains.</div>')
    +'<div id="cres" style="margin-top:16px"></div></div>'
    +'<div class="card" style="margin-top:16px"><h3>Acquire missing files from a set torrent</h3>'
    +(sourceState.errors||[]).map(error=>'<div class="panel-note panel-warn">'+esc(error)+'</div>').join('')
    +(sourceTorrents.length&&dats.length
      ?'<p class="help">Preview a DAT plan against one existing qBittorrent torrent. ROMarr selects only matching missing files, stages one budgeted batch at a time, verifies each staged set against the DAT, then restores the torrent’s original file priorities and paused state. The cap limits ROMarr staging copies; downloaded torrent files remain in qBittorrent and need separate source storage.</p>'
       +'<div class="row" style="flex-wrap:wrap">'
       +'<select id="csrc-torrent">'+sourceTorrents.map(t=>
         '<option value="'+esc(t.hash)+'" data-client="'+esc(t.client_id)+'">'
         +esc(t.client_name+' · '+t.name+' · '+t.state)+'</option>').join('')+'</select>'
       +'<label class="help">Staging cap (GiB) <input id="csrc-cap" type="number" min="0.01" step="1" value="100" style="width:90px"></label>'
       +'<button class="btn" id="csrc-plan">Preview source plan</button></div>'
       +'<div id="csrc-out" style="margin-top:10px"></div>'
      :(sourceTorrents.length
        ?'<p class="help">Load a DAT before previewing missing collection files.</p>'
        :'<p class="help">Enable a qBittorrent download client to use this workflow.</p>'))
    +'<div id="csrc-jobs" style="margin-top:10px"></div></div>'
    +'<div class="card" style="margin-top:16px"><h3>Batches</h3>'
    +'<div id="cbatch"></div></div>';

  const drawBatches=async()=>{
    const s=await j('/api/v1/collection').catch(()=>({batches:[]}));
    const cb=$('#cbatch'); if(!cb) return;   // left the Collections page
    const b=s.batches||[];
    cb.innerHTML=b.length
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

    cb.querySelectorAll('.cstep').forEach(el=>el.onclick=async e=>{
      const id=e.target.closest('tr').dataset.id;
      e.target.disabled=true; e.target.textContent='Running…';
      await j('/api/v1/collection/step',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
      drawBatches();
    });
    cb.querySelectorAll('.cact').forEach(el=>el.onclick=async e=>{
      const id=e.target.closest('tr').dataset.id, action=e.target.dataset.a;
      if(action==='cancel'&&!confirm('Cancel this set? Titles already requested are unaffected.')) return;
      await j('/api/v1/collection/control',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id,action:action})});
      drawBatches();
    });
  };
  drawBatches();

  const drawSourceJobs=async()=>{
    const node=$('#csrc-jobs'); if(!node)return;
    const state=await j('/api/v1/collection/source').catch(()=>({jobs:[]}));
    const jobs=state.jobs||[];
    node.innerHTML=jobs.length?'<h4>Selective torrent jobs</h4>'+jobs.map(x=>
      '<div class="row" style="justify-content:space-between;border-top:1px solid var(--line);padding:7px 0">'
      +'<span class="help">'+esc(x.torrent_name)+' · '+esc(x.status)+' · batch '
      +(x.batch||1)+'/'+(x.batches||1)+' · '+(x.done||0)+'/'+(x.total||0)+'</span>'
      +(x.status==='running'?'<span class="row"><button class="btn ghost csrc-step" data-id="'
       +esc(x.id)+'">Run next</button><button class="btn ghost csrc-cancel" data-id="'
       +esc(x.id)+'">Cancel</button></span>'
       :x.status==='restore-needed'?'<button class="btn ghost csrc-restore" data-id="'
       +esc(x.id)+'">Restore torrent priorities</button>':'')
      +'</div>').join(''):'';
    node.querySelectorAll('.csrc-step').forEach(btn=>btn.onclick=async()=>{
      btn.disabled=true;
      const r=await j('/api/v1/collection/source/step',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({id:btn.dataset.id})});
      if(r.error)toast(r.error);
      drawSourceJobs();
    });
    node.querySelectorAll('.csrc-restore').forEach(btn=>btn.onclick=async()=>{
      btn.disabled=true;
      const r=await j('/api/v1/collection/source/control',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:btn.dataset.id,action:'restore'})});
      if(r.error)toast(r.error);
      drawSourceJobs();
    });
    node.querySelectorAll('.csrc-cancel').forEach(btn=>btn.onclick=async()=>{
      if(!confirm('Cancel acquisition and restore the torrent’s original file priorities?'))return;
      btn.disabled=true;
      const r=await j('/api/v1/collection/source/control',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:btn.dataset.id,action:'cancel'})});
      if(r.error)toast(r.error);
      drawSourceJobs();
    });
  };
  drawSourceJobs();
  const sourceSelect=$('#csrc-torrent');
  const sourcePlanButton=$('#csrc-plan');
  const sourcePlatform=$('#cplat');
  const invalidateSourcePreview=()=>{
    const out=$('#csrc-out');
    if(out)out.innerHTML='<p class="help">Selection changed. Preview the source plan again before starting.</p>';
  };
  [sourcePlatform,$('#cdat'),sourceSelect,$('#csrc-cap')].filter(Boolean)
    .forEach(input=>input.addEventListener('change',invalidateSourcePreview));
  if(sourceSelect&&sourcePlanButton)sourcePlanButton.onclick=async()=>{
    const opt=sourceSelect.selectedOptions[0];
    const cap=Math.floor((parseFloat($('#csrc-cap').value)||0)*1024*1024*1024);
    const request={client_id:opt.dataset.client,torrent_hash:opt.value,
      dat:$('#cdat').value,platform:$('#cplat').value,cap_bytes:cap};
    const out=$('#csrc-out');
    out.innerHTML='<div class="empty">Reading source file list…</div>';
    const d=await j('/api/v1/collection/source/plan',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
    if(d.error){out.innerHTML='<div class="panel-note panel-warn">'+esc(d.error)+'</div>';return;}
    const groups=(d.batches||[]).flatMap((b,n)=>
      (b.groups||[]).map(g=>'<li>Batch '+(n+1)+': '+esc(g.title)+' · '
       +g.files.length+' file(s) · '+fmtBytes(g.size)+'</li>')).join('');
    out.innerHTML='<p class="help">'+d.matched_titles+' missing title(s) matched in '
      +(d.batches||[]).length+' batch(es); '+fmtBytes(d.total_bytes)
      +' staged payload plus '+fmtBytes(d.reserve_bytes)+' piece reserve.</p>'
      +'<ul>'+groups+'</ul>'
      +(d.skipped||[]).map(x=>'<p class="help">'+esc(x.title)+': '+esc(x.reason)+'</p>').join('')
      +((d.unmatched||[]).length?'<details><summary class="help">Unmatched titles ('+
        d.unmatched.length+')</summary><div class="help">'+d.unmatched.map(x=>
        esc(x.title)+' — '+esc(x.reason)).join('<br>')+'</div></details>':'')
      +'<button class="btn" id="csrc-start"'+(sourcePlatform.value.trim()?'':' disabled')+'>Start selective acquisition</button>';
    const start=$('#csrc-start'); if(!start)return;
    start.onclick=async()=>{
      if(request.platform!==sourcePlatform.value.trim()
          ||request.dat!==$('#cdat').value){invalidateSourcePreview();return;}
      if(!request.platform){start.disabled=true;return;}
      if(!confirm('Start this selective torrent workflow? ROMarr will temporarily change the selected torrent’s file priorities and restore their original values when the job ends or is cancelled.'))return;
      start.disabled=true;
      const r=await j('/api/v1/collection/source/start',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
      if(r.error){toast(r.error);start.disabled=!request.platform;return;}
      toast('Selective acquisition started');
      drawSourceJobs();
    };
  };

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
      +'&region_mode='+($('#cstrict').checked?'strict':'prefer')
      +'&translation_policy='+encodeURIComponent($('#ctrans').value)
      +'&translation_langs='+encodeURIComponent($('#ctranslangs').value.trim())
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
      +((c.region_omitted||0)?tile(c.region_omitted,'omitted — no dump in your regions','var(--dim)'):'')
      +'</div>'
      +'<p class="help">'+esc(d.dat)+(d.dat_version?' ('+esc(d.dat_version)+')':'')
      +' — region order '+esc((d.policy.regions||[]).join(' → '))
      +(d.policy.region_mode==='strict'?' (strict: titles without a matching-region dump are omitted)':'')+'.</p>'
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
          region_mode:$('#cstrict').checked?'strict':'prefer',
          translation_policy:$('#ctrans').value,
          translation_langs:($('#ctranslangs').value.trim()||'').split(',').map(s=>s.trim()).filter(Boolean),
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
  const manualOnly=[['RefreshLibrary','Re-read the library from RomM',null],
    ['Decompress','Batch decompress the library: extract via Compressatorium, '
     +'verify every output against the DATs. Originals are deleted only when '
     +'they verified AND the API call asked for deletion — this button never '
     +'deletes. Needs COMPRESSATORIUM_URL and decompress_path set in '
     +'settings.',null]];
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
    // Decompress from the Tasks page is always a dry run: extraction is
    // minutes-to-hours of service work and a mis-click must not start it on
    // the whole library. The real run is an API call with an explicit
    // directory -- and only that call can authorise deletion.
    const body={name:b.dataset.task};
    if(b.dataset.task==='Decompress') body.dry_run=true;
    const r=await j('/api/v1/command',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify(body)});
    toast(r.message||(r.scanned!==undefined?`Scanned ${r.scanned} (dry run)`:'Done'));
    go('tasks'); refreshCounts();
  });
};

let LOG_TIMER=null;
RENDER.update=async()=>{
  const d=await j('/api/v1/update').catch(()=>null);
  if(!d){$('#page').innerHTML='<div class="panel-note panel-warn">Could not read update status.</div>';return;}
  const inst=d.install||{}, st=d.settings||{};
  const kindLabel={git:'git checkout (self-update possible)',
    docker:'container image (updates come from the host)',
    none:'unknown install shape'}[inst.kind]||inst.kind;
  const badge=(on)=>on
    ?'<span class="pill" style="background:var(--ok);color:#08210d">ON</span>'
    :'<span class="pill">off</span>';
  const plugins=Object.entries(d.plugins||{});
  $('#page').innerHTML=
    '<div class="card"><h3>This install</h3>'
    +'<div class="st">'
    +'<div><b>'+esc(d.running_version)+'</b><span>Running</span></div>'
    +'<div><b>'+esc(d.latest_version||'—')+'</b><span>Latest on GitHub</span></div>'
    +'<div><b>'+(d.update_available?'<span style="color:var(--warn)">yes</span>':'no')+'</b><span>Update available</span></div>'
    +'</div>'
    +'<p class="help">Install shape: <b>'+esc(kindLabel)+'</b>'
    +(inst.branch?' — branch <code>'+esc(inst.branch)+'</code> at <code>'+esc(inst.commit||'')+'</code>':'')+'</p>'
    +'</div>'
    +'<div class="card"><h3>Automatic updates</h3>'
    +'<p class="help">Settings live under Settings → General; this page shows and acts. '
    +'Auto-update waits until nothing is downloading, then fetches, fast-forwards '
    +'and restarts. A container install cannot update itself — the host pulls the new image.</p>'
    +'<table><tbody>'
    +'<tr><td>Daily update check</td><td>'+badge(st.update_check)+'</td></tr>'
    +'<tr><td>Auto-apply ROMarr updates</td><td>'+badge(st.auto_update)+'</td></tr>'
    +'<tr><td>Channel</td><td>'+esc(st.update_channel||'stable')+'</td></tr>'
    +'<tr><td>Auto-update ROM Hub plugins</td><td>'+badge(st.auto_update_plugins)+'</td></tr>'
    +'</tbody></table>'
    +'<div class="row" style="gap:8px;margin-top:10px">'
    +'<button class="btn ghost" id="up-check">Check now</button>'
    +(inst.can_apply?'<button class="btn" id="up-apply">Update now</button>':'')
    +(plugins.length?'<button class="btn ghost" id="up-plugins">Update plugins now</button>':'')
    +'</div>'
    +'<div id="up-msg" style="margin-top:10px"></div>'
    +'</div>'
    +((d.docker_guidance||[]).length
      ?'<div class="card"><h3>How to update this container</h3><p class="help">'
       +'ROMarr runs from an image, so the update happens on the host:</p><pre style="font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim)">'
       +esc(d.docker_guidance.join('\n'))+'</pre></div>'
      :'')
    +(plugins.length
      ?'<div class="card"><h3>ROM Hub plugins</h3><table><thead><tr>'
       +'<th>Plugin</th><th>Source</th><th>Ref</th><th>Commit</th></tr></thead><tbody>'
       +plugins.map(([slug,info])=>'<tr><td>'+esc(slug)+'</td>'
         +'<td style="color:var(--dim);font-size:12px">'+esc(info.source||'—')+'</td>'
         +'<td>'+esc(info.ref||'latest')+'</td>'
         +'<td><code>'+esc((info.commit||'').slice(0,8))+'</code></td></tr>').join('')
       +'</tbody></table></div>'
      :'')
    +((d.update_log||'')
      ?'<div class="card"><h3>Last self-update log</h3>'
       +'<pre style="font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--dim);white-space:pre-wrap;max-height:40vh;overflow:auto">'
       +esc(d.update_log)+'</pre></div>'
      :'');
  const msg=$('#up-msg');
  const say=(t,bad)=>{msg.innerHTML='<div class="panel-note '+(bad?'panel-warn':'')+'">'+esc(t)+'</div>';};
  const run=(name,fn)=>async e=>{
    e.target.disabled=true; e.target.textContent='Running…';
    say('Working…');
    try{
      const r=await fn();
      say(r.message||(r.error?r.error:'done'), !r.ok&&!!r.error);
      setTimeout(()=>go('update'), r.ok&&name==='ApplyUpdate'?2500:0);
    }catch(err){say('request failed: '+err, true);}
    e.target.disabled=false;
  };
  const cmd=(name)=>j('/api/v1/command',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name})});
  $('#up-check').onclick=run('UpdateCheck',()=>cmd('UpdateCheck'));
  if($('#up-apply'))$('#up-apply').onclick=async e=>{
    if(!confirm('Update ROMarr now? Downloads in flight will defer the update; otherwise ROMarr restarts itself.'))return;
    await run('ApplyUpdate',()=>cmd('ApplyUpdate'))(e);
  };
  if($('#up-plugins'))$('#up-plugins').onclick=run('PluginUpdate',()=>cmd('PluginUpdate'));
};

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

/* ---------------- theme control ---------------- */
/* The mode and the accent are already applied by the head script -- all that
   is left here is wiring the buttons and keeping them showing the truth. */
function paintTheme(){
  const mode=ROMTHEME.mode(), accent=ROMTHEME.accent().toLowerCase();
  document.querySelectorAll('[data-theme-set]').forEach(b=>
    b.classList.toggle('on', b.dataset.themeSet===mode));
  document.querySelectorAll('[data-accent]').forEach(b=>
    b.classList.toggle('on', b.dataset.accent.toLowerCase()===accent));
  const pick=$('#accent-pick'); if(pick) pick.value=accent;
}
function bindTheme(){
  document.documentElement.addEventListener('themechange',paintTheme);
  document.querySelectorAll('[data-theme-set]').forEach(b=>b.onclick=()=>
    ROMTHEME.set(b.dataset.themeSet,null));
  document.querySelectorAll('[data-accent]').forEach(b=>b.onclick=()=>
    ROMTHEME.set(null,b.dataset.accent));
  const pick=$('#accent-pick');
  // `input` rather than `change`: the OS colour picker is a live preview, and
  // waiting for it to be dismissed makes the whole control feel broken.
  if(pick) pick.oninput=()=>ROMTHEME.set(null,pick.value);
  paintTheme();
}

/* ---------------- boot ---------------- */
async function refreshCounts(){
  const s=await j('/api/v1/system/counts').catch(()=>({}));
  document.querySelectorAll('[data-ct]').forEach(b=>{
    const v=s[b.dataset.ct];
    // null means "not counted yet" -- a dash is honest where 0 would claim
    // the library is empty.
    b.textContent = v == null ? (b.dataset.ct === 'games' ? '—' : '') : (v || '');
    b.style.display = (v == null && b.dataset.ct !== 'games') ? 'none' : '';
    // The games badge is a sum of two unlike things on any library that
    // catalogues entries it does not hold. It stays a sum -- a badge that
    // renumbered itself would read as data loss -- but it no longer keeps
    // that to itself.
    if(b.dataset.ct === 'games' && s.games_catalogued)
      b.title = `${s.games_on_disk} on disk here · `
              + `${s.games_catalogued} catalogued elsewhere`;
  });
}
(async()=>{
  document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>go(n.dataset.page));
  bindTheme();
  let searchTimer=null;
  $('#search').oninput=()=>{const p=location.hash.slice(1)||'library';
    if(p!=='library') return;
    // Debounced: the search is server-side now, and a request per
    // keystroke over 166k rows is rude to everyone involved.
    LIB.offset=0; LIB.limit=120;
    clearTimeout(searchTimer);
    searchTimer=setTimeout(()=>RENDER.library(),250);};
  let setupState;
  [PLATFORMS,SETTINGS,setupState]=await Promise.all([
    j('/api/platforms').catch(()=>[]), j('/api/v1/config').catch(()=>({})),
    j('/api/v1/setup/status').catch(()=>({}))]);
  go(location.hash.slice(1)||(setupState.suggested?'setup':'library'));
  refreshCounts(); setInterval(refreshCounts,15000);
})();
"""


def page() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>ROMarr</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style><script>{THEME_BOOT}</script></head><body>
<nav id="rail"><div id="brand">ROM<span>arr</span></div>
<div id="navwrap">{_nav_html()}</div>
{_theme_control_html()}</nav>
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
/* The stored mode and accent reach this screen too -- it is served before any
   of the app is, so a door in the wrong colours is the first thing an operator
   who chose light mode would see. */
body{display:flex;align-items:center;justify-content:center;min-height:100vh;
  padding:20px;
  background:radial-gradient(1100px 620px at 50% -10%,
    var(--accent-softer),transparent 70%),var(--bg)}
.box{width:100%;max-width:390px;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r-lg);padding:30px;
  box-shadow:var(--shadow-2),var(--edge)}
.box h1{display:flex;gap:0;font-size:26px;font-weight:700;letter-spacing:-.03em;
  justify-content:center;margin-bottom:6px}
.box h1 span{color:var(--accent-text)}
.box .sub{text-align:center;color:var(--dim);font-size:13px;margin-bottom:24px}
.box label{display:block;font-size:10.5px;font-weight:600;color:var(--dim);
  margin:15px 0 6px;text-transform:uppercase;letter-spacing:.08em}
.box input{width:100%;padding:10px 13px;background:var(--bg);color:var(--fg);
  border:1px solid var(--line);border-radius:var(--r-sm);outline:none;
  font-size:14px;transition:border-color var(--t),box-shadow var(--t)}
.box input:focus{border-color:var(--accent-line);box-shadow:var(--ring)}
.box button{width:100%;margin-top:22px;padding:11px;background:var(--accent);
  color:var(--accent-ink);border:0;border-radius:var(--r-sm);font-size:14px;
  font-weight:600;cursor:pointer;transition:filter var(--t),box-shadow var(--t)}
.box button:hover{filter:brightness(1.07);box-shadow:var(--shadow-1)}
.box button:disabled{opacity:.6;cursor:default;filter:none}
.note{margin-top:20px;padding:12px 14px;background:var(--bg);
  border:1px solid var(--line-soft);border-left:3px solid var(--info);
  border-radius:var(--r-sm);color:var(--dim);font-size:12px;line-height:1.65}
.err{margin-top:16px;padding:11px 13px;border-radius:var(--r-sm);font-size:13px;
  background:color-mix(in srgb,var(--bad) 14%,transparent);
  border:1px solid color-mix(in srgb,var(--bad) 45%,transparent);
  color:var(--bad);display:none}
.err.on{display:block}
.alt{margin-top:16px;text-align:center;font-size:12px;color:var(--dim)}
.alt a{cursor:pointer}
.sso{display:block;width:100%;padding:11px;margin-top:4px;text-align:center;
  background:var(--accent);color:var(--accent-ink);border-radius:var(--r-sm);
  font-size:14px;font-weight:600;text-decoration:none;
  transition:filter var(--t),box-shadow var(--t)}
.sso:hover{filter:brightness(1.07);box-shadow:var(--shadow-1)}
.sso-or{display:flex;align-items:center;gap:10px;margin:18px 0 2px;
  color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
.sso-or::before,.sso-or::after{content:"";flex:1;height:1px;
  background:var(--line)}
@media(max-width:420px){.box{padding:24px 20px}}
"""


def login_page(*, claimed: bool, totp: bool = False,
               oidc: dict | None = None) -> str:
    """The sign-in screen, or the first-run claim screen.

    Server-rendered and served unauthenticated, because the whole failure was
    a browser with no way to obtain a credential.

    `oidc` is service.oidc_status(): when enabled, an SSO button leads the
    round trip. Shown on the claim screen too — a configured IdP is a way in
    before anybody has set a password, and refusing it there would lock an
    SSO-only install out of its own setup.
    """
    oidc = oidc or {}
    sso_button = ""
    if oidc.get("enabled"):
        label = str(oidc.get("display_name") or "SSO")
        sso_button = (
            f'<a class="sso" href="/oidc/login">Sign in with '
            f'{label.replace("<", "").replace(">", "")}</a>'
            '<div class="sso-or"><span>or</span></div>')
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
<style>{CSS}{LOGIN_CSS}</style><script>{THEME_BOOT}</script></head><body>
<form class="box" id="f" autocomplete="on">
  <h1>ROM<span>arr</span></h1>
  <div class="sub">{title}</div>
  {sso_button}
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


# ------------------------------------------------------ the invitation link --
#
# Where a peering invitation lands, and the one page in ROMarr written for
# somebody who does not have an account here.
#
# Alice sends Bob a link. Bob clicks it and his browser goes to ALICE'S server,
# because Alice's address is the only address Alice knows. That is the whole
# difficulty with a Plex-style link in a system with no central anything:
# plex.tv/link works because plex.tv exists, and nothing here is allowed to. So
# this page's job is to hand Bob back to his own ROMarr, which it does by
# rewriting the same path and the same fragment onto the host he types once.
#
# The invariant that lets the page know which of the two servers it is running
# on: `u` appears in the fragment ONLY on a link that has been rehomed. No `u`
# means this is the inviter's server and there is nowhere to forward to. A `u`
# means the visitor is home, and the page hands the invitation to the app.
#
# Nothing here is a secret. The fragment carries an invitation id and a display
# name, and a browser never sends a fragment to any server -- so this page is
# reached, in the log, as a bare GET /link with no query and no referrer trail.
# The credential is the claim code, which travels by whatever channel the two
# of them chose and is typed in on the far side.

LINK_CSS = """
.box{max-width:460px}
.box .who{font-size:15px;font-weight:600;text-align:center;margin-bottom:4px}
.box .id{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--dim);
  text-align:center;word-break:break-all;margin-bottom:18px}
.box .step{display:flex;gap:10px;align-items:flex-start;margin:14px 0;
  font-size:13px;color:var(--dim);line-height:1.6}
.box .step b{display:flex;align-items:center;justify-content:center;
  flex:0 0 20px;height:20px;border-radius:50%;background:var(--surface-3);
  color:var(--fg);font-size:11px;margin-top:1px}
.box .copy{width:100%;margin-top:10px;padding:9px;background:transparent;
  color:var(--fg);border:1px solid var(--line);border-radius:4px;
  font-size:13px;cursor:pointer}
.box code{font:12px/1.6 ui-monospace,Menlo,monospace;word-break:break-all}
"""

#: Plain, uninterpolated: this script has regular expressions and apostrophes
#: in it, and every one of them is a chance for an f-string to eat a backslash
#: and produce a page that renders blank with one line in the console.
LINK_JS = r"""
/* The fragment, which never left this browser. */
function frag() {
  var out = {};
  location.hash.replace(/^#/, '').split('&').forEach(function (pair) {
    if (!pair) return;
    var i = pair.indexOf('=');
    var k = i < 0 ? pair : pair.slice(0, i);
    var v = i < 0 ? '' : pair.slice(i + 1);
    try { out[decodeURIComponent(k)] = decodeURIComponent(v); }
    catch (_) { out[k] = v; }
  });
  return out;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
var box = document.getElementById('box');
var f = frag(), here = location.origin;

function bad(msg) {
  box.innerHTML = '<h1>ROM<span>arr</span></h1>'
    + '<div class="sub">Not an invitation</div>'
    + '<div class="note">' + esc(msg) + '</div>';
}

/* Home. The fragment was rehomed onto this host, so this IS the recipient's
   server. Hand the invitation to the app through sessionStorage rather than
   through the URL: the app's router owns the hash, and an invitation has no
   business surviving into the next tab. If nobody is signed in here, the app
   shell serves the sign-in screen and the invitation is waiting afterwards. */
function home() {
  try {
    sessionStorage.setItem('romarr-invite', JSON.stringify(
      {url: f.u, peer_id: f.i, name: f.n || ''}));
  } catch (_) {}
  location.replace('/#peers');
}

/* Away. This is the inviter's server. Ask for the recipient's own address and
   rewrite the same link onto it, carrying this origin in `u` so their ROMarr
   knows who invited them. Remembered locally, because the second invitation
   somebody receives should not ask twice. */
function away() {
  var mine = '';
  try { mine = localStorage.getItem('romarr-home') || ''; } catch (_) {}
  var link = here + '/link#i=' + encodeURIComponent(f.i)
    + (f.n ? '&n=' + encodeURIComponent(f.n) : '');
  box.innerHTML = '<h1>ROM<span>arr</span></h1>'
    + '<div class="sub">You have been invited to peer</div>'
    + '<div class="who">' + esc(f.n || here) + '</div>'
    + '<div class="id">' + esc(here) + '</div>'
    + '<div class="step"><b>1</b><span>Tell ROMarr where your own server '
    + 'lives. This page is on your friend&rsquo;s server, so it has to send '
    + 'you home.</span></div>'
    + '<label for="home">Your ROMarr address</label>'
    + '<input id="home" type="url" placeholder="http://192.168.1.20:7878" '
    + 'value="' + esc(mine) + '" autofocus>'
    + '<button id="go">Open this invitation on my ROMarr</button>'
    + '<div class="step"><b>2</b><span>Your ROMarr will ask for the '
    + '<b>claim code</b> &mdash; eight characters your friend sends you '
    + 'separately. It is deliberately not in this link, and it expires in '
    + '15 minutes.</span></div>'
    + '<div class="note">No ROMarr on this device? Copy the link and paste it '
    + 'into <b>Friends &rarr; I have an invitation</b> on your own server.'
    + '<br><br><code>' + esc(link) + '</code>'
    + '<button class="copy" id="copy">Copy the link</button></div>';
  document.getElementById('copy').onclick = function () {
    var b = document.getElementById('copy');
    navigator.clipboard.writeText(link).then(
      function () { b.textContent = 'Copied'; },
      function () { b.textContent = 'Select the link above and copy it'; });
  };
  document.getElementById('go').onclick = function () {
    var raw = document.getElementById('home').value.trim();
    if (!raw) return;
    if (!/^https?:\/\//i.test(raw)) raw = 'http://' + raw;
    raw = raw.replace(/\/+$/, '');
    try { localStorage.setItem('romarr-home', raw); } catch (_) {}
    location.href = raw + '/link#i=' + encodeURIComponent(f.i)
      + (f.n ? '&n=' + encodeURIComponent(f.n) : '')
      + '&u=' + encodeURIComponent(here);
  };
}

if (!f.i) bad('This link carries no invitation. Copy the whole thing, '
  + 'including the part after the # — that is where the invitation is, '
  + 'and some chat clients cut it off.');
else if (f.u) home();
else away();
"""


def link_page() -> str:
    """The landing page for an invitation link.

    A constant. It takes no parameters, reads no invitation and returns the
    same bytes to everybody, which is what lets it sit on the open-path list
    without being a thing that can leak: there is nothing behind it to ask.

    `referrer: no-referrer` because this page navigates to the recipient's own
    server, and the inviter's hostname is not the recipient's server's business
    to learn from a header.
    """
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Invitation &middot; ROMarr</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>{CSS}{LOGIN_CSS}{LINK_CSS}</style></head><body>
<div class="box" id="box">
  <h1>ROM<span>arr</span></h1>
  <div class="sub">Checking this invitation&hellip;</div>
</div>
<script>{LINK_JS}</script></body></html>"""
