"use strict";
const profilesElement=document.getElementById('profiles'), status=document.getElementById('status');
let profiles=[];
async function save() { await chrome.storage.local.set({profiles}); }
function render() {
  profilesElement.replaceChildren();
  profiles.forEach((profile,index)=>{
    const article=document.createElement('article'), name=document.createElement('strong'), address=document.createElement('span'), actions=document.createElement('div');
    name.textContent=profile.name; address.textContent=profile.url; actions.className='actions';
    for(const [title,hash] of [['Library','library'],['Queue','queue'],['Emulators','emulators'],['Settings','general']]) {
      const link=document.createElement('a'); link.textContent=title; link.href=serverURL(profile.url)+'#'+hash; link.target='_blank'; link.rel='noopener noreferrer'; actions.append(link);
    }
    const remove=document.createElement('button'); remove.textContent='Remove'; remove.setAttribute('aria-label','Remove '+profile.name);
    remove.onclick=async()=>{const previous=profiles;profiles=profiles.filter((_,i)=>i!==index);try{await save();render();status.textContent='Server removed.';}catch{profiles=previous;status.textContent='Could not save changes.';}};
    actions.append(remove); article.append(name,address,actions); profilesElement.append(article);
  });
  if(!profiles.length) profilesElement.textContent='No saved servers yet.';
}
document.getElementById('add').onsubmit=async event=>{
  event.preventDefault();
  try {
    const profile=serverProfile({name:document.getElementById('name').value,url:document.getElementById('url').value});
    if(profiles.some(p=>p.url===profile.url)) throw new Error('That server is already saved.');
    if(profiles.length>=20) throw new Error('Remove a server before adding more (20 maximum).');
    const next=[...profiles,profile]; await chrome.storage.local.set({profiles:next}); profiles=next;
    render();event.target.reset();status.textContent='Server saved on this device.';
  } catch(error){status.textContent=error.message;}
};
(async()=>{
  try {
    await chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
    const stored=await chrome.storage.local.get('profiles');
    profiles=(Array.isArray(stored.profiles)?stored.profiles:[]).slice(0,20).flatMap(value=>{try{return [serverProfile(value)];}catch{return [];}});
    render();
  } catch{status.textContent='Could not read local server profiles.';}
})();
