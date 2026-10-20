"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ctx={setTimeout,clearTimeout};vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/ambient.js'),'utf8'),ctx);
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(overrides={}){
 let now=0,id=0,active=true,counts=[],commits=0,shown=0,choice=0;const timers=new Map();
 const service=ctx.MovieClawAmbient.create({active:()=>active,random:()=>[0,.4,.99,0][choice++%4],now:()=>now,setTimer(fn,ms){const key=++id;timers.set(key,{fn,at:now+ms});return key;},clearTimer:key=>timers.delete(key),
  prepare:async n=>{counts.push(n);return {n};},apply:async()=>{shown++;return true;},confirm:async()=>{commits++;},...overrides});
 async function advance(ms){const end=now+ms;for(;;){const entries=[...timers.entries()].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at);if(!entries.length)break;const [key,row]=entries[0];now=row.at;timers.delete(key);row.fn();await tick();}now=end;await tick();}
 return {service,advance,counts,timers,deactivate:()=>active=false,get shown(){return shown;},get commits(){return commits;}};
}
(async()=>{
 let f=fixture();f.service.activity();await f.advance(4999);assert.deepEqual(f.counts,[]);f.service.activity();await f.advance(4999);assert.equal(f.commits,0);await f.advance(1);assert.equal(f.commits,1);await f.advance(3000);assert.deepEqual(f.counts,[1,2,3,1]);f.service.close();assert.equal(f.timers.size,0);
 let finish;f=fixture({prepare:n=>new Promise(resolve=>finish=()=>resolve({n}))});f.service.activity();await f.advance(5000);f.service.activity();finish();await tick();assert.equal(f.shown,0,'movement cancels a prepared patch before presentation');f.service.close();
 let applied=false;f=fixture({apply:async(p,valid,wait)=>{applied=await wait(650,valid);return applied;}});f.service.activity();await f.advance(5000);await f.advance(300);f.service.activity();await tick();assert.equal(applied,false);assert.equal(f.commits,0);f.service.close();
 let confirmEnd;f=fixture({confirm:()=>new Promise(resolve=>confirmEnd=resolve)});f.service.activity();await f.advance(5000);assert.equal(f.shown,1);assert.equal(f.service.committing,true);f.service.activity();confirmEnd();await f.service.flush();assert.equal(f.service.committing,false,'visible patch is acknowledged after activity');f.service.close();
 f=fixture();f.service.activity();f.deactivate();await f.advance(30000);assert.equal(f.shown,0,'hidden or non-wall pages cannot rotate');f.service.close();
 // Real choreography: starts and replacements are individually staggered,
 // and interruption retains only already displayed posters for acknowledgement.
 async function choreography(cancelAt=Infinity,reduced=false){
  let time=0;const events=[],restored=[];
  const shown=await ctx.MovieClawAmbient.transition({rows:['a','b','c'],settings:{fade_out:1100,fade_in:1500,gap:650,overlap:0,variation:0},random:()=>0,reduced,valid:()=>time<cancelAt,
   wait:async ms=>{time+=ms;return time<cancelAt;},out:row=>events.push(['out',row,time]),swap:row=>events.push(['in',row,time]),restore:row=>restored.push(row)});
  return {shown:Array.from(shown),events,restored};
 }
 let motion=await choreography();assert.deepEqual(motion.events,[['out','a',0],['out','b',650],['in','a',1100],['out','c',1300],['in','b',1750],['in','c',2400]]);
 assert.deepEqual(motion.shown,['a','b','c']);assert.deepEqual(motion.restored,['a','b','c']);
 motion=await choreography(1200);assert.deepEqual(motion.shown,['a'],'never count an interrupted, unseen replacement');assert.deepEqual(motion.restored,['a','b','c']);
 motion=await choreography(300);assert.deepEqual(motion.shown,[]);assert.deepEqual(motion.restored,['a','b','c']);
 motion=await choreography(Infinity,true);assert.ok(motion.events.every(row=>row[2]===0),'reduced motion does not wait through fades');
 console.log('PASS: screensaver 5s idle/reset, random 1–3 posters and faster continuous cadence, movement cancels preparation/fade, displayed acknowledgement, inactive and bounded scheduler cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
