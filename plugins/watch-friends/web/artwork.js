"use strict";
// 图片选择、画幅预览和网格共用一处；只接收 TMDB 文件名，不接收外部图床地址。
window.MovieFriendsArtwork=(()=>{
  let bridge,serial=0;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const valid=p=>typeof p==="string"&&/^\/[A-Za-z0-9_-]{1,200}\.(jpg|jpeg|png|webp)$/i.test(p);
  const url=(option,large=false)=>`/api/v1/images/proxy?url=${encodeURIComponent(`https://image.tmdb.org/t/p/${large?"original":option.type==="poster"?"w500":"w780"}${option.path}`)}&w=${large?1280:500}`;
  function button(text,fn){const b=el("button","button button-quiet",text);b.type="button";b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){bridge.toast(bridge.safeError(e),true);}finally{b.disabled=false;}};return b;}
  function selections(item){const values=Array.isArray(item?.cover_images)?item.cover_images:item?.cover_path?[{path:item.cover_path,type:item.cover_type}]:[];const seen=new Set();return values.filter(v=>v&&valid(v.path)&&["poster","backdrop"].includes(v.type)&&!seen.has(v.path)&&seen.add(v.path)).slice(0,9);}
  function lightbox(images,index=0){
    const d=el("dialog","artwork-lightbox"),head=el("div","dialog-heading"),body=el("div","artwork-lightbox-content"),picture=el("img"),caption=el("p","muted"),controls=el("div","watch-row-actions");
    head.append(el("h2","","图片预览"),button("关闭",()=>d.close()));picture.alt="完整图片预览";picture.referrerPolicy="same-origin";
    function draw(){picture.src=url(images[index],true);caption.textContent=`${index+1} / ${images.length}`;}
    controls.append(button("上一张",()=>{index=(index+images.length-1)%images.length;draw();}),caption,button("下一张",()=>{index=(index+1)%images.length;draw();}));
    picture.onerror=()=>{caption.textContent="图片暂时不可用，可以切换其他图片";};body.append(picture);if(images.length>1)body.append(controls);d.append(head,body);d.onclose=()=>d.remove();document.body.append(d);draw();window.MovieFriendsUI.open(d);
  }
  function gallery(images,{editable=false,onChange=()=>{}}={}){
    const root=el("div","artwork-gallery"),count=Math.min(9,images.length);root.dataset.count=String(count);root.dataset.columns=String(count===4?2:Math.min(3,count));
    for(const [index,option] of images.slice(0,9).entries()){
      const tile=el("div","artwork-tile"),open=button("",()=>lightbox(images,index)),image=el("img");open.className="artwork-open";open.setAttribute("aria-label",`预览第 ${index+1} 张图片`);image.src=url(option,true);image.alt=`第 ${index+1} 张${option.type==="poster"?"海报":"剧照"}`;image.loading="lazy";image.decoding="async";image.referrerPolicy="same-origin";image.onerror=()=>{tile.classList.add("is-unavailable");open.setAttribute("aria-label","图片暂不可用");};open.append(image);tile.append(open);
      if(editable){const actions=el("div","artwork-tile-actions"),up=button("前移",()=>{if(!index)return;const copy=[...images];[copy[index-1],copy[index]]=[copy[index],copy[index-1]];onChange(copy);});up.disabled=index===0;up.setAttribute("aria-label",`前移第 ${index+1} 张图片`);actions.append(el("small","",`${index+1}`),up,button("移除",()=>onChange(images.filter((_,i)=>i!==index))));tile.append(actions);}root.append(tile);
    }return root;
  }
  function picker(root,item,{max=9,selected=[],onChange=()=>{},title="选择推荐图片"}={}){
    const session=bridge.sessionVersion(),id=++serial,active=()=>bridge.user()&&bridge.sessionVersion()===session&&root.isConnected;let chosen=selected.slice(0,max),options=[],type="backdrop",shown=36,dead=false;
    const alive=()=>!dead&&active();root.hidden=false;root.classList.add("artwork-picker");
    const head=el("div","feed-cover-heading"),info=el("div"),status=el("p","field-help","正在读取图片…"),count=el("span","small-label"),preview=el("div","artwork-selection-preview"),tabs=el("div","feed-cover-tabs"),choices=el("div","feed-cover-choices"),more=el("div","watch-row-actions"),tools=el("div","watch-row-actions");info.append(el("h3","",title),status);head.append(info,count);root.replaceChildren(head,preview,tabs,tools,choices,more);
    function update(values){const focus=document.activeElement?.value;chosen=values;onChange(chosen);renderPreview();draw();if(focus)for(const input of choices.querySelectorAll("input"))if(input.value===focus){input.focus();break;}}
    function renderPreview(){preview.replaceChildren();count.textContent=`${chosen.length} / ${max}`;if(chosen.length)preview.append(gallery(chosen,{editable:true,onChange:update}));else preview.append(el("p","muted","使用默认封面；选择图片后可调整顺序，点击图片查看完整大图。"));}
    function add(values){const seen=new Set(options.map(o=>o.path));for(const option of values)if(valid(option?.path)&&["poster","backdrop"].includes(option.type)&&!seen.has(option.path)){seen.add(option.path);options.push(option);}}
    function draw(){
      tabs.replaceChildren();choices.replaceChildren();more.replaceChildren();
      for(const [value,label] of [["backdrop","横版剧照"],["poster","竖版海报"]]){const n=options.filter(o=>o.type===value).length;if(n){const b=button(`${label} · ${n}`,()=>{type=value;shown=36;draw();});b.setAttribute("aria-pressed",String(type===value));tabs.append(b);}}
      const available=options.filter(o=>o.type===type);
      for(const [index,option] of available.slice(0,shown).entries()){
        const label=el("label",`feed-cover-choice${type==="poster"?" is-portrait":""}`),input=el("input"),image=el("img"),caption=el("span","",`${type==="poster"?"竖版海报":"横版剧照"} ${index+1}`);input.type=max===1?"radio":"checkbox";input.name=`artwork-${id}`;input.value=option.path;input.checked=chosen.some(v=>v.path===option.path);input.setAttribute("aria-label",caption.textContent);
        input.onchange=()=>{if(!alive())return;if(input.checked&&chosen.length>=max&&max>1){input.checked=false;bridge.toast(`最多选择 ${max} 张图片`,true);return;}update(input.checked?(max===1?[option]:[...chosen,option]):chosen.filter(v=>v.path!==option.path));};
        image.src=url(option);image.alt="";image.loading="lazy";image.referrerPolicy="same-origin";image.onerror=()=>{if(!alive())return;label.classList.add("is-unavailable");input.disabled=true;caption.textContent="图片暂不可用";};label.classList.toggle("is-selected",input.checked);label.append(input,image,caption);choices.append(label);
      }
      if(available.length>shown)more.append(button(`显示更多图片（剩余 ${available.length-shown} 张）`,()=>{shown+=36;draw();}));
    }
    tools.append(button("使用默认封面",()=>update([])));
    async function load(full=false){
      status.textContent=full?"正在读取主程序完整候选图库…":"正在读取备选图片…";
      try{const result=full?await bridge.fullArtwork(item):await bridge.titleDetails(item);if(!alive())return;
        add(result?.images||[]);add([{path:result?.backdrop_path,type:"backdrop"},{path:result?.poster_path||item.poster_path,type:"poster"}]);if(!options.some(o=>o.type===type))type="poster";status.textContent=result?.notice||(options.length?"图片完整显示，点击已选图片可预览大图；选择顺序即发布顺序。":"暂无备选图片，可保留默认封面。");draw();
      }catch(e){if(alive()){status.textContent=full?bridge.safeError(e):"备选图片暂时无法读取，仍可使用默认封面。";more.replaceChildren(button("重新读取图片",()=>load(full)));}}
    }
    if(bridge.user()?.role==="admin")tools.append(button("读取完整候选图库",()=>load(true)));
    renderPreview();load();return {destroy(){dead=true;},get(){return chosen;}};
  }
  return {configure(value){bridge=value;},picker,gallery,selections,lightbox,url};
})();
