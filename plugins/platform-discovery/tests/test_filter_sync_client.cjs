"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
 .replace('return {open(){','return {state,initialize,refreshFilters,saveFilters,currentFilters,open(){')
 .replace('async function loadWall(force=false) {','async function loadWall(force=false) {state.qaReloads=(state.qaReloads||0)+1;return;');
const nodes=new Map(),calls=[];let username='alice',counter=0,delayRead=null,delayWrite=null;
function node(tag,text){return {textContent:text||'',children:[],events:{},open:false,hidden:true,style:{setProperty(){},getPropertyValue(){return '';}},classList:{add(){},remove(){},toggle(){}},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;},setAttribute(){},addEventListener(k,f){this.events[k]=f;},querySelector(){return null;},querySelectorAll(){return [];},close(){this.open=false;},getBoundingClientRect(){return {width:900,top:200};}};}
const element=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const records=new Map(),empty=()=>({filters:{media_type:'all',countries:[],eras:[]},revision:''});
const document={getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){},add(){},remove(){}}},addEventListener(){}};
const context={document,window:{addEventListener(){}},innerWidth:900,localStorage:{getItem(){return null;}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},MovieClawTransport:{request:async()=>({role:'member',username})}};vm.createContext(context);vm.runInContext(source,context);
const controller=context.MovieClawPersonal.create({node,button:node,image:()=>node('img'),showDetail(){},compactDate(){},announce(){},api:async(url,options={})=>{
 calls.push({url,method:options.method||'GET',account:options.headers?.['X-Platform-Account']});const account=options.headers?.['X-Platform-Account'];
 if(url==='/personal/account')return {sync:{mode:'account'},profile:{revision:'taste1',has_test:false}};
 if(url==='/personal/questionnaire')return {questions:[]};
 if(url==='/personal/profile')return {revision:'taste1',has_test:false};
 if(url==='/personal/filters'){
  if(options.method!=='POST'){const value=JSON.parse(JSON.stringify(records.get(account)||empty()));if(delayRead)await delayRead;return value;}
  const selection=JSON.parse(options.body).selection;if(delayWrite)await delayWrite;
  const current=records.get(account)||empty();if(current.revision!==selection.revision){const error=Error('另一设备已更新筛选');error.status=409;throw error;}
  const value={filters:{media_type:selection.media_type,countries:[...selection.countries].sort(),eras:[...selection.eras].sort()},revision:(++counter).toString(16).padStart(24,'0')};records.set(account,value);return value;
 }
 throw Error(url);
}});
(async()=>{
 await controller.initialize();assert.equal(controller.state.country,'all');
 let release;delayWrite=new Promise(r=>release=r);
 const selection={media_type:'tv',countries:['KR','JP'],eras:['2000s','1990s']};const first=controller.saveFilters(selection),duplicate=controller.saveFilters(selection);await new Promise(r=>setImmediate(r));assert.equal(await duplicate,false);assert.equal(calls.filter(c=>c.method==='POST'&&c.url==='/personal/filters').length,1,'save guard covers authentication wait');release();assert.equal(await first,true);delayWrite=null;
 assert.equal(controller.state.country,'JP,KR');assert.equal(controller.state.era,'1990s,2000s');assert.equal(controller.state.mediaType,'tv');
 controller.state.loaded=true;controller.state.nextWalls=[{feed:{prepared_ticket:'old'}}];records.set('member:alice',{filters:{media_type:'movie',countries:['US'],eras:['1980s']},revision:'a'.repeat(24)});await controller.refreshFilters(true);
 assert.equal(controller.state.country,'US');assert.equal(controller.state.era,'1980s');assert.equal(controller.state.nextWalls.length,0,'remote filters discard speculative walls');assert.equal(controller.state.loaded,false);
 const stale=await controller.saveFilters(selection,'0'.repeat(24));assert.equal(stale,false);assert.equal(records.get('member:alice').filters.countries[0],'US','stale form cannot overwrite another device');assert.equal(controller.state.filtersSaving,false);
 let readRelease;delayRead=new Promise(r=>readRelease=r);const oldRead=controller.refreshFilters(true);await new Promise(r=>setImmediate(r));const newSave=controller.saveFilters(selection,controller.state.filtersRevision);await new Promise(r=>setImmediate(r));assert.equal(await newSave,true);delayRead=null;readRelease();await oldRead;assert.equal(controller.state.country,'JP,KR','late read cannot roll back a successful save');
 username='bob';await controller.initialize();assert.equal(controller.state.country,'all');assert.equal(controller.state.era,'all');assert.equal(controller.state.mediaType,'all','other account has independent filter defaults');
 console.log('PASS: account filter restoration, atomic multi-select save, duplicate guard, cross-device invalidation, conflict protection, stale reads and account switching');
})().catch(e=>{console.error(e);process.exitCode=1;});
