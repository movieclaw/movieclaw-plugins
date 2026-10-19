"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
  .replace('return {open(){','return {state,wallCard,readShelf,open(){')
  .replace('async function loadWall(force=false) {','async function loadWall(force=false) { state.qaReloads=(state.qaReloads||0)+1;return;');
let username='alice',hover=true,phone=false,details=0,reads=0,held=null;const elements=new Map(),writes=[],notices=[],documentEvents={};
function node(tag,text){const n={tag,textContent:text||'',children:[],events:{},attrs:{},dataset:{},style:{setProperty(){}},classList:{add(){},remove(){},toggle(){}},getBoundingClientRect(){return {width:1200,top:200};},append(...xs){this.children.push(...xs);for(const x of xs)x.parent=this;},replaceChildren(...xs){this.children=[];this.append(...xs);},setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},addEventListener(k,fn){this.events[k]=fn;},contains(el){return !!el&&(this===el||this.children.some(c=>c.contains(el)));},matches:()=>false,focus(){document.activeElement=this;},close(){this.open=false;},open:false};Object.defineProperty(n,'id',{get(){return this.nodeId;},set(v){this.nodeId=v;elements.set(v,n);}});return n;}
const element=id=>{if(!elements.has(id))elements.set(id,node('div'));return elements.get(id);};
const button=(text,cls,action)=>{const n=node('button',text);if(action)n.events.click=action;return n;};
const document={activeElement:null,getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){}}},addEventListener(k,fn){(documentEvents[k]||=[]).push(fn);}};
const shelf={saved:[],liked:[],watched:[],dismissed:[]};
const context={document,window:{addEventListener(){}},localStorage:{getItem(){return null;}},matchMedia:q=>({matches:q==='(hover:hover)'?hover:q==='(max-width:767px)'?phone:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},MovieClawTransport:{request:async()=>({role:'member',username})}};
vm.createContext(context);vm.runInContext(source,context);
const controller=context.MovieClawPersonal.create({node,button,image:()=>node('img'),showDetail(){details++;},compactDate(){},announce:t=>notices.push(t),api:async(url,options={})=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
  if(url==='/personal/shelf'){reads++;return structuredClone(shelf);}
  if(url==='/personal/profile')return {revision:'v1',learning:{enabled:true}};
  if(url==='/personal/account')return {sync:{mode:'account'},profile:{revision:'v1',learning:{enabled:true}}};
  if(url==='/personal/questionnaire')return {questions:[]};
  if(url==='/personal/feedback'){
    const p=JSON.parse(options.body);writes.push(p);if(held)await held;
    const key={like:'liked',unlike:'liked',watch:'watched',unwatch:'watched',save:'saved',remove:'saved',hide:'dismissed',unhide:'dismissed'}[p.action];
    shelf[key]=shelf[key].filter(row=>(row.item?.detail_ref||row.detail_ref)!==p.item.detail_ref);
    if(['like','watch','save'].includes(p.action))shelf[key].push({item:p.item});
    if(p.action==='hide')shelf.dismissed.push({detail_ref:p.item.detail_ref,title:p.item.title});
    return structuredClone(shelf);
  }
  throw Error(url);
}});
Object.assign(controller.state,{account:'member:alice',initialized:true,ready:Promise.resolve(),profile:{revision:'v1',learning:{enabled:true}},profileReadAt:Date.now()});
const value=i=>({title:'影片'+i,detail_ref:'tmdb:movie:'+i,metadata:{},media_type:'movie',year:2000});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const event=()=>({pointerType:'mouse',stopped:false,prevented:false,stopPropagation(){this.stopped=true;},preventDefault(){this.prevented=true;}});
(async()=>{
  const cards=Array.from({length:30},(_,i)=>controller.wallCard(value(i+1),i,{items:[]}));
  assert.equal(reads,0,'rendering many posters must not issue a shelf request per card');
  const root=cards[0],[poster,toggle,panel]=root.children,[like,watch,save,hide]=panel.children;
  assert.equal(poster.tag,'button');assert.equal(poster.children.some(n=>n.tag==='button'),false,'actions must not be nested inside detail button');
  for(const r of cards.slice(0,5))r.events.pointerenter(event());await tick();
  assert.equal(reads,1,'multiple reveals share one shelf read');
  assert.equal(cards[3].dataset.quickOpen,'false');assert.equal(cards[4].dataset.quickOpen,'true','only one touch/hover panel open');
  root.events.pointerenter(event());await tick();assert.equal(root.dataset.quickOpen,'true');
  assert.equal(toggle.attrs['aria-expanded'],'true');assert.equal(panel.attrs.role,'group');
  const tap=event();await like.events.click(tap);assert.equal(tap.stopped,true);assert.equal(writes.at(-1).action,'like');assert.equal(like.attrs['aria-pressed'],'true');assert.equal(details,0);
  await like.events.click(event());assert.equal(writes.at(-1).action,'unlike');
  await save.events.click(event());assert.equal(writes.at(-1).action,'save');assert.match(save.textContent,/✓/);assert.equal(details,0);
  // Use the other card's distinct like while the first is pending: global guard prevents interleaving toggles.
  let release;held=new Promise(resolve=>release=resolve);const pending=like.events.click(event());await tick();const count=writes.length;
  await cards[1].children[2].children[0].events.click(event());assert.equal(writes.length,count);release();await pending;held=null;
  await watch.events.click(event());assert.equal(writes.at(-1).action,'watch');assert.equal(controller.state.qaReloads,1);
  await hide.events.click(event());assert.equal(writes.at(-1).action,'hide');assert.equal(controller.state.qaReloads,2);
  // Touch never depends on hover; explicit trigger is independent of opening details.
  hover=false;const touch=cards[5];touch.events.pointerenter({pointerType:'touch'});assert.notEqual(touch.dataset.quickOpen,'true');
  const touchTap=event();touch.children[1].events.click(touchTap);await tick();assert.equal(touch.dataset.quickOpen,'true');assert.equal(touchTap.stopped,true);assert.equal(root.dataset.quickOpen,'false');
  const escape=event();escape.key='Escape';touch.events.keydown(escape);assert.equal(escape.stopped,true);assert.equal(escape.prevented,true);assert.equal(touch.dataset.quickOpen,'false');
  touch.children[1].events.click(event());for(const fn of documentEvents.pointerdown)fn({target:element('outside')});assert.equal(touch.dataset.quickOpen,'false');
  // Narrow desktop emulation and touch use the same explicit toggle: no
  // pointer-enter/click double toggle, and lifting a finger cannot hide actions.
  phone=true;hover=true;const narrow=cards[6];narrow.events.pointerenter(event());assert.notEqual(narrow.dataset.quickOpen,'true');
  narrow.children[1].events.click(event());assert.equal(narrow.dataset.quickOpen,'true');
  narrow.events.pointerleave({pointerType:'touch'});assert.equal(narrow.dataset.quickOpen,'true');
  narrow.events.pointerleave({pointerType:'mouse'});assert.equal(narrow.dataset.quickOpen,'true');
  narrow.children[1].events.click(event());assert.equal(narrow.dataset.quickOpen,'false');phone=false;
  // Keyboard focus reveals actions; Enter on the poster still opens details normally.
  root.events.focusin({target:{matches:()=>true}});assert.equal(root.dataset.quickOpen,'true');poster.events.click();assert.equal(details,1);
  username='bob';const before=writes.length;await like.events.click(event());assert.equal(writes.length,before,'stale account cards cannot submit into a new account');assert.match(notices.at(-1),/账号已切换/);
  console.log('PASS: hover/touch/keyboard quick actions, separate detail target, single shelf read, account safety, duplicate guard, explicit mutations and watched/hidden refresh');
})().catch(error=>{console.error(error);process.exitCode=1;});
