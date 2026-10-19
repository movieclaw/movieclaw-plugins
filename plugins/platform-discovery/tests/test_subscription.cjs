"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const code=fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/subscription.js"),"utf8");
function setup(kind="tv",options={}){
  const calls=[],busy=[],announcements=[];
  const prepared={status:"ready",media:{kind,tmdb_id:42,title:"真实作品",year:2020,status:"Ended"},seasons:[{season_number:0,episode_count:1,aired_count:1},{season_number:1,episode_count:8,aired_count:8},{season_number:2,episode_count:10,aired_count:6}],suggested_seasons:[],...options.prepared};
  const context={AbortController,setTimeout,clearTimeout,TypeError,fetch:async(url,init)=>{
    const body=init.body?JSON.parse(init.body):undefined;calls.push({url,...init,body});
    let data,status=200;
    if(url.endsWith("/auth/me"))data={username:options.switched?"other":"viewer",role:options.admin?"admin":"member",capabilities:{allow_subscribe:!options.denied}};
    else if(url.includes("/download-routing-preview?"))data={library_name:"现有剧集库",ok:false,warning:"映射需要检查"};
    else if(url.endsWith("/title-preview")){if(options.waitPreview)await options.waitPreview;data=prepared;status=options.previewStatus||200;}
    else if(url.includes("/smart-profiles/"))data={kind,revision:4,preferences:options.rules?null:{resolution:"2160p",source:"web-dl",wait_seconds:10800}};
    else if(url.endsWith("/subscriptions")){if(options.waitCreate)await options.waitCreate;if(options.createError)throw new TypeError("offline");data={subscription:{id:17,media:{kind,tmdb_id:42}}};}
    else throw Error("Unexpected endpoint: "+url);
    return {ok:status===200,status,json:async()=>({success:status===200,data,message:status===403?"禁止订阅":"ok"})};
  }};
  function node(tag,text,cls){return {tag,textContent:text==null?"":String(text),className:cls||"",children:[],events:{},attrs:{},hidden:false,disabled:false,checked:false,
    setAttribute(k,v){this.attrs[k]=v;},append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;},
    addEventListener(type,handler){this.events[type]=handler;},focus(){},scrollIntoView(){},
    querySelectorAll(selector){const tags=selector.split(",");return this.children.flatMap(child=>[...(tags.includes(child.tag)?[child]:[]),...child.querySelectorAll(selector)]);}};}
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/transport.js"),"utf8"),context);vm.runInContext(code,context);
  const item={title:"真实作品",media_type:kind,detail_ref:options.ref||"tmdb:"+kind+":42"};
  const instance=context.MovieClawSubscriptions.create({item,node,button(text,cls,handler){const element=node("button",text,cls);if(handler)element.events.click=handler;return element;},onBusy:value=>busy.push(value),announce:value=>announcements.push(value)});
  const find=(predicate,root=instance.root)=>predicate(root)?root:root.children.map(child=>find(predicate,child)).find(Boolean);
  const change=(input,checked)=>{input.checked=checked;input.events.change();};
  const submit=()=>find(n=>n.tag==="form").events.submit({preventDefault(){}});
  const creates=()=>calls.filter(c=>c.url==="/api/v1/subscriptions");
  return {instance,calls,busy,announcements,find,change,submit,creates,options,prepared};
}
(async()=>{
  const quick=setup("tv",{admin:true});await quick.instance.open();
  assert.ok(quick.calls.some(c=>c.url.includes("/download-routing-preview?")));
  quick.find(n=>n.tag==="button"&&n.textContent==="清空").events.click();assert.equal(quick.find(n=>n.type==="submit").disabled,true);
  quick.find(n=>n.tag==="button"&&n.textContent==="全选正季").events.click();
  assert.deepEqual(quick.instance.root.querySelectorAll("input").filter(n=>n.checked).map(n=>n.value),["1","2"]);
  assert.equal(quick.creates().length,0,"quick selection and routing preview cannot create");
  const tv=setup();await tv.instance.open();assert.equal(tv.creates().length,0,"previewing cannot create a subscription");
  const seasons=tv.instance.root.querySelectorAll("input").filter(n=>n.value!==undefined);
  assert.deepEqual(seasons.filter(n=>n.checked).map(n=>n.value),["1","2"],"specials are not implicitly selected");
  tv.change(seasons[1],false);tv.change(seasons[0],true);await tv.submit();
  assert.deepEqual(tv.creates()[0].body,{title_ref:"tmdb:tv:42",selected_seasons:[0,2],follow_future:false,selection_mode:"smart",smart_profile_revision:4});
  assert.equal(tv.instance.entry.textContent,"已订阅");assert.deepEqual(tv.busy,[true,false]);
  assert.ok(tv.calls.every(c=>c.credentials==="same-origin"&&c.cache==="no-store"));
  assert.ok(tv.calls.every(c=>!c.headers.Authorization&&!c.headers["X-Platform-Profile"]),"no admin/profile credential is sent to subscription APIs");
  const movie=setup("movie",{rules:true});await movie.instance.open();assert.equal(movie.creates().length,0);await movie.submit();
  assert.deepEqual(movie.creates()[0].body,{title_ref:"tmdb:movie:42",selected_seasons:[],follow_future:false,selection_mode:"rules"});
  assert.ok(!("rule_set_id" in movie.creates()[0].body)&&!("library_id" in movie.creates()[0].body),"saved routing/settings must remain server-selected");
  const empty=setup();await empty.instance.open();for(const input of empty.instance.root.querySelectorAll("input").filter(n=>n.checked))empty.change(input,false);
  assert.equal(empty.find(n=>n.type==="submit").disabled,true);await empty.submit();assert.equal(empty.creates().length,0);
  const follow=empty.instance.root.querySelectorAll("input").find(n=>n.value===undefined);empty.change(follow,true);await empty.submit();assert.equal(empty.creates()[0].body.follow_future,true);
  const denied=setup("tv",{denied:true});await denied.instance.open();assert.equal(denied.calls.length,1);assert.match(denied.find(n=>n.attrs.role==="alert").textContent,/未获准/);
  const forbidden=setup("tv",{previewStatus:403});await forbidden.instance.open();assert.equal(forbidden.creates().length,0);assert.match(forbidden.find(n=>n.attrs.role==="alert").textContent,/没有操作权限/);
  const switched=setup();await switched.instance.open();switched.options.switched=true;await switched.submit();assert.equal(switched.creates().length,0);assert.match(switched.find(n=>n.attrs.role==="alert").textContent,/账号已切换/);
  let resolve;const waitCreate=new Promise(done=>resolve=done),double=setup("movie",{waitCreate});await double.instance.open();const first=double.submit();await double.submit();
  for(let i=0;i<10;i++)await Promise.resolve();assert.equal(double.creates().length,1);assert.equal(double.instance.isSubmitting(),true);resolve();await first;
  const existing=setup("tv",{prepared:{existing_subscription_id:7}});await existing.instance.open();assert.equal(existing.find(n=>n.tag==="form"),undefined);assert.equal(existing.creates().length,0);
  const stale=setup("tv",{waitPreview:new Promise(done=>resolve=done)});const loading=stale.instance.open();for(let i=0;i<5;i++)await Promise.resolve();stale.instance.dispose();resolve();await loading;assert.equal(stale.find(n=>n.tag==="form"),undefined);
  const failed=setup("movie",{createError:true});await failed.instance.open();await failed.submit();assert.equal(failed.creates().length,1);assert.match(failed.find(n=>n.attrs.role==="alert").textContent,/重新检查/);assert.equal(failed.find(n=>n.tag==="form"),undefined,"uncertain results require rechecking, never auto retry writes");
  const mismatch=setup("tv",{prepared:{media:{kind:"movie",tmdb_id:42,title:"Other"}}});await mismatch.instance.open();assert.match(mismatch.find(n=>n.attrs.role==="alert").textContent,/身份不一致/);
  const invalid=setup("tv",{ref:"https://evil.example/"});await invalid.instance.open();assert.equal(invalid.calls.length,0);assert.equal(invalid.instance.entry.disabled,true);
  console.log("PASS: explicit-confirm movie/TV create, season/follow payloads, preserved smart/default routing, current-user auth, deny/account switch, duplicate submit, existing subscription, disposed preview, uncertain write and identity guards");
})().catch(error=>{console.error(error);process.exitCode=1;});
