"use strict";
globalThis.MovieClawNavigation={
  entry(base){
    const url=new URL(base);
    if(!["http:","https:"].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=="/"||/[\s\\]/.test(base))throw new Error("请输入完整的服务器地址，如 http://192.168.1.100:3000；不要包含账号、路径或参数。");
    return url.origin+"/api/v1/plugins/platform-discovery/panel?view=personal";
  }
};
(()=>{
  const $=id=>document.getElementById(id),open=$("entry-links-open");if(!open)return;
  const dialog=$("entry-links-dialog"),key="movieclaw.platform-discovery.entry-addresses";
  function link(container,label,base){const url=globalThis.MovieClawNavigation.entry(base),a=document.createElement("a"),note=document.createElement("p");a.className="button";a.textContent=label+" ↗";a.href=url;a.target="_top";note.className="entry-url";note.textContent=url;container.append(a,note);}
  function render(){const current=$("entry-current"),generated=$("entry-generated");current.replaceChildren();generated.replaceChildren();link(current,"打开当前地址的海报墙",location.origin);for(const [id,label] of [["entry-lan","局域网入口"],["entry-wan","公网入口"]])if($(id).value.trim())link(generated,label,$(id).value.trim());}
  open.addEventListener("click",()=>{try{const values=JSON.parse(localStorage.getItem(key)||"{}");$("entry-lan").value=values.lan||"";$("entry-wan").value=values.wan||"";render();}catch{$("entry-lan").value="";$("entry-wan").value="";render();}dialog.showModal();});
  $("entry-links-close").addEventListener("click",()=>dialog.close());
  $("entry-links-form").addEventListener("submit",event=>{event.preventDefault();try{render();try{localStorage.setItem(key,JSON.stringify({lan:$("entry-lan").value.trim(),wan:$("entry-wan").value.trim()}));}catch{}$("entry-links-error").textContent="入口已生成。";}catch(error){$("entry-links-error").textContent=error.message;}});
})();
