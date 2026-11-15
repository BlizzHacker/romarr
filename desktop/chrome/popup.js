"use strict";
const profilesElement=document.getElementById('profiles'), status=document.getElementById('status');
const form=document.getElementById('add'), retry=document.getElementById('retry');
let profiles=[], ready=false, busy=false, launcherTabs={};
const opening=new Set();
function controls() {
  form.querySelectorAll('input,button').forEach(element=>{element.disabled=!ready||busy;});
  profilesElement.querySelectorAll('button').forEach(button=>{button.disabled=!ready||busy;});
  retry.disabled=busy;
}
async function mutate(nextProfiles, message) {
  if(!ready||busy)return false;
  busy=true;controls();
  try {
    const next=nextProfiles();
    await chrome.storage.local.set({profiles:next});
    profiles=next;render();status.textContent=message;
    return true;
  } catch(error) {
    status.textContent=error.message||'Could not save changes.';
    return false;
  } finally {busy=false;controls();}
}
async function openServer(profile, hash='library') {
  if(!ready||busy||opening.has(profile.url))return;
  opening.add(profile.url);
  try {
    const response=await chrome.runtime.sendMessage({type:'openServer',profile,hash});
    if(response.error)throw new Error(response.error);
    if(response.denied){status.textContent='Server access was not granted. Allow this server to reuse its signed-in tab.';return;}
    launcherTabs[profile.url]=response.tabId;
    render();
    status.textContent=response.held?'Existing tab is on another site. Finish sign-in there, or close it to reopen this server.'
      :'Server tab opened. Sign in there once if needed; the section buttons reuse this tab.';
  } catch {
    status.textContent='Could not open the server tab. Try Open / Connect again.';
  } finally {opening.delete(profile.url);}
}

function render() {
  profilesElement.replaceChildren();
  profiles.forEach(profile=>{
    const article=document.createElement('article'), name=document.createElement('strong'), address=document.createElement('span'), actions=document.createElement('div');
    name.textContent=profile.name; address.textContent=profile.url; actions.className='actions';
    const connect=document.createElement('button');connect.textContent='Open / Connect server';
    connect.onclick=()=>openServer(profile);actions.append(connect);
    for(const [title,hash] of [['Library','library'],['Queue','queue'],['Emulators','emulators'],['Settings','general']]) {
      const link=document.createElement('button'); link.textContent=title; link.onclick=()=>openServer(profile,hash); actions.append(link);
    }
    const remove=document.createElement('button'); remove.textContent='Remove'; remove.setAttribute('aria-label','Remove '+profile.name);
    remove.onclick=()=>mutate(()=>profiles.filter(value=>value.url!==profile.url),'Server removed.');
    actions.append(remove); const connection=document.createElement('p');connection.textContent=Number.isInteger(launcherTabs[profile.url])?'Server tab linked; section buttons reuse it.':'Open / Connect, then sign in on your server.'; article.append(name,address,connection,actions); profilesElement.append(article);
  });
  if(!profiles.length) profilesElement.textContent='No saved servers yet.';
  controls();
}
form.onsubmit=async event=>{
  event.preventDefault();
  const saved=await mutate(()=>{
    const profile=serverProfile({name:document.getElementById('name').value,url:document.getElementById('url').value});
    if(profiles.some(p=>p.url===profile.url)) throw new Error('That server is already saved.');
    if(profiles.length>=20) throw new Error('Remove a server before adding more (20 maximum).');
    return [...profiles,profile];
  },'Server saved on this device.');
  if(saved)event.target.reset();
};
async function initialize() {
  if(busy)return;
  ready=false;busy=true;retry.hidden=true;controls();
  status.textContent='Reading local server profiles…';
  try {
    await chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
    const stored=await chrome.storage.local.get('profiles');
    const session=await chrome.storage.session.get('launcherTabs');
    launcherTabs=session.launcherTabs&&typeof session.launcherTabs==='object'?session.launcherTabs:{};
    profiles=(Array.isArray(stored.profiles)?stored.profiles:[]).slice(0,20).flatMap(value=>{try{return [serverProfile(value)];}catch{return [];}});
    ready=true;render();status.textContent='';
  } catch{status.textContent='Could not read local server profiles. Retry before changing servers.';retry.hidden=false;}
  finally{busy=false;controls();}
}
retry.onclick=initialize;
initialize();
