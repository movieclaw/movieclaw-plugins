"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
 .replace('return {open(){','return {state,loadWall,open(){')
 .replace('function measureWall(){','function measureWall(){return {count:6,columns:3,rows:2,height:600};')
 .replace('function renderWall(feed){','function renderWall(feed){state.qaRender=(state.qaRender||0)+1;state.qaFeed=feed;return;');
const nodes=new Map(),order=[];let shelfRelease,listRelease,warmRelease;
function node(tag,text){return {tag,textContent:text||'',children:[],events:{},style:{setProperty(){},getPropertyValue(){return '';}},classList:{add(){},remove(){},toggle(){}},append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},setAttribute(){},addEventListener(k,f){this.events[k]=f;},querySelector(){return this.hasPosters?{}:null;},getBoundingClientRect(){return {width:900,top:200};},open:false};}
const element=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const document={getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){}}},addEventListener(){}};
const image=()=>node('img');image.warmWall=async items=>{order.push('warm:'+items.length);await new Promise(r=>warmRelease=r);};
const context={MovieClawAmbient:{create:()=>({activity(){},flush:async()=>{},wake(){assert.equal(controller.state.loading,false,"idle scheduler wakes only after layout/feed loading is released");order.push("idle-ready");}})},document,window:{addEventListener(){}},innerWidth:900,localStorage:{getItem(){return null;}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},MovieClawTransport:{nonce:()=>String(order.length),request:async url=>{
 if(url.endsWith('/subscriptions')){order.push('subscriptions');return new Promise(r=>listRelease=r);}
 return {role:'member',username:'alice'};
}}};vm.createContext(context);vm.runInContext(source,context);
const controller=context.MovieClawPersonal.create({node,button:node,image,showDetail(){},compactDate(){},announce(){},api:async(url)=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
 if(url.startsWith('/personal/recommendations?')){order.push('recommendations');return {items:Array.from({length:6},(_,i)=>({detail_ref:'tmdb:movie:'+i})),message:'real test candidates'};}
 if(url==='/personal/shelf'){order.push('shelf');return new Promise(r=>shelfRelease=r);}
 if(url==='/personal/subscriptions')return {revision:'v1',learning:{enabled:true}};
 throw Error(url);
}});
Object.assign(controller.state,{initialized:true,account:'member:alice',ready:Promise.resolve(),profile:{revision:'v1'},profileReadAt:Date.now()});
(async()=>{
 const result=await Promise.race([controller.loadWall(),new Promise((_,reject)=>setTimeout(()=>reject(Error('first wall blocked on background profile work')),500))]);
 assert.equal(controller.state.qaRender,1);assert.ok(order.includes('idle-ready'));assert.ok(order.indexOf('recommendations')<order.indexOf('shelf'));assert.ok(listRelease&&shelfRelease,'both slow background requests started without blocking render');
 shelfRelease({saved:[],dismissed:[],liked:[],watched:[]});listRelease([]);await new Promise(r=>setImmediate(r));
 element('poster-wall').hasPosters=true;const before=order.filter(x=>x==='recommendations').length;
 await controller.loadWall();assert.equal(controller.state.qaRender,2);assert.equal(order.filter(x=>x==='recommendations').length,before+1);
 controller.state.preload=false;await controller.loadWall();assert.equal(controller.state.qaRender,3,'mandatory batch presentation is independent of optional background preload');
 console.log('PASS: wall render precedes slow shelf/subscription sync; no extra preheat delay, single recommendation and preload-off paths');
})().catch(error=>{console.error(error);process.exitCode=1;});
