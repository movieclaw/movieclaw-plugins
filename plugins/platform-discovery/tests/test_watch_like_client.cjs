"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
// Exercise real feedback UI independently of grid/network rendering (covered elsewhere).
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
  .replace('return {open(){','return {state,renderShelf,open(){')
  .replace('async function loadWall(force=false) {','async function loadWall(force=false) { state.qaReloads=(state.qaReloads||0)+1;return;');
const elements=new Map(),writes=[],announcements=[];let username='alice',held=null;
function node(tag,text){const result={tag,textContent:text||'',children:[],events:{},attrs:{},getAttribute(k){return this.attrs[k];},style:{setProperty(){}},classList:{add(){},remove(){},toggle(){}},append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,fn){this.events[k]=fn;},close(){this.open=false;},open:false};Object.defineProperty(result,'id',{set(value){elements.set(value,result);}});return result;}
const element=id=>{if(!elements.has(id))elements.set(id,node('div'));return elements.get(id);};
const button=(text,cls,handler)=>{const result=node('button',text);if(handler)result.events.click=handler;return result;};
const shelf={saved:[],dismissed:[],liked:[],watched:[]};
const context={document:{getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){}}},addEventListener(){}},window:{addEventListener(){}},localStorage:{getItem(){return null;}},matchMedia:()=>({matches:false,addEventListener(){}}),setInterval:()=>1,clearInterval(){},setTimeout,clearTimeout,MovieClawTransport:{request:async()=>({role:'member',username})}};
vm.createContext(context);vm.runInContext(source,context);
const instance=context.MovieClawPersonal.create({node,button,image:()=>node('img'),showDetail(){},compactDate(){},announce:t=>announcements.push(t),api:async(url,options={})=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
  if(url==='/personal/shelf')return structuredClone(shelf);
  if(url==='/personal/account')return {sync:{mode:'account'},profile:{revision:'v1',learning:{enabled:true}}};
  if(url==='/personal/questionnaire')return {questions:[]};
  if(url==='/personal/profile')return {revision:'v1',learning:{enabled:true}};
  if(url==='/personal/feedback'){
    const payload=JSON.parse(options.body);writes.push(payload);if(held)await held;
    const key={like:'liked',unlike:'liked',watch:'watched',unwatch:'watched'}[payload.action];
    if(!key)throw Error('unexpected action');
    shelf[key]=shelf[key].filter(row=>row.item.detail_ref!==payload.item.detail_ref);
    if(['like','watch'].includes(payload.action))shelf[key].push({item:payload.item});
    return structuredClone(shelf);
  }
  throw Error(url);
}});
instance.state.account='member:alice';instance.state.initialized=true;instance.state.ready=Promise.resolve();instance.state.profile={revision:'v1',learning:{enabled:true}};instance.state.profileReadAt=Date.now();
const value={detail_ref:'tmdb:movie:123',title:'A film',year:2000};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  let actions=instance.detailActions(value);await tick();const [save,like,watch]=actions.children;
  assert.equal(like.textContent,'喜欢');assert.equal(watch.attrs['aria-pressed'],'false');
  let release;held=new Promise(resolve=>release=resolve);const first=like.events.click();await tick();await like.events.click();
  assert.equal(writes.length,1,'duplicate click cannot issue a second write');assert.equal(watch.disabled,true);
  release();await first;held=null;assert.equal(like.textContent,'已喜欢 · 取消');assert.equal(like.attrs['aria-pressed'],'true');
  await watch.events.click();assert.equal(watch.textContent,'已看过 · 取消');assert.equal(instance.state.qaReloads,1,'mark watched requests a fresh wall');
  assert.equal(instance.state.profile.learning.liked,1);assert.equal(instance.state.profile.learning.watched,1);
  await watch.events.click();assert.equal(watch.textContent,'已看过');assert.equal(like.textContent,'已喜欢 · 取消','undo watching preserves explicit like');
  // Server changed on another device after the dialog opened: re-read before deciding toggle.
  shelf.liked=[];await like.events.click();assert.equal(writes.at(-1).action,'like');
  username='bob';await like.events.click();assert.equal(writes.length,4,'old-account dialog must not write after a switch');assert.match(announcements.at(-1),/账号已切换/);
  username='alice';instance.state.account='member:alice';instance.state.shelf={saved:[],dismissed:[],liked:[],watched:Array.from({length:60},(_,i)=>({item:{...value,detail_ref:'tmdb:movie:'+String(i+1)}}))};
  instance.renderShelf();const section=element('shelf-content').children.find(el=>el.tag==='details'&&el.children[0].textContent.startsWith('已看过'));
  assert.equal(section.children[1].children.length,0,'collapsed long history does not render posters');section.open=true;section.events.toggle();
  assert.equal(section.children[1].children.length,24);section.children[2].events.click();assert.equal(section.children[1].children.length,48);instance.renderShelf();const refreshed=element('shelf-watched');assert.equal(refreshed.open,true);assert.equal(refreshed.children[1].children.length,48,'background refresh preserves expanded page count');
  console.log('PASS: account-bound like/watch toggles, duplicate guard, re-read cross-device state, independent undo, watched refresh and lazy shelf pages');
})().catch(error=>{console.error(error);process.exitCode=1;});
