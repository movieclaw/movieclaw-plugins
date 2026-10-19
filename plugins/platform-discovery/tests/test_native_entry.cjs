"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const script=fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/page.js"),"utf8");
const functions=script.slice(script.indexOf("  function nativeTarget("),script.indexOf("  function posterCard("));
function make(){
  const elements=new Map(),navigations=[];let status={available:false,state:"loading",message:"影片资料服务正在连接，请稍后重试。"},calls=0,resolve;
  function node(tag,text,cls){return {tag,textContent:text==null?"":String(text),className:cls||"",children:[],events:new Map(),attrs:{},style:{},hidden:false,open:false,
    classList:{add(){},remove(){},toggle(){return false;}},append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},
    setAttribute(key,value){this.attrs[key]=value;},removeAttribute(key){delete this.attrs[key];},showModal(){this.open=true;},
    addEventListener(type,handler){this.events.set(type,handler);}};}
  const $=id=>{if(!elements.has(id))elements.set(id,node("div"));return elements.get(id);};
  const context={AbortController,detailReadController:null,personal:{detailActions:()=>node("div")},detailGeneration:0,detailSubscription:null,detailNavigation:null,MovieClawSubscriptions:{create:()=>({entry:node("button","订阅影片"),root:node("section"),isSubmitting:()=>false,dispose(){}})},state:{region:"US"},preview:false,$,node,
    document:{body:{classList:{add(){}}}},location:{assign:target=>navigations.push(target)},
    button(text,cls,handler){const element=node("button",text,cls);if(handler)element.addEventListener("click",handler);return element;},
    placeholder:title=>node("div",title),image:()=>null,compactDate:value=>value,externalLink:()=>null,safeUrl:()=>null,announce(){},
    api:async url=>{if(url.startsWith("/public-titles/"))return {};calls++;return status===null?new Promise(done=>resolve=done):status;}};
  vm.createContext(context);vm.runInContext(functions,context);
  const open=(title="Example")=>context.showDetail({title,media_type:"movie",detail_ref:"tmdb:movie:42",poster_url:null},
    {id:"personal",name:"私人海报墙",url:"https://www.themoviedb.org/"},{source:{label:"私人推荐"}});
  function find(cls,element=$("detail-content")){return element.className===cls?element:element.children.map(child=>find(cls,child)).find(Boolean);}
  const click=()=>{const actions=find("detail-actions"),link=actions.children.find(child=>child.tag==="a");return {link,promise:link.events.get("click")({preventDefault(){}})};};
  return {context,$,open,find,click,navigations,setStatus(value){status=value;},finish(value){resolve(value);},get calls(){return calls;}};
}
(async()=>{
  const navigation=make(),batch=[{title:"One",detail_ref:"tmdb:movie:42",media_type:"movie"},{title:"Two",detail_ref:"tmdb:movie:43",media_type:"movie"}];
  navigation.context.showDetail(batch[0],{id:"personal",name:"私人海报墙"},{items:batch,source:{}});
  assert.equal(navigation.$("detail-position").textContent,"1 / 2");assert.equal(navigation.$("detail-previous").disabled,true);
  navigation.context.moveDetail(1);assert.equal(navigation.$("detail-header-title").textContent,"Two");assert.equal(navigation.$("detail-next").disabled,true);
  navigation.context.moveDetail(1);assert.equal(navigation.$("detail-header-title").textContent,"Two");
  navigation.context.detailSubscription.isSubmitting=()=>true;navigation.context.moveDetail(-1);assert.equal(navigation.$("detail-header-title").textContent,"Two");
  const test=make();test.open();let clicked=test.click();await clicked.promise;
  assert.equal(test.navigations.length,0,"A loading native service must keep the user inside the plugin");
  assert.equal(test.find("detail-native-status").hidden,false);assert.match(test.find("detail-native-status").textContent,/正在连接/);
  assert.equal(clicked.link.textContent,"重试 MovieClaw 入口");assert.equal(clicked.link.attrs["aria-disabled"],undefined);
  test.setStatus({available:true});await test.click().promise;assert.deepEqual(test.navigations,["/media/movie/42"]);
  const pending=make();pending.open();pending.setStatus(null);clicked=pending.click();const duplicate=pending.click();await duplicate.promise;
  assert.equal(pending.calls,1,"Repeated clicks share the active readiness check");assert.equal(clicked.link.attrs["aria-disabled"],"true");
  pending.open("Another item");pending.finish({available:true});await clicked.promise;
  assert.equal(pending.navigations.length,0,"An old result cannot navigate after the selected title changes");
  const closed=make();closed.open();closed.setStatus(null);clicked=closed.click();closed.$("detail-dialog").open=false;
  closed.finish({available:true});await clicked.promise;assert.equal(closed.navigations.length,0,"Closing details cancels pending navigation");
  console.log("PASS: loading readiness stays in plugin with retry, ready-only navigation, duplicate-click guard, changed/closed-detail stale responses ignored");
})().catch(error=>{console.error(error);process.exitCode=1;});
