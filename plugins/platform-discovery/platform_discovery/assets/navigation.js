"use strict";
const MC_WEB_BASE="/api/v1/plugins/platform-discovery/web/",MC_APP_BASE="/apps/platform-discovery/";
const MC_MODES={personal:"",theater:"theater",platforms:"platforms",random:"random"};
globalThis.MovieClawNavigation={
  isNative(location=globalThis.location){return !!location&&(location.pathname===MC_WEB_BASE.slice(0,-1)||location.pathname.startsWith(MC_WEB_BASE));},
  isEmbedded(){try{return this.isNative()&&!!globalThis.window&&globalThis.window.top!==globalThis.window;}catch{return false;}},
  mode(location=globalThis.location){
    if(this.isNative(location)){
      const sub=location.pathname.slice(MC_WEB_BASE.length).replace(/\/$/,"");
      return ({theater:"theater",platforms:"platforms",random:"random"})[sub]||"personal";
    }
    const view=new URLSearchParams(location?.search||"").get("view");return Object.hasOwn(MC_MODES,view)?view:"personal";
  },
  modeUrl(mode,location=globalThis.location){
    mode=Object.hasOwn(MC_MODES,mode)?mode:"personal";
    if(this.isNative(location))return MC_WEB_BASE+MC_MODES[mode];
    return "/api/v1/plugins/platform-discovery/panel?view="+mode;
  },
  updateMode(mode){const target=this.modeUrl(mode);if(target!==globalThis.location.pathname+globalThis.location.search)globalThis.history?.pushState(null,"",target);},
  hostNavigate(path){
    if(typeof path!=="string"||!path.startsWith("/")||path.startsWith("//")||/[\\\r\n]/.test(path))throw new Error("主程序地址无效。");
    try{(globalThis.window?.top?.location||globalThis.location).assign(path);}catch{globalThis.location.assign(path);}
  },
  entry(base,{native=false,mode="personal"}={}){
    const url=new URL(base);
    if(!["http:","https:"].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=="/"||/[\s\\]/.test(base))throw new Error("请输入完整的服务器地址，如 http://192.168.1.100:3000；不要包含账号、路径或参数。");
    return url.origin+(native?MC_APP_BASE+(MC_MODES[mode]||""):"/api/v1/plugins/platform-discovery/panel?view="+(Object.hasOwn(MC_MODES,mode)?mode:"personal"));
  }
};
(()=>{
  if(globalThis.MovieClawNavigation.isEmbedded())document.documentElement?.classList?.add("native-page");
  const $=id=>document.getElementById(id),open=$("entry-links-open");if(!open)return;
  const dialog=$("entry-links-dialog"),key="movieclaw.platform-discovery.entry-addresses";
  function link(container,label,base){const url=globalThis.MovieClawNavigation.entry(base,{native:globalThis.MovieClawNavigation.isNative()}),a=document.createElement("a"),note=document.createElement("p");a.className="button";a.textContent=label+" ↗";a.href=url;a.target="_top";note.className="entry-url";note.textContent=url;container.append(a,note);}
  function render(){const current=$("entry-current"),generated=$("entry-generated");current.replaceChildren();generated.replaceChildren();link(current,"打开当前地址的海报墙",location.origin);for(const [id,label] of [["entry-lan","局域网入口"],["entry-wan","公网入口"]])if($(id).value.trim())link(generated,label,$(id).value.trim());}
  open.addEventListener("click",()=>{try{const values=JSON.parse(localStorage.getItem(key)||"{}");$("entry-lan").value=values.lan||"";$("entry-wan").value=values.wan||"";render();}catch{$("entry-lan").value="";$("entry-wan").value="";render();}dialog.showModal();});
  $("entry-links-close").addEventListener("click",()=>dialog.close());
  $("entry-links-form").addEventListener("submit",event=>{event.preventDefault();try{render();try{localStorage.setItem(key,JSON.stringify({lan:$("entry-lan").value.trim(),wan:$("entry-wan").value.trim()}));}catch{}$("entry-links-error").textContent="入口已生成。";}catch(error){$("entry-links-error").textContent=error.message;}});
})();
