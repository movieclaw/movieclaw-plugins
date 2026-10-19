"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/personal.js'),'utf8')
 .replace('return {open(){','return {state,loadWall,renderWall,open(){')
 .replace('function measureWall(){','function measureWall(){return {count:6,columns:3,rows:2,height:600};')
 .replace('function wallCard(item,index,feed,columns=state.layout?.columns||9) {','function wallCard(item,index,feed,columns=state.layout?.columns||9) {return node("article",item.detail_ref,"wall-tile");');
const nodes=new Map(),order=[];let shelfRelease,listRelease,warmRelease,presentRelease;
function node(tag,text){return {tag,textContent:text||'',children:[],events:{},style:{setProperty(){},getPropertyValue(){return '';}},classList:{add(){},remove(){},toggle(){}},append(...xs){this.children.push(...xs);},replaceChildren(...xs){this.children=xs;},setAttribute(){},addEventListener(k,f){this.events[k]=f;},querySelector(){return this.hasPosters?{}:null;},getBoundingClientRect(){return {width:900,top:200};},open:false,hidden:true,remove(){},querySelectorAll(){return this.children;}};}
const element=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const document={getElementById:element,createTextNode:t=>node('text',t),body:{dataset:{mode:'personal'},classList:{toggle(){}}},addEventListener(){}};
const image=()=>node('img');image.warmWall=async items=>{order.push('warm:'+items.length);await new Promise(r=>warmRelease=r);};
image.present=()=>{throw Error("render must never wait for images");};
const context={IntersectionObserver:class{observe(){}disconnect(){}},innerHeight:900,scrollY:0,document,window:{addEventListener(){}},innerWidth:900,localStorage:{getItem(){return null;}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},MovieClawTransport:{nonce:()=>String(order.length),request:async url=>{
 if(url.endsWith('/subscriptions')){order.push('subscriptions');return new Promise(r=>listRelease=r);}
 return {role:'member',username:'alice'};
}}};vm.createContext(context);vm.runInContext(source,context);
const controller=context.MovieClawPersonal.create({node,button:node,image,showDetail(){},compactDate(){},announce(){},api:async(url)=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
 if(url.startsWith('/personal/recommendations/resume?'))return {items:Array.from({length:6},(_,i)=>({detail_ref:'cached:'+i})),cache:{resume:true}};
 if(url.startsWith('/personal/recommendations?')){order.push('recommendations');return {items:Array.from({length:6},(_,i)=>({detail_ref:'tmdb:movie:'+i})),message:'real test candidates'};}
 if(url==='/personal/shelf'){order.push('shelf');return new Promise(r=>shelfRelease=r);}
 if(url==='/personal/subscriptions')return {revision:'v1',learning:{enabled:true}};
 throw Error(url);
}});
Object.assign(controller.state,{initialized:true,account:'member:alice',ready:Promise.resolve(),profile:{revision:'v1'},profileReadAt:Date.now()});
(async()=>{
 const wall=element('poster-wall'),old=node('article','OLD');wall.hasPosters=true;wall.children=[old];
 const feed={items:Array.from({length:6},(_,i)=>({detail_ref:'new:'+i})),message:'test'};
 await controller.renderWall(feed);assert.equal(wall.children.length,6);assert.equal(wall.children[0].textContent,'new:0','new metadata is shown without waiting on image promises');
 controller.state.loaded=false;await controller.loadWall();assert.equal(wall.children[0].textContent,'cached:0');assert.equal(order.filter(x=>x==='recommendations').length,0,'return visit resumes without new library selection');
 assert.equal(controller.state.seamless,false,'new layout defaults to cards');assert.equal(controller.state.immersive,false);
 console.log('PASS: immediate wall replacement independent of image readiness, account resume fast path and fullscreen/mosaic off defaults');
})().catch(e=>{console.error(e);process.exitCode=1;});
