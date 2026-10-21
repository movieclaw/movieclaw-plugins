"use strict";
// 说明默认收起；同一时间只展开一项，支持悬停、键盘和触屏，不使用 HTML 注入。
window.MovieFriendsHelp = (() => {
  let nextId=0,current=null;
  function create(label,text){
    const root=document.createElement("span"),trigger=document.createElement("button"),tip=document.createElement("span");
    root.className="info-help";trigger.type="button";trigger.className="info-help-trigger";
    trigger.setAttribute("aria-label",`${label}说明`);trigger.setAttribute("aria-expanded","false");
    trigger.append(window.MovieFriendsIcons.create("info"));
    tip.id=`info-help-${++nextId}`;tip.className="info-help-popover";tip.setAttribute("role","tooltip");tip.textContent=text;tip.hidden=true;
    trigger.setAttribute("aria-describedby",tip.id);root.append(trigger);let pinned=false,leaveTimer;
    function position(){if(!root.isConnected){close();return;}const rect=trigger.getBoundingClientRect(),box=tip.getBoundingClientRect();tip.style.left=`${Math.max(12,Math.min(rect.left,innerWidth-box.width-12))}px`;tip.style.top=`${rect.bottom+8+box.height>innerHeight-12?Math.max(12,rect.top-box.height-8):rect.bottom+8}px`;}
    function close(){clearTimeout(leaveTimer);pinned=false;tip.hidden=true;tip.remove();trigger.setAttribute("aria-expanded","false");if(current?.root===root)current=null;}
    function open(){if(current?.root!==root)current?.close();clearTimeout(leaveTimer);(root.closest("dialog")||document.body).append(tip);tip.hidden=false;trigger.setAttribute("aria-expanded","true");current={root,tip,close,position};position();}
    function leave(){if(!pinned&&document.activeElement!==trigger)leaveTimer=setTimeout(close,180);}
    root.addEventListener("pointerenter",event=>{clearTimeout(leaveTimer);if(event.pointerType!=="touch")open();});
    root.addEventListener("pointerleave",leave);tip.addEventListener("pointerenter",()=>clearTimeout(leaveTimer));tip.addEventListener("pointerleave",leave);
    trigger.addEventListener("focus",open);trigger.addEventListener("blur",event=>{if(!pinned||event.relatedTarget)close();});
    trigger.addEventListener("click",()=>{pinned=!pinned;if(pinned)open();else close();});
    trigger.addEventListener("keydown",event=>{if(event.key==="Escape"){event.preventDefault();close();}});
    return root;
  }
  function enhance(root=document){for(const item of root.querySelectorAll("[data-info]"))item.replaceWith(create(item.dataset.infoLabel||"更多信息",item.dataset.info));}
  document.addEventListener("pointerdown",event=>{if(current&&!current.root.contains(event.target)&&!current.tip.contains(event.target))current.close();});
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&current){event.preventDefault();current.close();}});
  document.addEventListener("scroll",()=>current?.position(),true);window.addEventListener("resize",()=>current?.position());
  document.addEventListener("DOMContentLoaded",()=>enhance());
  return {create,enhance,close:()=>current?.close()};
})();
