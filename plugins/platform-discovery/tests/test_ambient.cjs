"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ctx={setTimeout,clearTimeout};vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/ambient.js'),'utf8'),ctx);
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(overrides={}){
 let now=0,id=0,active=true,counts=[],commits=0,shown=0;const timers=new Map();
 const service=ctx.MovieClawAmbient.create({active:()=>active,now:()=>now,setTimer(fn,ms){const key=++id;timers.set(key,{fn,at:now+ms});return key;},clearTimer:key=>timers.delete(key),
  prepare:async n=>{counts.push(n);return {n};},apply:async()=>{shown++;return true;},confirm:async()=>{commits++;},...overrides});
 async function advance(ms){const end=now+ms;for(;;){const entries=[...timers.entries()].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at);if(!entries.length)break;const [key,row]=entries[0];now=row.at;timers.delete(key);row.fn();await tick();}now=end;await tick();}
 return {service,advance,counts,timers,deactivate:()=>active=false,get shown(){return shown;},get commits(){return commits;}};
}
(async()=>{
 let f=fixture();f.service.activity();await f.advance(4999);assert.deepEqual(f.counts,[]);f.service.activity();await f.advance(4999);assert.equal(f.commits,0);await f.advance(1);assert.equal(f.commits,1);await f.advance(21000);assert.deepEqual(f.counts,[1,2,3,1]);f.service.close();assert.equal(f.timers.size,0);
 let finish;f=fixture({prepare:n=>new Promise(resolve=>finish=()=>resolve({n}))});f.service.activity();await f.advance(5000);f.service.activity();finish();await tick();assert.equal(f.shown,0,'movement cancels a prepared patch before presentation');f.service.close();
 let applied=false;f=fixture({apply:async(p,valid,wait)=>{applied=await wait(650,valid);return applied;}});f.service.activity();await f.advance(5000);await f.advance(300);f.service.activity();await tick();assert.equal(applied,false);assert.equal(f.commits,0);f.service.close();
 let confirmEnd;f=fixture({confirm:()=>new Promise(resolve=>confirmEnd=resolve)});f.service.activity();await f.advance(5000);assert.equal(f.shown,1);assert.equal(f.service.committing,true);f.service.activity();confirmEnd();await f.service.flush();assert.equal(f.service.committing,false,'visible patch is acknowledged after activity');f.service.close();
 f=fixture();f.service.activity();f.deactivate();await f.advance(30000);assert.equal(f.shown,0,'hidden or non-wall pages cannot rotate');f.service.close();
 console.log('PASS: screensaver 5s idle/reset, 1/2/3 sequence, movement cancels preparation/fade, displayed acknowledgement, inactive and bounded scheduler cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
