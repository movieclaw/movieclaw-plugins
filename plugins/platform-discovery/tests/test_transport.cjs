"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/transport.js"),"utf8");
function setup(fetch,extra={}){const context={...extra,AbortController,TypeError,setTimeout,clearTimeout,fetch};vm.createContext(context);vm.runInContext(source,context);return context.MovieClawTransport;}
(async()=>{
  const http=setup(()=>{throw Error("nonce must not use network");},{crypto:{getRandomValues:require("node:crypto").webcrypto.getRandomValues.bind(require("node:crypto").webcrypto)}});
  const ids=new Set(Array.from({length:100},()=>http.nonce()));assert.equal(ids.size,100);assert.ok([...ids].every(id=>/^[0-9a-f]{32}$/.test(id)),"HTTP has no randomUUID but getRandomValues works");
  assert.match(setup(()=>{}).nonce(),/^[A-Za-z0-9]{1,64}$/,"non-auth nonce still works if crypto is unavailable");
  let count=0;const normal=setup(async(url,options)=>{count++;assert.equal(url,"/api/v1/auth/me");assert.equal(options.credentials,"same-origin");assert.equal(options.cache,"no-store");return {ok:true,json:async()=>({data:{username:"viewer"}})};});
  assert.equal((await normal.request("/api/v1/auth/me",{credentials:"include",cache:"force-cache"})).username,"viewer");
  for(const url of ["https://evil.example/api/v1/x","//evil.example/api/v1/x","/api/v1/\\evil","/api/v1/x\n"])await assert.rejects(normal.request(url),/地址无效/);
  assert.equal(count,1);
  const denied=setup(async()=>({ok:false,status:401,json:async()=>({detail:"expired"})}));await assert.rejects(denied.request("/api/v1/auth/me"),error=>error.status===401&&/登录已过期/.test(error.message));
  let writes=0;const blocked=setup((url,options)=>new Promise((resolve,reject)=>{writes++;const abort=()=>reject(Object.assign(new Error("aborted"),{name:"AbortError"}));if(options.signal.aborted)abort();else options.signal.addEventListener("abort",abort,{once:true});}));
  await assert.rejects(blocked.request("/api/v1/subscriptions",{method:"POST"},8),/结果可能已提交/);assert.equal(writes,1,"timeouts must never retry writes");
  for(const method of ["POST","post","PUT","PATCH","DELETE"]){
    let calls=0;
    const offline=setup(async()=>{calls++;throw new TypeError("Failed to fetch");});
    await assert.rejects(offline.request("/api/v1/subscriptions",{method}),/结果可能已提交/);
    assert.equal(calls,1,"network failures must never retry writes");
  }
  const offlineRead=setup(async()=>{throw new TypeError("Failed to fetch");});
  await assert.rejects(offlineRead.request("/api/v1/auth/me"),/网络连接中断/);
  const parent=new AbortController(),pending=blocked.request("/api/v1/auth/me",{signal:parent.signal});parent.abort();await assert.rejects(pending,error=>error.name==="AbortError");
  const malformed=setup(async()=>({ok:true,json:async()=>[] }));await assert.rejects(malformed.request("/api/v1/auth/me"),/格式暂不可用/);
  console.log("PASS: same-origin credentials, invalid URLs, auth status, bounded timeout, parent cancellation, malformed data and no write retry");
})().catch(error=>{console.error(error);process.exitCode=1;});
