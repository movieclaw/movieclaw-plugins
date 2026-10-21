"use strict";
// Lucide 原图固定为此提交，全部随插件离线打包；许可证见 lucide-LICENSE.txt。
// https://github.com/lucide-icons/lucide/tree/c29c276f16590cf017126671535ad8c792be64d8/icons
window.MovieFriendsIcons = (() => {
  const nodes = {"check":[["path",{"d":"m20 6-11 11-5-5"}]],"ellipsis":[["circle",{"cx":"12","cy":"12","r":"1"}],["circle",{"cx":"19","cy":"12","r":"1"}],["circle",{"cx":"5","cy":"12","r":"1"}]],"list-video":[["path",{"d":"M21 5H3"}],["path",{"d":"M10 12H3"}],["path",{"d":"M10 19H3"}],["path",{"d":"M15 12.003a1 1 0 0 1 1.517-.859l4.997 2.997a1 1 0 0 1 0 1.718l-4.997 2.997a1 1 0 0 1-1.517-.86z"}]],"film":[["rect",{"width":"18","height":"18","x":"3","y":"3","rx":"2"}],["path",{"d":"M7 3v18"}],["path",{"d":"M3 7.5h4"}],["path",{"d":"M3 12h18"}],["path",{"d":"M3 16.5h4"}],["path",{"d":"M17 3v18"}],["path",{"d":"M17 7.5h4"}],["path",{"d":"M17 16.5h4"}]],"heart":[["path",{"d":"M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"}]],"users-round":[["path",{"d":"M18 21a8 8 0 0 0-16 0"}],["circle",{"cx":"10","cy":"8","r":"5"}],["path",{"d":"M22 20c0-3.37-2-6.5-4-8a5 5 0 0 0-.45-8.3"}]],"compass":[["circle",{"cx":"12","cy":"12","r":"10"}],["path",{"d":"m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"}]],"clapperboard":[["path",{"d":"m12.296 3.464 3.02 3.956"}],["path",{"d":"M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3z"}],["path",{"d":"M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"}],["path",{"d":"m6.18 5.276 3.1 3.899"}]],"image":[["rect",{"width":"18","height":"18","x":"3","y":"3","rx":"2","ry":"2"}],["circle",{"cx":"9","cy":"9","r":"2"}],["path",{"d":"m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"}]],"clock":[["circle",{"cx":"12","cy":"12","r":"10"}],["path",{"d":"M12 6v6l4 2"}]],"circle-alert":[["circle",{"cx":"12","cy":"12","r":"10"}],["line",{"x1":"12","x2":"12","y1":"8","y2":"12"}],["line",{"x1":"12","x2":"12.01","y1":"16","y2":"16"}]],"refresh-cw":[["path",{"d":"M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"}],["path",{"d":"M21 3v5h-5"}],["path",{"d":"M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"}],["path",{"d":"M8 16H3v5"}]],"x":[["path",{"d":"M18 6 6 18"}],["path",{"d":"m6 6 12 12"}]],"arrow-up-right":[["path",{"d":"M7 7h10v10"}],["path",{"d":"M7 17 17 7"}]],"plus":[["path",{"d":"M5 12h14"}],["path",{"d":"M12 5v14"}]],"star":[["path",{"d":"M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"}]],"bookmark":[["path",{"d":"M17 3a2 2 0 0 1 2 2v15a1 1 0 0 1-1.496.868l-4.512-2.578a2 2 0 0 0-1.984 0l-4.512 2.578A1 1 0 0 1 5 20V5a2 2 0 0 1 2-2z"}]],"bell":[["path",{"d":"M10.268 21a2 2 0 0 0 3.464 0"}],["path",{"d":"M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"}]],"info":[["circle",{"cx":"12","cy":"12","r":"10"}],["path",{"d":"M12 16v-4"}],["path",{"d":"M12 8h.01"}]],"message-circle":[["path",{"d":"M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719"}]],"chevron-left":[["path",{"d":"m15 18-6-6 6-6"}]],"chevron-right":[["path",{"d":"m9 18 6-6-6-6"}]]};
  const ns = "http://www.w3.org/2000/svg";
  function create(name, extraClass="") {
    const svg=document.createElementNS(ns,"svg");
    for(const [key,value] of Object.entries({viewBox:"0 0 24 24",width:"24",height:"24",fill:"none",stroke:"currentColor","stroke-width":"1.7","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true",focusable:"false"}))svg.setAttribute(key,value);
    svg.setAttribute("class",`ui-icon ${extraClass}`.trim());svg.dataset.icon=name;
    for(const [tag,attrs] of nodes[name]||nodes.film){const shape=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))shape.setAttribute(key,value);svg.append(shape);}
    return svg;
  }
  // 只用于静态操作文案；电影名、昵称和用户正文仍由原组件直接作为文本渲染。
  function label(element,value){
    let text=String(value),icon=null,filled=false,trailing=false;
    for(const [prefix,name] of [["♡ ","heart"],["♥ ","heart"],["＋ ","plus"]])if(text.startsWith(prefix)){icon=name;filled=prefix==="♥ ";text=text.slice(prefix.length);break;}
    if(icon==="heart" && text==="收藏影片")icon="bookmark";
    if(text.endsWith(" ↗")){icon="arrow-up-right";trailing=true;text=text.slice(0,-2);}
    if(text==="关闭 ✕"){icon="x";trailing=true;text="关闭";}
    element.textContent=text;element.classList.toggle("has-icon",Boolean(icon));
    if(icon){const shape=create(icon,filled?"is-filled":"");if(trailing)element.append(shape);else element.prepend(shape);}
    return element;
  }
  function enhance(root=document){
    for(const item of root.querySelectorAll("[data-ui-icon]"))item.replaceWith(create(item.dataset.uiIcon,item.className));
    for(const item of root.querySelectorAll("[data-icon-label]"))label(item,item.textContent);
  }
  document.addEventListener("DOMContentLoaded",()=>enhance());
  return {create,label,enhance};
})();
