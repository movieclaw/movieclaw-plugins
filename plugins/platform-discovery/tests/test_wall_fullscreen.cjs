"use strict";
// Exercise the real controller with DOM events; no browser or network required.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/personal.js"),"utf8");
function make({reject=false,deferred=false}={}) {
  const listeners=new Map(),elements=new Map(),timers=new Map(),notices=[];let timerId=0,requests=0,exits=0,finish;
  const classes=new Set();
  const classList={add(...names){names.forEach(name=>classes.add(name));},remove(...names){names.forEach(name=>classes.delete(name));},
    toggle(name,enabled=!classes.has(name)){enabled?classes.add(name):classes.delete(name);return enabled;},contains:name=>classes.has(name)};
  const document={body:{dataset:{mode:"personal"},classList},activeElement:null,fullscreenElement:null,dialogOpen:false,
    addEventListener(type,handler){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(handler);},
    querySelector(selector){return selector==="dialog[open]"&&this.dialogOpen?{}:null;},getElementById:id=>element(id)};
  const emitDocument=(type,event={})=>(listeners.get(type)||[]).forEach(handler=>handler(event));
  function element(id){
    if(!elements.has(id))elements.set(id,{id,attrs:{},hidden:["immersive-tools","immersive-trigger"].includes(id),events:new Map(),
      setAttribute(key,value){this.attrs[key]=value;},addEventListener(type,handler){if(!this.events.has(type))this.events.set(type,[]);this.events.get(type).push(handler);},
      focus(){document.activeElement=this;(this.events.get("focus")||[]).forEach(fn=>fn({}));},
      matches(){return !!this.hovered||this.id==="immersive-tools"&&["immersive-shuffle","immersive-taste","immersive-exit"].includes(document.activeElement?.id);}});
    return elements.get(id);
  }
  document.documentElement={requestFullscreen(){requests++;
    if(reject)return Promise.reject(new Error("Browser denied fullscreen"));
    if(deferred)return new Promise(resolve=>{finish=()=>{document.fullscreenElement=document.documentElement;emitDocument("fullscreenchange");resolve();};});
    document.fullscreenElement=document.documentElement;emitDocument("fullscreenchange");return Promise.resolve();}};
  document.exitFullscreen=()=>{exits++;document.fullscreenElement=null;emitDocument("fullscreenchange");return Promise.resolve();};
  const context={setInterval:()=>1,clearInterval(){},document,window:{addEventListener(){}},localStorage:{getItem(){return null;},setItem(){}},
    matchMedia:()=>({matches:false,addEventListener(){}}),scrollY:210,scrollTo(){},
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(source,context);
  const controller=context.MovieClawPersonal.create({api(){throw Error("Fullscreen must not request a feed before opening the wall");},
    node(){},button(){},image(){},showDetail(){},compactDate(){},announce:value=>notices.push(value)});
  const fire=async(id,type="click",event={})=>{for(const handler of element(id).events.get(type)||[])await handler(event);};
  return {document,controller,element,fire,emitDocument,timers,classes,notices,finish:()=>finish(),
    get requests(){return requests;},get exits(){return exits;}};
}
async function main(){
  {const test=make();await test.fire("wall-fullscreen-launch");assert(test.classes.has("wall-immersive"));assert.equal(test.requests,1,"visible launcher requests native browser fullscreen in the original click gesture");assert.equal(test.document.body.dataset.fullscreenMode,"native");assert.equal(test.element("immersive-title").textContent,"浏览器全屏");await test.fire("immersive-exit");assert(!test.classes.has("wall-immersive"));}

  {
    const test=make();await test.fire("wall-fullscreen");
    assert.equal(test.requests,1);assert.equal(test.document.fullscreenElement,test.document.documentElement);
    assert(test.classes.has("wall-immersive"));assert.equal(test.element("wall-fullscreen").attrs["aria-checked"],"true");
    assert.equal(test.element("immersive-trigger").hidden,false);assert.equal(test.element("immersive-tools").hidden,false);
    const latestTimer=()=>Array.from(test.timers.values()).at(-1).fn();
    latestTimer();assert.equal(test.classes.has("immersive-controls-visible"),false,"Idle tools must leave the poster field unobstructed");
    await test.fire("immersive-trigger","pointerenter");assert(test.classes.has("immersive-controls-visible"));
    test.element("immersive-tools").hovered=true;latestTimer();assert(test.classes.has("immersive-controls-visible"),"Hovered tools remain usable");
    test.element("immersive-tools").hovered=false;await test.fire("immersive-tools","pointerleave");latestTimer();
    assert.equal(test.classes.has("immersive-controls-visible"),false);
    test.element("immersive-trigger").focus();latestTimer();assert(test.classes.has("immersive-controls-visible"),"Focused edge handle remains visible");
    test.document.activeElement=null;await test.fire("immersive-trigger","blur");latestTimer();
    assert.equal(test.classes.has("immersive-controls-visible"),false,"Leaving the focused edge handle must restart auto-hide");
    test.document.dialogOpen=true;let prevented=false;
    test.emitDocument("keydown",{key:"Escape",preventDefault(){prevented=true;}});
    assert.equal(prevented,false,"Escape belongs to an open detail/questionnaire dialog first");
    assert(test.classes.has("wall-immersive"));test.document.dialogOpen=false;
    // Native Esc can arrive as fullscreenchange without a page keydown.
    test.document.fullscreenElement=null;test.emitDocument("fullscreenchange");
    assert.equal(test.classes.has("wall-immersive"),true,"native fullscreen exit must not silently stop a visible screensaver");
    assert.equal(test.document.body.dataset.fullscreenMode,"window");assert.equal(test.element("immersive-native").hidden,false);
    await test.fire("immersive-native");assert.equal(test.requests,2,"window mode can re-enter native fullscreen from an explicit click");
    test.emitDocument("keydown",{key:"Escape",preventDefault(){}});
    assert.equal(test.classes.has("wall-immersive"),false);assert.equal(test.element("immersive-tools").hidden,true);
    assert.equal(test.document.activeElement,test.element("wall-fullscreen"));
  }
  {
    const test=make({reject:true});await test.fire("wall-fullscreen");
    assert(test.classes.has("wall-immersive"),"A denied native request retains the browser-window mosaic");assert.equal(test.notices.length,1);assert.equal(test.document.body.dataset.fullscreenMode,"window");
    await test.fire("immersive-trigger","focus");assert.equal(test.element("immersive-trigger").attrs["aria-expanded"],"true");
    await test.fire("immersive-trigger");assert.equal(test.document.activeElement,test.element("immersive-shuffle"));
    test.emitDocument("keydown",{key:"Escape",preventDefault(){}});
    assert.equal(test.classes.has("wall-immersive"),false);assert.equal(test.exits,0);
  }
  {
    const test=make();await test.fire("wall-fullscreen");test.controller.leave();
    assert.equal(test.exits,1);assert.equal(test.document.fullscreenElement,null);assert.equal(test.classes.has("wall-immersive"),false);
  }
  {
    const test=make({deferred:true}),opening=test.fire("wall-fullscreen");
    test.controller.leave();test.document.body.dataset.mode="platforms";test.finish();await opening;
    assert.equal(test.classes.has("wall-immersive"),false);
    assert.equal(test.document.fullscreenElement,null,"A late native success must not fullscreen an already-exited page");
    assert.equal(test.exits,1);
  }
  {
    const test=make({deferred:true}),opening=test.fire("wall-fullscreen");
    // Native state may be set before fullscreenchange or request resolution.
    test.document.fullscreenElement=test.document.documentElement;test.controller.leave();
    assert.equal(test.document.fullscreenElement,null,"Owned document fullscreen must exit even before the native flag is set");
    test.document.body.dataset.mode="platforms";test.finish();await opening;
    assert.equal(test.document.fullscreenElement,null);
  }
  console.log("PASS: native/fallback entry, dialog Escape priority, keyboard controls, native Escape, leave cleanup, delayed-request races");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
