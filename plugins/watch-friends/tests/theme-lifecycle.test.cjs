'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const events={},values=new Map();
const root={dataset:{},style:{getPropertyValue:key=>values.get(key)||'',setProperty:(key,value)=>values.set(key,value),removeProperty:key=>values.delete(key)},toggleAttribute(){}};
const host={documentElement:{dataset:{theme:'silver'}}};
let scheme='dark',observer;
class Observer {
  constructor(callback){this.callback=callback;this.targets=new Set();observer=this;}
  observe(target){this.targets.add(target);}
  disconnect(){this.targets.clear();}
}
const window={parent:{document:host,getComputedStyle:()=>({colorScheme:scheme,color:'#ffffff',getPropertyValue:()=>''})},
  matchMedia:()=>({matches:false,addEventListener(){}}),
  addEventListener:(name,callback,options)=>{(events[name]??=[]).push({callback,once:options?.once});}};
function dispatch(name,event={}){for(const handler of [...(events[name]||[])]){handler.callback(event);if(handler.once)events[name]=events[name].filter(h=>h!==handler);}}
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../movieclaw_friends/static/theme.js'),'utf8'),{document:{documentElement:root},window,MutationObserver:Observer,queueMicrotask:fn=>fn()});
assert.equal(root.dataset.mcColorScheme,'dark');
for(let round=0;round<2;round++){
  dispatch('pagehide',{persisted:true});assert.equal(observer.targets.size,0);
  scheme=round===0?'light':'dark';
  dispatch('pageshow',{persisted:true});
  assert.equal(root.dataset.mcColorScheme,scheme,'restored page must refresh the current host theme');
  assert.ok(observer.targets.has(host.documentElement),'restored page must observe later host theme changes');
  scheme=scheme==='light'?'dark':'light';observer.callback();
  assert.equal(root.dataset.mcColorScheme,scheme);
}
console.log('PASS: cached page restores current theme and observers over repeated back/forward navigation');
