"use strict";
(() => {
  const $=id=>document.getElementById(id), base='/api/v1/plugins/pt-site-pack';
  const el=(tag,value,cls)=>{const n=document.createElement(tag);if(value!=null)n.textContent=value;if(cls)n.className=cls;return n;};
  const button=(label,action,cls='button secondary')=>{const b=el('button',label,cls);b.type='button';b.addEventListener('click',action);return b;};
  const states={downloading:'下载中',stalled:'等待传输',queued:'排队中',paused:'已暂停',checking:'校验中',completed:'已完成',error:'任务异常',missing:'任务缺失',unknown:'状态未知'};
  let data=null, privateMode=true, hideSites=false, hideNames=false, loading=false, loadVersion=0, lastRead=0;
  let taskScope=null, taskPage=1, taskData=null, taskVersion=0, taskTimer=null;
  let shareScope=null, shareTasks=false, imagePage=0, imagePages=1, shareVersion=0;
  let shareData=null, shareRows=null, shareStamp='', shareBusy=false;
  const shareIcons=new Map();
  function loadShareIcon(path){
    if(!path||!/^\/api\/v1\/plugins\/pt-site-pack\/icons\/[a-z0-9_-]+$/.test(path))return Promise.resolve(null);
    if(shareIcons.has(path))return shareIcons.get(path);
    const task=new Promise(resolve=>{const img=new Image();let done=false;const finish=value=>{if(done)return;done=true;clearTimeout(timer);img.onload=null;img.onerror=null;resolve(value);};const timer=setTimeout(()=>finish(null),3500);img.referrerPolicy='no-referrer';img.onload=()=>finish(img.naturalWidth&&img.naturalHeight?img:null);img.onerror=()=>finish(null);img.src=path;});shareIcons.set(path,task);return task;
  }
  const resolvedIcons=new Map();
  const date=value=>{if(!value)return '暂无快照';const d=new Date(value);return Number.isNaN(d.valueOf())?'未知时间':d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});};
  const bytes=(n,speed=false)=>{if(n==null||!Number.isFinite(n))return '—';const units=['B','KiB','MiB','GiB','TiB','PiB'];let i=0;while(n>=1024&&i<5){n/=1024;i++;}return n.toLocaleString('zh-CN',{maximumFractionDigits:i?2:0})+' '+units[i]+(speed?'/s':'');};
  const count=n=>n==null?'—':n.toLocaleString('zh-CN');
  const ratio=n=>n==null?'—':n.toLocaleString('zh-CN',{maximumFractionDigits:2});
  const metric=(name,value,note)=>{const a=el('article');a.append(el('span',name),el('strong',value),el('small',note));return a;};
  function privacyQuery(p=privateMode,hs=hideSites,hn=hideNames){return new URLSearchParams({privacy:String(p),hide_sites:String(hs),hide_names:String(hn)});}
  async function api(path,params){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);try{
    const response=await fetch(base+path+'?'+params,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    const body=await response.json();if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:body.message||'读取失败，请刷新重试');return body.data||body;
  }finally{clearTimeout(timeout);}}
  function status(message,error=false){$('ov-status').textContent=message;$('ov-status').className='notice'+(error?' error':'');}
  function visibleSites(){if(!data)return [];const q=$('ov-search').value.trim().toLowerCase(),source=$('ov-source').value,sort=$('ov-sort').value;
    const rows=data.sites.filter(s=>(source==='all'||s.source===source)&&(!q||s.name.toLowerCase().includes(q)));
    if(sort!=='name') rows.sort((a,b)=>{const value=s=>sort==='speed'?(s.live.upspeed_bytes||0)+(s.live.dlspeed_bytes||0):s.profile[sort]??-1;return value(b)-value(a);});return rows;
  }
  function siteCard(site){const root=el('article',null,'overview-card'),head=el('div',null,'overview-card-head'),title=el('div');
    const logo=el('div',site.name.slice(0,1),'site-logo');
    if(site.icon_url){const img=el('img');img.alt='';img.src=site.icon_url;img.referrerPolicy='no-referrer';img.addEventListener('error',()=>img.remove());img.addEventListener('load',()=>logo.firstChild?.nodeType===3&&logo.firstChild.remove());logo.append(img);}
    title.append(el('h3',site.name),el('p',privateMode?'账号已隐藏':site.profile.username||'尚无账号资料','account-name'));
    head.append(logo,title,el('span',site.source_label,'badge'));root.append(head);
    const flow=el('div',null,'overview-flow');flow.append(metric('累计上传',bytes(site.profile.uploaded_bytes),'站点记录'),metric('累计下载',bytes(site.profile.downloaded_bytes),'站点记录'));root.append(flow);
    const facts=el('dl',null,'overview-facts');
    for(const [label,value]of [[site.profile.field_sources?.ratio==='calculated'?'分享率 · 计算值':'分享率',ratio(site.profile.ratio)],['魔力 / 积分',count(site.profile.bonus)],['站点做种',count(site.profile.seeding_count)],['站点下载',count(site.profile.leeching_count)]]){const wrap=el('div');wrap.append(el('dt',label),el('dd',value));facts.append(wrap);}root.append(facts);
    const live=el('div',null,'overview-live');live.append(el('span','↑ '+bytes(site.live.upspeed_bytes,true),'upload-value'),el('span','↓ '+bytes(site.live.dlspeed_bytes,true),'download-value'),el('small','已关联 '+count(site.live.task_count)+' 项'));root.append(live);
    const actions=el('div',null,'overview-card-actions');actions.append(button('上传 '+count(site.live.uploading),()=>openTasks(site.key,'uploading')),button('下载 '+count(site.live.downloading),()=>openTasks(site.key,'downloading')),button('查看任务',()=>openTasks(site.key)),button('分享',()=>openShare(site.key,false)));
    if(site.configured){const collect=button(site.profile.incomplete?'补齐资料':'更新资料',async()=>{collect.disabled=true;try{const response=await fetch(base+'/overview/collect?'+new URLSearchParams({site_key:site.key}),{method:'POST',credentials:'same-origin'});const result=await response.json();if(!response.ok)throw new Error(result.detail||'采集失败');status((result.data||result).message);setTimeout(()=>refresh(true),10000);}catch(error){status(error.message,true);}finally{setTimeout(()=>{collect.disabled=false;},60000);}});collect.title='访问此站点验证账号并采集资料；不会修改账号配置';actions.append(collect);}
    actions.append(button('统计检查 '+(site.collection?.profile_collected??0)+'/6',()=>window.PTStatistics?.inspect(site,{hideSites,privacy:privateMode,stale:data?.profile_snapshot_stale})));
    root.append(actions,el('p',(site.profile.stale?'资料已过期 · ':'')+(site.profile.incomplete?'部分字段待采集 · ':'')+date(site.profile.fetched_at),'site-note'));
    if(site.profile.age_days!=null)root.append(el('p','本站注册约 '+(site.profile.age_days/365.2425).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 年 · 来源：站点注册记录','site-note'));
    if(!site.usable)root.append(el('p',site.verification_label,'badge '+(site.verification==='failed'?'failed':'pending')));return root;
  }
  function render(){if(!data)return;const s=data.summary;
    window.PTStatistics?.update(data);
    window.PTInsights?.render(data,visibleSites(),{hideSites});
    $('ov-summary').replaceChildren(metric('累计上传 · 已采集',bytes(s.uploaded_bytes),s.profile_coverage+' / '+s.configured+' 个账号有流量资料'),metric('累计下载 · 已采集',bytes(s.downloaded_bytes),'站点计费量，含站点促销规则'),metric('当前上传',bytes(s.upspeed_bytes,true),'正在上传 '+count(s.uploading)+' 项'),metric('当前下载',bytes(s.dlspeed_bytes,true),'下载中 '+count(s.downloading)+' 项'));
    $('ov-grid').replaceChildren(...visibleSites().map(siteCard));if(!visibleSites().length)$('ov-grid').append(el('p','没有匹配的站点，请先在“我的站点”添加站点。','site-note'));
    $('ov-coverage').textContent='已添加 '+s.site_count+' 站 · 已连接 '+s.connected_downloaders+' / '+s.downloader_count+' 台下载器 · '+s.stale_sites+' 站资料已过期 · 更新于 '+date(data.updated_at);
    const unassigned=$('ov-unassigned');unassigned.replaceChildren();
    if(s.unknown_tasks){const note=el('div');note.append(el('strong','未归属任务 '+s.unknown_tasks+' 项'),el('p','这些任务没有可信的站点来源，单独统计；已计入顶部实时速度。','site-note'));unassigned.append(note,button('查看未归属',()=>openTasks('_unassigned')));}
    status(data.warning||'自动读取缓存和下载器快照；刷新不访问 PT 站，不重新登录。',Boolean(data.warning));
  }
  async function refresh(force=false){if($('overview-pane').hidden&&!force)return;if(loading&&!force)return;if(!force&&Date.now()-lastRead<15000)return;
    const version=++loadVersion;loading=true;
    try{const result=await api('/overview',privacyQuery());if(version!==loadVersion)return;data=result;lastRead=Date.now();render();}
    catch(error){if(version===loadVersion)status((error.name==='AbortError'?'读取超时':error.message)+(data?'；保留上次快照。':''),true);}
    finally{if(version===loadVersion)loading=false;}
  }
  async function changePrivacy(){privateMode=!privateMode;await privacyChanged();}
  async function privacyChanged(){
    window.PTStatistics?.closeForPrivacy();
    window.PTInsights?.clear();
    ++loadVersion;++taskVersion;loading=false;lastRead=0;data=null;taskData=null;
    $('ov-grid').replaceChildren();$('ov-summary').replaceChildren();$('ov-task-list').replaceChildren();$('ov-unassigned').replaceChildren();
    $('ov-privacy').textContent='隐私模式 · '+(privateMode?'已开启':'已关闭');$('ov-privacy').setAttribute('aria-pressed',String(privateMode));
    $('ov-task-privacy').textContent=privateMode?'隐私模式 · 已开启':'隐私模式 · 已关闭';
    if($('ov-share-dialog').open)$('ov-share-dialog').close();status('正在更新隐私显示…');await refresh(true);if($('ov-task-dialog').open)await loadTasks();
  }
  function scopeName(scope){return scope==='_unassigned'?'未归属任务':scope?data?.sites.find(s=>s.key===scope)?.name||'站点任务':'全部任务';}
  async function openTasks(scope=null,state='all'){
    taskScope=scope;taskPage=1;$('ov-task-search').value='';$('ov-task-state').value=state;$('ov-task-list').replaceChildren();taskData=null;
    $('ov-task-title').textContent=scopeName(scope);$('ov-task-privacy').textContent=privateMode?'隐私模式 · 已开启':'隐私模式 · 已关闭';
    if(!$('ov-task-dialog').open)$('ov-task-dialog').showModal();await loadTasks();
    if(!taskTimer)taskTimer=setInterval(()=>{if($('ov-task-dialog').open&&!document.hidden)loadTasks();},15000);
  }
  function taskParams(p=privateMode,hs=hideSites,hn=hideNames){const params=privacyQuery(p,hs,hn);if(taskScope)params.set('site_id',taskScope);params.set('page',String(taskPage));params.set('page_size','20');params.set('state',$('ov-task-state').value);params.set('q',$('ov-task-search').value.trim());return params;}
  function taskRow(t){const row=el('article',null,'task-row'),title=el('div',null,'task-name');title.append(el('strong',t.name),el('small',(privateMode?'下载器名称已隐藏':t.downloader_name||'下载器')+' · '+(states[t.state]||t.state)));
    const progress=el('div',null,'task-progress');const bar=el('progress');bar.max=1;if(t.progress!=null)bar.value=t.progress;bar.setAttribute('aria-label','任务进度');progress.append(el('span',t.progress==null?'进度未知':(t.progress*100).toFixed(1)+'%'),bar);
    const size=el('span',bytes(t.size_bytes),'task-size'),speed=el('div',null,'task-speed');speed.append(el('span','↑ '+bytes(t.upspeed_bytes,true),'upload-value'),el('span','↓ '+bytes(t.dlspeed_bytes,true),'download-value'));
    const hr=el('span',t.hit_and_run===true?'H&R 有考核':t.hit_and_run===false?'H&R 无考核':'H&R 未知','badge '+(t.hit_and_run===true?'pending':''));hr.title=t.hr_observed_at?'站点观测：'+date(t.hr_observed_at)+'；不代表个人考核完成':t.hr_reason||'尚无可靠的站点考核证据';
    const hrBox=el('div',null,'task-hr');hrBox.append(hr);
    if(t.can_check_hr){const check=button('核对 H&R',async()=>{check.disabled=true;const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),65000);try{const response=await fetch(base+'/overview/hr-check?'+new URLSearchParams({task_key:t.key}),{method:'POST',credentials:'same-origin',signal:controller.signal});const result=await response.json();if(!response.ok)throw new Error(result.detail||'核对失败');await loadTasks();$('ov-task-status').textContent=(result.data||result).message;}catch(error){$('ov-task-status').textContent=error.name==='AbortError'?'核对超时，原有证据保留':error.message;}finally{clearTimeout(timeout);check.disabled=false;}});hrBox.append(check);}
    else if(t.hit_and_run==null)hrBox.append(el('small',t.hr_reason||'缺少可核对的来源','site-note'));
    row.append(title,size,progress,speed,hrBox);return row;
  }
  async function loadTasks(){const version=++taskVersion;$('ov-task-status').textContent='正在读取任务快照…';
    try{const result=await api('/overview/tasks',taskParams());if(version!==taskVersion||!$('ov-task-dialog').open)return;taskData=result;taskPage=result.page;
      $('ov-task-title').textContent=scopeName(taskScope);$('ov-task-list').replaceChildren(...result.items.map(taskRow));
      if(!result.items.length)$('ov-task-list').append(el('p','当前筛选下没有下载器任务。站点个人做种总数可能包含未连接的设备。','site-note'));
      $('ov-task-status').textContent=(result.warning?result.warning+' · ':'')+result.total+' 项 · '+date(result.updated_at);$('ov-page').textContent=result.page+' / '+result.pages+' 页';$('ov-prev').disabled=result.page<=1;$('ov-next').disabled=result.page>=result.pages;
    }catch(error){if(version===taskVersion)$('ov-task-status').textContent=error.name==='AbortError'?'读取超时，请稍后重试':error.message;}
  }
  function openShare(scope=null,tasks=false){shareScope=scope;shareTasks=tasks;imagePage=0;shareData=null;shareRows=null;
    $('ov-share-mode').value='private';$('ov-share-hide-sites').checked=hideSites;$('ov-share-hide-names').checked=true;$('ov-share-hide-names').disabled=false;
    $('ov-share-title').textContent=tasks?'分享本页任务':scope?'分享单站统计':'分享当前筛选的站点';$('ov-share-canvas').width=1;$('ov-share-canvas').height=1;
    if(!$('ov-share-dialog').open)$('ov-share-dialog').showModal();buildShare();
  }
  async function buildShare(){const version=++shareVersion;shareBusy=true;shareRows=null;shareData=null;$('ov-share-canvas').width=1;$('ov-share-canvas').height=1;$('ov-save-image').disabled=true;$('ov-share-status').textContent='正在生成预览…';
    const privacy=$('ov-share-mode').value==='private',hs=$('ov-share-hide-sites').checked,hn=privacy&&$('ov-share-hide-names').checked;
    try{const nextData=await api('/overview',privacyQuery(privacy,hs,hn));if(version!==shareVersion)return;shareData=nextData;
      if(shareTasks){const result=await api('/overview/tasks',taskParams(privacy,hs,hn));if(version!==shareVersion)return;shareRows=result.items;shareStamp=result.updated_at;}
      else{const keys=new Set(shareScope?[shareScope]:visibleSites().map(s=>s.key));shareRows=shareData.sites.filter(s=>keys.has(s.key));shareStamp=shareData.updated_at;}
      imagePages=Math.max(1,Math.ceil(shareRows.length/(shareTasks?20:24)));imagePage=Math.min(imagePage,imagePages-1);
      const iconSites=shareTasks?shareData.sites:shareRows;
      await Promise.all([...new Set(iconSites.map(s=>s.icon_url).filter(Boolean))].map(async path=>{const img=await loadShareIcon(path);if(img)resolvedIcons.set(path,img);}));
      if(version!==shareVersion)return;paintShare(privacy);
      $('ov-share-status').textContent=(privacy?'隐私分享':'完整展示 · 包含可见账号与任务资料')+' · '+(shareTasks?'仅导出当前任务页':'当前筛选 '+shareRows.length+' 个站点')+' · '+date(shareStamp);
    }catch(error){if(version===shareVersion){$('ov-share-status').textContent=error.message;$('ov-share-canvas').width=1;$('ov-share-canvas').height=1;}}
    finally{if(version===shareVersion){shareBusy=false;$('ov-save-image').disabled=!shareRows;}}
  }
  function paintShare(privacy=$('ov-share-mode').value==='private'){
    if(!shareRows)return;const rows=shareRows.slice(imagePage*(shareTasks?20:24),(imagePage+1)*(shareTasks?20:24));
    const canvas=$('ov-share-canvas');canvas.width=1200;canvas.height=shareTasks?310+Math.max(1,rows.length)*86:360+Math.max(1,Math.ceil(rows.length/3))*236;
    const c=canvas.getContext('2d');
    const theme={bg:document.documentElement.dataset.theme==='netflix'?'#080808':'#0e1419',surface:document.documentElement.dataset.theme==='netflix'?'#191919':'#19232a',text:'#f5f6f8',muted:'#a7b0b7',line:'#354049',up:'#79dfb5',down:'#88b7f3'};
    c.fillStyle=theme.bg;c.fillRect(0,0,canvas.width,canvas.height);
    function txt(s,x,y,size=18,color=theme.text,weight=400,max=1080){c.font=weight+' '+size+'px system-ui,"PingFang SC",sans-serif';c.fillStyle=color;let value=String(s);while(c.measureText(value).width>max&&value.length>1)value=value.slice(0,-2)+'…';c.fillText(value,x,y);}
    function rect(x,y,w,h){c.fillStyle=theme.surface;c.beginPath();c.roundRect(x,y,w,h,14);c.fill();c.strokeStyle=theme.line;c.lineWidth=1;c.stroke();}
    function logo(site,x,y,size=36){c.save();c.beginPath();c.roundRect(x,y,size,size,8);c.clip();c.fillStyle=theme.line;c.fillRect(x,y,size,size);const img=site?.icon_url?resolvedIcons.get(site.icon_url):null;if(img){const scale=Math.min((size-6)/img.naturalWidth,(size-6)/img.naturalHeight),w=img.naturalWidth*scale,h=img.naturalHeight*scale;c.drawImage(img,x+(size-w)/2,y+(size-h)/2,w,h);}else{c.font='600 '+Math.round(size*.48)+'px system-ui';c.fillStyle=theme.text;c.textAlign='center';c.fillText((site?.name||'站').slice(0,1),x+size/2,y+size*.68);}c.restore();}
    txt('PT岛',48,54,19,theme.muted,500);txt(shareTasks?'我的传输任务':'我的站点 · 分享记录',48,103,34,theme.text,650);
    txt((privacy?'隐私分享':'完整展示')+'   /   '+date(shareStamp)+'   /   '+(imagePage+1)+' · '+imagePages,48,137,16,theme.muted);
    const sum=key=>{const values=shareRows.map(s=>s.profile[key]).filter(n=>n!=null);return values.length?values.reduce((a,b)=>a+b,0):null;};
    const liveSum=key=>{const values=shareRows.map(s=>s.live[key]).filter(n=>n!=null);return values.length?values.reduce((a,b)=>a+b,0):null;};
    const summary=shareTasks?shareData.summary:{uploaded_bytes:sum('uploaded_bytes'),downloaded_bytes:sum('downloaded_bytes'),upspeed_bytes:liveSum('upspeed_bytes'),dlspeed_bytes:liveSum('dlspeed_bytes')},metrics=shareTasks?[["本页任务",rows.length+' 项'],['有 H&R',rows.filter(t=>t.hit_and_run===true).length+' 项'],['H&R 未知',rows.filter(t=>t.hit_and_run==null).length+' 项']]:[["累计上传 · 已采集",bytes(summary.uploaded_bytes)],["累计下载 · 已采集",bytes(summary.downloaded_bytes)],["当前上传",bytes(summary.upspeed_bytes,true)],["当前下载",bytes(summary.dlspeed_bytes,true)]];
    const width=1104/metrics.length;metrics.forEach(([name,value],i)=>{txt(name,48+i*width,183,16,theme.muted);txt(value,48+i*width,221,27,i%2?theme.down:theme.up,600,width-16);});
    if(!rows.length)txt('当前筛选没有记录',48,290,20,theme.muted);
    if(shareTasks)rows.forEach((t,i)=>{const y=250+i*86,site=shareData.sites.find(s=>s.key===t.site_key);rect(48,y,1104,74);logo(site,66,y+19);txt(t.name,114,y+28,18,theme.text,550,665);txt(states[t.state]+' · '+bytes(t.size_bytes)+' · '+(t.progress==null?'—':(t.progress*100).toFixed(1)+'%'),114,y+54,15,theme.muted,400,665);txt('↑ '+bytes(t.upspeed_bytes,true)+'   ↓ '+bytes(t.dlspeed_bytes,true),800,y+28,16,theme.text,400,330);txt(t.hit_and_run===true?'H&R 有考核':t.hit_and_run===false?'H&R 无考核':'H&R 未知',800,y+54,15,t.hit_and_run===true?'#f5c451':theme.muted);});
    else {const fun=window.PTInsights?.summary(shareRows);if(fun)txt(fun.age+'   ·   上传里程碑：'+fun.milestone,48,255,16,theme.muted,500);
      rows.forEach((s,i)=>{const x=48+(i%3)*374,y=282+Math.floor(i/3)*236;rect(x,y,356,218);logo(s,x+18,y+17,38);txt(s.name,x+68,y+33,21,theme.text,650,270);txt(privacy?'账号已隐藏':s.profile.username||'暂无账号资料',x+68,y+55,14,theme.muted,400,270);
        txt('↑ '+bytes(s.profile.uploaded_bytes),x+18,y+91,22,theme.up,600,320);txt('↓ '+bytes(s.profile.downloaded_bytes),x+18,y+122,22,theme.down,600,320);txt('分享率'+(s.profile.field_sources?.ratio==='calculated'?'(计算)':'')+' '+ratio(s.profile.ratio)+'   做种 '+count(s.profile.seeding_count)+'   下载 '+count(s.profile.leeching_count),x+18,y+150,14,theme.muted,400,320);
        txt(s.profile.age_days==null?'注册时长待采集':s.profile.age_days<365?'本站注册不足 1 年':'本站注册约 '+(s.profile.age_days/365.2425).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 年',x+18,y+176,13,theme.muted,400,320);txt(s.profile.stale?'资料已过期':s.profile.incomplete?'部分字段待采集':'站点账号快照',x+18,y+199,13,theme.muted,400,320);});}
    txt(shareTasks?'仅为当前下载器快照；H&R 标记不代表考核完成。':'累计量仅汇总所选站点；速度为已关联任务，未归属任务未计入。未知字段以 — 表示。',48,canvas.height-32,14,theme.muted);
    $('ov-image-page').textContent=(imagePage+1)+' / '+imagePages+' 张';$('ov-image-prev').disabled=imagePage===0;$('ov-image-next').disabled=imagePage>=imagePages-1;
  }
  $('ov-privacy').addEventListener('click',changePrivacy);$('ov-task-privacy').addEventListener('click',changePrivacy);
  $('ov-hide-sites').addEventListener('change',async()=>{hideSites=$('ov-hide-sites').checked;$('ov-search').value='';await privacyChanged();});
  $('ov-hide-names').addEventListener('change',async()=>{hideNames=$('ov-hide-names').checked;await privacyChanged();});
  for(const id of ['ov-search','ov-source','ov-sort'])$(id).addEventListener(id==='ov-search'?'input':'change',render);
  $('ov-all-tasks').addEventListener('click',()=>openTasks());$('ov-share').addEventListener('click',()=>openShare());
  $('ov-close-tasks').addEventListener('click',()=>$('ov-task-dialog').close());$('ov-close-share').addEventListener('click',()=>$('ov-share-dialog').close());
  $('ov-task-dialog').addEventListener('close',()=>{++taskVersion;if(taskTimer){clearInterval(taskTimer);taskTimer=null;}});
  $('ov-share-dialog').addEventListener('close',()=>{++shareVersion;shareRows=null;shareData=null;$('ov-share-canvas').width=1;$('ov-share-canvas').height=1;});
  let searchTimer; $('ov-task-search').addEventListener('input',()=>{clearTimeout(searchTimer);taskPage=1;searchTimer=setTimeout(loadTasks,250);});
  $('ov-task-state').addEventListener('change',()=>{taskPage=1;loadTasks();});
  $('ov-prev').addEventListener('click',()=>{taskPage=Math.max(1,taskPage-1);loadTasks();});$('ov-next').addEventListener('click',()=>{taskPage++;loadTasks();});
  $('ov-share-tasks').addEventListener('click',()=>openShare(taskScope,true));
  $('ov-share-mode').addEventListener('change',()=>{const full=$('ov-share-mode').value==='full';$('ov-share-hide-names').disabled=full;imagePage=0;buildShare();});
  for(const id of ['ov-share-hide-sites','ov-share-hide-names'])$(id).addEventListener('change',()=>{imagePage=0;buildShare();});
  $('ov-image-prev').addEventListener('click',()=>{imagePage=Math.max(0,imagePage-1);paintShare();});$('ov-image-next').addEventListener('click',()=>{imagePage=Math.min(imagePages-1,imagePage+1);paintShare();});
  $('ov-save-image').addEventListener('click',()=>{if(shareBusy||!shareRows)return;
    const version=shareVersion,page=imagePage,mode=$('ov-share-mode').value,name='pt-'+(shareTasks?'tasks':'overview')+'-'+mode+'-'+(page+1)+'.png';
    try{$('ov-share-canvas').toBlob(blob=>{
      // A privacy switch, page turn or closed dialog invalidates an in-flight export.
      if(version!==shareVersion||page!==imagePage||mode!==$('ov-share-mode').value||shareBusy||!$('ov-share-dialog').open)return;
      if(!blob){$('ov-share-status').textContent='生成图片失败，请重试';return;}const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
    },'image/png');}catch{$('ov-share-status').textContent='生成图片失败，请刷新预览后重试';}
  });
  window.PTOverview={open:()=>refresh(true),refresh:()=>refresh(true),poll:()=>refresh(false),tasks:openTasks};
  if(!$('overview-pane').hidden)refresh(true);
})();
