"use strict";
function serverURL(value) {
  if (typeof value !== 'string' || value.length > 2048 || /[\\\x00-\x20\x7f]/.test(value)) throw new Error('Enter a valid HTTP or HTTPS server URL.');
  let url;
  try { url = new URL(value); } catch { throw new Error('Enter a complete server URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || /[?#]/.test(value)) throw new Error('Use HTTP or HTTPS without credentials, query parameters or fragments.');
  url.pathname = url.pathname.replace(/\/+$/, '') + '/';
  return url.href;
}
function serverProfile(profile) {
  if (!profile || typeof profile.name !== 'string' || !profile.name.trim() || profile.name.length > 60) throw new Error('Enter a name of 1 to 60 characters.');
  return {name: profile.name.trim(), url: serverURL(profile.url)};
}
if (typeof module !== 'undefined') module.exports = {serverURL, serverProfile};
