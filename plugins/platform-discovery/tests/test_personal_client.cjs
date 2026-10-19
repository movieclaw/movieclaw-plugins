"use strict";
// Expose controller internals only in this VM to reproduce delayed reads/account changes.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/personal.js"),"utf8").replace('return {open(){','return {state,initialize,updateShelf,measureWall,syncSubscriptions,refreshProfile,open(){');
const elements=new Map(),storage=new Map(),calls=[];let username="first",delayProfile=null;
function node(tag,text){return {tag,textContent:text||"",children:[],events:{},hidden:true,open:false,style:{values:new Map(),getPropertyValue(k){return this.values.get(k)||"";},setProperty(k,v){this.values.set(k,String(v));},removeProperty(k){this.values.delete(k);}},
  classList:{add(){},remove(){},toggle(){}},append(...rows){this.children.push(...rows);},replaceChildren(...rows){this.children=rows;},setAttribute(){},addEventListener(k,fn){this.events[k]=fn;},close(){this.open=false;},querySelector(){return this.hasPosters?{}:null;},getBoundingClientRect(){return {width:1200,top:200};}};}
const element=id=>{if(!elements.has(id))elements.set(id,node("div"));return elements.get(id);};
const document={getElementById:element,createTextNode:text=>node("text",text),body:{dataset:{mode:"personal"},classList:{toggle(){}}},addEventListener(){}};
const context={setInterval:()=>1,clearInterval(){},document,window:{addEventListener(){}},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},matchMedia:()=>({matches:false,addEventListener(){}}),getComputedStyle:()=>({gridTemplateColumns:"100px 100px 100px 100px 100px 100px",columnGap:"0"}),innerHeight:1000,innerWidth:1200,scrollY:0,MovieClawTransport:{request:async()=>({username,role:"member"})},setTimeout,clearTimeout};
vm.createContext(context);vm.runInContext(source,context);
const instance=context.MovieClawPersonal.create({node,button:node,image(){},showDetail(){},compactDate(){},announce(){},api:async(url,options={})=>{
 if(url==='/personal/filters')return {filters:{media_type:'all',countries:[],eras:[]},revision:''};
  calls.push({url,account:options.headers?.["X-Platform-Account"],body:options.body});
  if(url==="/personal/account"){if(delayProfile)await delayProfile;return {sync:{mode:"account"},profile:{has_test:false,revision:"v1"}};}
  if(url==="/personal/questionnaire")return {questions:[]};
  if(url==="/personal/subscriptions")return {has_test:false,learning:{enabled:true,saved:0,dismissed:0,subscriptions:JSON.parse(options.body).refs.length}};
  if(url==="/personal/feedback")return {saved:[],dismissed:[]};
  if(url==="/personal/profile")return {has_test:false};throw Error(url);
}});
(async()=>{
  let done;delayProfile=new Promise(resolve=>done=resolve);const first=instance.initialize(),second=instance.initialize();await Promise.resolve();await Promise.resolve();done();await Promise.all([first,second]);delayProfile=null;
  assert.equal(calls.filter(row=>row.url==="/personal/account").length,1,"concurrent startup creates one profile");
  assert.equal(instance.state.account,"member:first");
  element("poster-wall").hasPosters=true;element("poster-wall").style.setProperty("--wall-columns",4);element("poster-wall").style.setProperty("--wall-rows",3);instance.measureWall();
  assert.equal(element("poster-wall").style.getPropertyValue("--wall-rows"),"3","waiting for a larger batch keeps existing complete rows");assert.equal(element("poster-wall").style.getPropertyValue("--wall-columns"),"4");
  instance.state.loading=true;element("detail-dialog").open=true;element("poster-wall").append(node("private"));username="second";
  await assert.rejects(instance.updateShelf("save",{detail_ref:"tmdb:movie:42"}),/账号已切换/);
  assert.equal(instance.state.account,"member:second");assert.equal(instance.state.pendingReload,true);assert.equal(element("detail-dialog").open,false);assert.equal(element("poster-wall").children.length,0);
  assert.equal(calls.filter(row=>row.url==="/personal/feedback").length,0,"old-account actions must not write the new profile");
  assert.equal(storage.size,0,"new account profiles do not depend on local capabilities");
  await instance.updateShelf("save",{detail_ref:"tmdb:movie:43"});assert.equal(calls.at(-1).account,"member:second");
  let listDone;context.MovieClawTransport.request=async url=>url.endsWith("/subscriptions")?new Promise(resolve=>listDone=resolve):({username,role:"member"});
  const syncing=instance.syncSubscriptions(true);username="first";listDone([{media:{kind:"tv",tmdb_id:42}}]);await syncing;
  assert.equal(calls.filter(row=>row.url==="/personal/subscriptions").length,0,"an account switch during list read must not sync into another profile");
  username="second";context.MovieClawTransport.request=async url=>url.endsWith("/subscriptions")?[{media:{kind:"tv",tmdb_id:42}},{media:{kind:"tv",tmdb_id:42}},{media:{kind:"movie",tmdb_id:43}}]:({username,role:"member"});
  await instance.syncSubscriptions(true);assert.equal(instance.state.profile.learning.subscriptions,2);
  assert.equal(calls.filter(row=>row.url==="/personal/subscriptions").length,1);
  const oldRevision=instance.state.profile.revision;await instance.refreshProfile(true);assert.equal(calls.at(-1).url,"/personal/profile");
  console.log("PASS: profile singleflight, account partition, old-account write blocked, private DOM cleared and complete rows preserved during resize");
})().catch(error=>{console.error(error);process.exitCode=1;});
