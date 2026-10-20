"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assets=path.join(__dirname,'../platform_discovery/assets'),elements=new Map(),saved=new Map();let account='first',writes=0,reads=0,hold=null,init=async()=>{};
function element(id){if(!elements.has(id))elements.set(id,{id,value:'',open:false,events:{},addEventListener(k,fn){this.events[k]=fn;},showModal(){this.open=true;},close(){this.open=false;}});return elements.get(id);}
const ctx={document:{getElementById:element}};vm.createContext(ctx);for(const file of ['ambient.js','ambient-settings.js'])vm.runInContext(fs.readFileSync(path.join(assets,file),'utf8'),ctx);
const defaults={...ctx.MovieClawAmbient.defaults},changes=[];
const controller=ctx.MovieClawAmbientSettings.create({initialize:()=>init(),getAccount:()=>account,onChange:value=>changes.push(value),request:async(_,options)=>{
 if(!options){reads++;const value=saved.get(account)||{settings:defaults,revision:''};if(hold)await hold;return structuredClone(value);}
 writes++;const {selection}=JSON.parse(options.body),current=saved.get(account);assert.equal(selection.revision,current?.revision||'');const value={settings:selection.settings,revision:String(writes)};saved.set(account,value);return structuredClone(value);
}});
const event=(id,type='click')=>element(id).events[type]({preventDefault(){}});
(async()=>{
 assert.equal(reads,0,'settings do not add network work to a cold wall open');
 await controller.open();assert.equal(element('ambient-save').disabled,false);assert.equal(element('ambient-fade-out').value,'2000');
 element('ambient-fade-out').value='3200';await event('ambient-fade-out','input');assert.equal(element('ambient-fade-out-value').textContent,'3.2 秒');
 await event('ambient-form','submit');assert.equal(saved.get('first').settings.fade_out,3200);assert.equal(changes.at(-1).fade_out,3200);
 await controller.open();assert.equal(element('ambient-fade-out').value,'3200');await event('ambient-reset');assert.equal(element('ambient-fade-out').value,'2000');await event('ambient-form','submit');
 assert.equal(saved.get('first').settings.fade_out,2000);const before=writes;init=async()=>{account='second';};await event('ambient-form','submit');assert.equal(writes,before,'old-account dialog cannot write a new account');init=async()=>{};
 let release;hold=new Promise(resolve=>release=resolve);const readBefore=reads,opening=controller.open();while(reads===readBefore)await Promise.resolve();account='third';release();await opening;assert.equal(element('ambient-save').disabled,true,'stale account read cannot unlock saving');hold=null;
 const a=ctx.MovieClawAmbient.timing(defaults,()=>0),b=ctx.MovieClawAmbient.timing(defaults,()=>1);assert.ok(a.out<b.out&&a.incoming<b.incoming&&a.gap<b.gap);
 let at=0;const phases=[];await ctx.MovieClawAmbient.transition({rows:[1,2,3],settings:{...defaults,variation:0},valid:()=>true,wait:async ms=>{at+=ms;return true;},out:row=>phases.push(['out',row,at]),swap:row=>phases.push(['in',row,at]),restore(){}});
 assert.equal(phases.find(v=>v[0]==='in')[2],800,'new image enters before the 2-second old-image fade finishes');
 assert.ok(phases.filter(v=>v[0]==='in').length===3);
 console.log('PASS: lazy account rhythm settings, custom save/reopen, reset, account/stale guards, randomized durations and overlapping arrivals');
})().catch(error=>{console.error(error);process.exitCode=1;});
