// Bounded DOM fixture for exercising the shipped sidebar/router functions in Node.
// Layout, native details, CSS, and real browser focus are covered by browser QA.
const assert = require('node:assert/strict');
let mobile = true;
class Element {
  constructor(tag, attrs = {}) {
    this.tag = tag; this.attrs = {...attrs}; this.children = []; this.events = {};
    this.dataset = {}; this.style = {setProperty(k,v){this[k]=v;}, removeProperty(k){delete this[k];}};
    this.classes = new Set((attrs.class || '').split(' ').filter(Boolean));
    this.classList = {contains: c=>this.classes.has(c), add:c=>this.classes.add(c),
      remove:c=>this.classes.delete(c), toggle:(c,on)=>{if(on===undefined)on=!this.classes.has(c);on?this.classes.add(c):this.classes.delete(c);}};
    for(const [k,v] of Object.entries(attrs)) if(k.startsWith('data-'))
      this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;
    this.open = 'open' in attrs; this.hidden = 'hidden' in attrs;
  }
  appendChild(el){el.parent=this;this.children.push(el);return el;}
  contains(el){return el===this || this.children.some(c=>c.contains(el));}
  setAttribute(k,v){this.attrs[k]=v;}
  getAttribute(k){return this.attrs[k] ?? null;}
  removeAttribute(k){delete this.attrs[k];}
  addEventListener(k,fn){(this.events[k] ||= []).push(fn);}
  fire(k, props={}){const e={target:this,preventDefault(){this.prevented=true;},...props};for(const fn of this.events[k]||[])fn(e);return e;}
  focus(){document.activeElement=this;}
  getClientRects(){return this.hidden ? [] : [{}];}
  matches(sel){
    const not=sel.match(/:not\(([^)]+)\)/); if(not){if(this.matches(not[1]))return false;sel=sel.replace(not[0],'');}
    const tag=sel.match(/^[a-z][a-z0-9]*/); if(tag && tag[0]!==this.tag)return false;
    for(const m of sel.matchAll(/#([\w-]+)/g))if(this.attrs.id!==m[1])return false;
    for(const m of sel.matchAll(/\.([\w-]+)/g))if(!this.classes.has(m[1]))return false;
    for(const m of sel.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)){
      const val=m[1]==='open'?(this.open?'':undefined):this.attrs[m[1]];
      if(val===undefined || m[2]!==undefined && val!==m[2])return false;
    }return true;
  }
  closest(sel){for(let e=this;e;e=e.parent)if(e.matches(sel))return e;return null;}
  querySelectorAll(selector){
    const all=[];const walk=e=>{for(const c of e.children){all.push(c);walk(c);}};walk(this);
    return all.filter(e=>selector.split(',').some(s=>{
      s=s.trim();if(s.startsWith(':scope > '))return e.parent===this && e.matches(s.slice(9));
      const parts=s.split(/\s+/);if(!e.matches(parts.pop()))return false;
      let p=e.parent;while(parts.length){const part=parts.pop();while(p&&!p.matches(part))p=p.parent;if(!p)return false;p=p.parent;}return true;
    }));
  }
  querySelector(s){return this.querySelectorAll(s)[0] || null;}
}
const document = new Element('document');
document.documentElement = document.appendChild(new Element('html'));
document.body = document.documentElement.appendChild(new Element('body'));
document.createElement = tag=>new Element(tag);
const make=(tag,attrs,parent=document.body)=>parent.appendChild(new Element(tag,attrs));
const rail=make('nav',{id:'rail'}),brand=make('div',{id:'brand'},rail);
make('button',{id:'sidebar-close'},brand);
const navwrap=make('div',{id:'navwrap'},rail);
for(const [name,items] of NAV){
  const group=make('details',{class:'navgroup','data-group':name.toLowerCase()},navwrap);
  make('summary',{class:'navhead','data-group':name.toLowerCase(),'data-first-page':items[0][0]},group);
  const links=make('div',{class:'navlinks'},group);
  for(const [page,label,count] of items){const link=make('a',{class:'nav','data-page':page,href:'#'+page},links);if(count)make('b',{'data-ct':count},link);}
  if(name==='Activity')make('b',{'data-ct':'queued'},group.children[0]);
}
const appearance=make('details',{id:'appearance-details'},rail);
make('summary',{id:'appearance-toggle'},appearance);
make('button',{'data-theme-set':'dark'},appearance);
const scrim=make('div',{id:'sidebar-scrim',hidden:''});
const main=make('div',{id:'main'}),menu=make('button',{id:'sidebar-menu'},main);
const heading=make('h1',{},make('div',{id:'top'},main));
const search=make('input',{id:'search'},main);
const window={matchMedia:()=>({matches:mobile}),innerHeight:640,events:{},
  addEventListener(k,fn){(this.events[k] ||= []).push(fn);}};
window.visualViewport={height:600,offsetTop:0,addEventListener(){}};
const addEventListener=(k,fn)=>window.addEventListener(k,fn);
const getComputedStyle=()=>({visibility:'visible'});
const location={hash:''};
const $=s=>document.querySelector(s);
let rendered='';
const RENDER=Object.fromEntries(NAV.flatMap(([,items])=>items.map(([p])=>[p,()=>{rendered=p;}])));
