"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
 .replace('return {open(){','return {state,loadWall,prepareNext,cancelNext,open(){')
 .replace('function measureWall(){','function measureWall(){return {count:6,columns:3,rows:2,height:600};')
 .replace('function renderWall(feed){','function renderWall(feed){state.qaRender=(state.qaRender||0)+1;state.qaFeed=feed;return;');
const nodes=new Map(),order=[];let shelfRelease,listRelease,warmRelease,settleRelease,warmNextRelease,preparedCalls=0,commitCalls=0,failCommit=false,allowPrepare=true,firstSettle=true,firstWarm=true;
function node(tag,text){return {tag,textContent:text||'',children:[],events:{},style:{setProperty(){},getPropertyValue(){return '';}},classList:{add(){},remove(){},toggle(){}},append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},setAttribute(){},addEventListener(k,f){this.events[k]=f;},querySelector(){return this.hasPosters?{}:null;},getBoundingClientRect(){return {width:900,top:200};},open:false};}
const element=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const document={getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){}}},addEventListener(){}};
const image=()=>node('img');image.warmWall=async()=>{};
image.settled=()=>firstSettle?new Promise(r=>settleRelease=r):Promise.resolve(true);image.canPrepare=()=>allowPrepare;image.cancelNext=()=>order.push('cancel-next');image.warmNext=()=>firstWarm?new Promise(r=>warmNextRelease=r):Promise.resolve(true);
const context={AbortController,document,window:{addEventListener(){}},innerWidth:900,localStorage:{getItem(){return null;}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},MovieClawTransport:{nonce:()=>String(order.length),request:async url=>{
 if(url.endsWith('/subscriptions')){order.push('subscriptions');return new Promise(r=>listRelease=r);}
 return {role:'member',username:'alice'};
}}};vm.createContext(context);vm.runInContext(source,context);
const controller=context.MovieClawPersonal.create({node,button:node,image,showDetail(){},compactDate(){},announce(){},api:async(url,options={})=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
 if(url.startsWith('/personal/recommendations/prepare?')){order.push('prepare-url:'+url);preparedCalls++;return {items:[{detail_ref:'tmdb:movie:prepared'}],prepared_ticket:'ticket'+preparedCalls,status:'stale'};}
 if(url==='/personal/recommendations/commit'){commitCalls++;if(failCommit)throw Error('409');return {items:[{detail_ref:'tmdb:movie:'+JSON.parse(options.body).ticket}]};}
 if(url.startsWith('/personal/recommendations?')){order.push('recommendations');return {items:Array.from({length:6},(_,i)=>({detail_ref:'tmdb:movie:'+i})),message:'real test candidates'};}
 if(url==='/personal/shelf'){order.push('shelf');return new Promise(r=>shelfRelease=r);}
 if(url==='/personal/subscriptions')return {revision:'v1',learning:{enabled:true}};
 throw Error(url);
}});
Object.assign(controller.state,{initialized:true,account:'member:alice',ready:Promise.resolve(),profile:{revision:'v1'},profileReadAt:Date.now()});
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 Object.assign(controller.state,{loaded:true,syncedAt:Date.now()});element('poster-wall').hasPosters=true;
 const task=controller.prepareNext();await tick();assert.equal(preparedCalls,0);firstSettle=false;settleRelease(true);await tick();assert.equal(preparedCalls,3);assert.equal(controller.state.nextWalls.length,3,'all three lists usable while warming is still unresolved');firstWarm=false;warmNextRelease(true);await task;
 assert.equal(controller.state.nextWalls.length,3,'real, revalidated cached candidates can prewarm after a resumed wall');assert.match(element('wall-next-status').textContent,/已备选 3 面/);assert.equal(preparedCalls,3);
 for(let i=1;i<=3;i++){await controller.loadWall();assert.equal(preparedCalls,3,"idle refill must not contend with rapid next-face commits");assert.equal(commitCalls,i);assert.equal(controller.state.qaFeed.items[0].detail_ref,'tmdb:movie:ticket'+i);}
 assert.equal(order.filter(x=>x==='recommendations').length,0,'three consecutive clicks use queued commits');assert.equal(controller.state.nextWalls.length,0);
 allowPrepare=true;controller.state.refillAfter=0;await controller.prepareNext();assert.equal(controller.state.nextWalls.length,3);failCommit=true;allowPrepare=false;await controller.loadWall();assert.equal(controller.state.nextWalls.length,0);assert.equal(order.filter(x=>x==='recommendations').length,1,'conflict clears whole queue and reselects latest eligible');
 failCommit=false;allowPrepare=true;controller.state.refillAfter=0;firstWarm=true;const canceled=controller.prepareNext();await tick();controller.cancelNext();firstWarm=false;warmNextRelease(true);await canceled;assert.equal(controller.state.nextWalls.length,0,'cancelled warming cannot promote stale batch');
 controller.state.country='KR';controller.state.era='1990s';controller.state.mediaType='tv';controller.state.refillAfter=0;await controller.prepareNext();assert.match(order.filter(x=>x.startsWith('prepare-url:')).at(-1),/media_type=tv&era=1990s&country=KR/);controller.cancelNext();
 controller.state.preload=false;await controller.prepareNext();assert.equal(controller.state.nextWalls.length,0);controller.cancelNext();
 console.log('PASS: three-face queue waits for visible posters, nonblocking three-list readiness, three fast commits, conflict whole-queue reset, cancellation and off');
})().catch(error=>{controller.state.preload=false;controller.cancelNext();console.error(error);process.exitCode=1;});
