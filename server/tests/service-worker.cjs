const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {};
let windows = [];
let opened = null;
let notice = null;
const self = {location:{origin:'https://compendium.test'}, registration:{showNotification:async (...args) => { notice = args; }},
  clients:{matchAll:async () => windows, openWindow:async url => { opened = url; }, claim:async()=>{}}, skipWaiting:async()=>{},
  addEventListener:(name, fn) => { listeners[name] = fn; }};
const context = {self, URL, caches:{open:async()=>({addAll:async()=>{},put:async()=>{}}),keys:async()=>[],delete:async()=>{},match:async()=>{}}, fetch:async()=>({ok:true,clone(){return this}}), Promise};
vm.runInNewContext(fs.readFileSync('server/web/sw.js', 'utf8'), context, {filename:'sw.js'});
async function trigger(name, event) { listeners[name](event); await event.done; }
async function main() {
  let payload = {title:'secret',body:'private',url:'https://evil.test'};
  await trigger('push', {data:{json:()=>payload}, waitUntil(p){ this.done=p; }});
  assert.equal(JSON.stringify(notice), JSON.stringify(['Agentic Compendium', {body:'New deliveries are ready. Open your private archive to read them.',icon:'/app/icon-192.png',badge:'/app/icon-192.png',tag:'compendium-deliveries',renotify:false}]));
  const inbox = {url:'https://compendium.test/app/',focused:false,focus:async()=>{inbox.focused=true;},postMessage:m=>{inbox.message=m;}};
  windows = [inbox, {url:'https://evil.test/app/',focused:true,focus:async()=>{throw Error('wrong client');},postMessage:()=>{throw Error('wrong client');}}];
  let closed = false;
  await trigger('notificationclick', {notification:{close:()=>{closed=true;}},waitUntil(p){ this.done=p; }});
  assert.equal(closed, true); assert.equal(inbox.focused, true); assert.equal(JSON.stringify(inbox.message), JSON.stringify({type:'compendium-deliveries'})); assert.equal(opened, null);
  windows = []; opened = null;
  await trigger('notificationclick', {notification:{close:()=>{}},waitUntil(p){ this.done=p; }});
  assert.equal(opened, '/app/');
  payload = null; let foreground = 0; windows = [{url:'https://compendium.test/app/drafts',postMessage:()=>foreground++}];
  await trigger('push', {data:{json:()=>{throw Error('bad');}},waitUntil(p){ this.done=p; }});
  assert.equal(foreground, 1); assert.equal(notice[0], 'Agentic Compendium');
}
main().catch(error=>{console.error(error);process.exitCode=1});
