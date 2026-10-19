"use strict";
// 固定同源请求、统一超时和状态码；写请求绝不在这里自动重试。
globalThis.MovieClawTransport={
  // 换面标识不是鉴权凭证；HTTP 局域网仍支持 getRandomValues。
  nonce(){
    if(typeof globalThis.crypto?.randomUUID==="function")return globalThis.crypto.randomUUID().replaceAll("-","");
    if(typeof globalThis.crypto?.getRandomValues==="function"){const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);return Array.from(bytes,value=>value.toString(16).padStart(2,"0")).join("");}
    return Date.now().toString(36)+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2);
  },
  loginUrl(location=globalThis.location){
    const base="/api/v1/plugins/platform-discovery/panel";
    let target=location?.pathname?.startsWith("/apps/platform-discovery")?"/apps/platform-discovery/":base;
    const view=new URLSearchParams(location?.search||"").get("view");
    if(["personal","random","platforms","theater"].includes(view))target+="?view="+view;
    return "/login?next="+encodeURIComponent(target);
  },
  async request(path,options={},timeoutMs=30000){
    if(typeof path!=="string"||!path.startsWith("/api/v1/")||/[\\\r\n]/.test(path))throw new Error("请求地址无效。");
    const controller=new AbortController(),parent=options.signal;let timedOut=false;
    const abort=()=>controller.abort();if(parent?.aborted)abort();else parent?.addEventListener("abort",abort,{once:true});
    const timer=setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);
    try{
      const response=await fetch(path,{...options,signal:controller.signal,credentials:"same-origin",cache:"no-store"});
      if(response.status===401){
        if(globalThis.location?.protocol&&["http:","https:"].includes(globalThis.location.protocol)){
          const destination=globalThis.MovieClawTransport.loginUrl();
          try{(globalThis.window?.top?.location||globalThis.location).replace(destination);}catch{globalThis.location.replace(destination);}
        }
        const error=new Error("登录已过期，请重新登录。");error.status=401;throw error;
      }
      let body;try{body=await response.json();}catch(error){if(controller.signal.aborted)throw error;throw new Error("服务器暂未返回有效数据，请稍后重试。");}
      if(!body||typeof body!=="object"||Array.isArray(body))throw new Error("服务器返回的数据格式暂不可用。");
      if(!response.ok||body.success===false){
        const message=response.status===401?"登录已过期，请重新登录。":response.status===403?"当前账号没有操作权限，请联系管理员。":typeof body.detail==="string"?body.detail:typeof body.message==="string"?body.message:"请求失败，请稍后重试。";
        const error=new Error(message);error.status=response.status;throw error;
      }
      return body.data??body;
    }catch(error){
      if(timedOut)throw new Error(options.method==="POST"?"请求等待超时，结果可能已提交，请先重新检查状态。":"请求等待超时，已有片单会保留，请稍后重试。");
      if(parent?.aborted||error.status||error.name==="AbortError")throw error;
      if(error instanceof TypeError)throw new Error("网络连接中断，请检查连接后重试。");
      throw error;
    }finally{clearTimeout(timer);parent?.removeEventListener("abort",abort);}
  }
};
