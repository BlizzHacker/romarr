"use strict";
importScripts('validation.js');
const pending=new Map(), permissionRequests=new Map();
let launchQueue=Promise.resolve();
async function launchSavedServer(profile, hash) {
  profile=serverProfile(profile);
  if(!['library','queue','emulators','general'].includes(hash))throw Error('Invalid section.');
  const base=new URL(profile.url), pattern=base.origin+'/*';
  const stored=await chrome.storage.local.get('profiles');
  if(!(stored.profiles||[]).some(saved=>{try{return serverProfile(saved).url===profile.url;}catch{return false;}}))
    throw Error('Save this server before opening it.');
  if(!await chrome.permissions.contains({origins:[pattern]}))throw Error('Server access is not granted.');
  const session=await chrome.storage.session.get('launcherTabs');
  const launcherTabs=session.launcherTabs&&typeof session.launcherTabs==='object'?session.launcherTabs:{};
  let tab;
  if(Number.isInteger(launcherTabs[profile.url])){
    try{tab=await chrome.tabs.get(launcherTabs[profile.url]);}catch{delete launcherTabs[profile.url];}
  }
  if(!tab){
    const matches=await chrome.tabs.query({url:pattern});
    tab=matches.find(value=>{
      try{const url=new URL(value.url);return url.origin===base.origin&&url.pathname.startsWith(base.pathname);}
      catch{return false;}
    });
  }
  let held=false;
  const destination=base.href+'#'+hash;
  if(tab){
    let address;
    try{address=new URL(tab.url);}catch{}
    // Unapproved sign-in authorities have no readable URL. Keep their flow
    // intact: focus the known tab rather than navigating or opening another.
    held=!address||address.origin!==base.origin||!address.pathname.startsWith(base.pathname);
  }else tab=await chrome.tabs.create({url:destination,active:false});
  launcherTabs[profile.url]=tab.id;
  // Persist before navigation/focus can close the action popup.
  await chrome.storage.session.set({launcherTabs});
  if(!held)await chrome.tabs.update(tab.id,{url:destination});
  await chrome.tabs.update(tab.id,{active:true});
  await chrome.windows.update(tab.windowId,{focused:true});
  return {tabId:tab.id,held};
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id||message?.type!=='openServer')return false;
  let profile;
  try {
    profile=serverProfile(message.profile);
    if(!['library','queue','emulators','general'].includes(message.hash))throw Error('Invalid section.');
  }catch{reply({error:'Invalid saved server or section.'});return false;}
  const key=profile.url+"#"+message.hash;
  if(!pending.has(key)){
    // Runtime messages carry the originating click's user gesture. Start the
    // prompt synchronously in the worker so closing its popup cannot abort it.
    if(!permissionRequests.has(profile.url)){
      const request=chrome.permissions.request({origins:[new URL(profile.url).origin+'/*']});
      permissionRequests.set(profile.url,request);
      request.finally(()=>{if(permissionRequests.get(profile.url)===request)permissionRequests.delete(profile.url);}).catch(()=>{});
    }
    const permission=permissionRequests.get(profile.url);
    const operation=permission.then(granted=>{
      if(!granted)return {denied:true};
      const launch=launchQueue.then(()=>launchSavedServer(profile,message.hash));
      launchQueue=launch.catch(()=>{});
      return launch;
    });
    pending.set(key,operation);
    operation.finally(()=>pending.delete(key)).catch(()=>{});
  }
  pending.get(key).then(reply,()=>reply({error:'Could not open the saved server.'}));
  return true;
});
