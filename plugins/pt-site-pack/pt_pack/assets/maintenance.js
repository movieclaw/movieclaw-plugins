'use strict';
(() => {
  const $=id=>document.getElementById(id),base='/api/v1/plugins/pt-site-pack';
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  let data=null,dirty=false,loading=false,saving=false,queueing=false,lastRead=0,version=0;
  const drafts=new Map(),opened=new Map();
  function status(text,error=false){$('maint-status').textContent=text;$('maint-status').className='notice'+(error?' error':'');}
  async function api(path,body){const r=await fetch(base+path,{credentials:'same-origin',cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});let d;try{d=await r.json();}catch{throw new Error('维护记录暂不可读取');}if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'请求失败，请刷新后重试');return d.data||d;}
  function time(value){if(!value)return'—';try{return new Intl.DateTimeFormat('zh-CN',{timeZone:data?.timezone||'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).format(new Date(value));}catch{return'—';}}
  function button(text,fn){const b=el('button',text,'button secondary');b.type='button';b.addEventListener('click',fn);return b;}
  function input(label,value,type='text'){const wrap=el('label',label),n=el('input');n.type=type;n.value=value||'';wrap.append(n);return[wrap,n];}
  function field(label,value){const wrap=el('div');wrap.append(el('dt',label),el('dd',value));return wrap;}
  function change(){dirty=true;status('有尚未保存的修改；点击“保存当前选择”，或重新生成随机时间并保存。');}
  async function run(site,action){if(dirty){status('请先保存当前修改，再执行检查或签到。',true);return;}try{status(site.name+'：正在排队…');const r=await api('/maintenance/'+encodeURIComponent(site.site_id)+'/'+action,{});status(site.name+'：'+r.message);await refresh(true);}catch(e){status(e.message,true);}}
  function batchBusy(){return ['queued','running'].includes(data?.batch?.status);}
  function batchStatus(){
    const b=data?.batch,active=batchBusy(),usable=data?.sites.some(s=>s.usable);
    for(const id of['maint-both','maint-sign-all','maint-visit-all'])$(id).disabled=queueing||active||!usable;
    const labels={queued:'已排队',running:'执行中',completed:'已完成',interrupted:'执行中断',failed:'执行失败，请检查站点记录'};
    const name=b?.current?data.sites.find(s=>s.site_id===b.current)?.name:null;
    $('maint-batch-status').textContent=b?'批量任务'+labels[b.status]+' · 已处理 '+b.completed+' / '+b.total+' 站 · 跳过 '+b.skipped+' 站'+(name?' · 当前：'+name:'')+'。处理数量包含失败，请查看各站结果。':'首次执行后会分别显示签到与访问的判定，成功时间持续保留。模拟访问不保证站点将其计入活跃时间或免除保号要求。';
  }
  async function batch(action){
    if(queueing||batchBusy())return;if(dirty){status('请先保存当前修改，再批量执行。',true);return;}
    queueing=true;batchStatus();try{const r=await api('/maintenance/batch',{action});status(r.message);await refresh(true);}catch(e){status(e.message,true);}finally{queueing=false;batchStatus();}
  }
  function capability(site,action){
    const r=site.operations?.[action],confirmed=action==='sign'?site.last_signed_at:site.last_visited_at;
    const errors={challenge:'需要人工验证',login_required:'需要重新登录',blocked:'访问被拦截',rate_limited:'站点限流',failed:'本次失败',uncertain:'结果未确认',interrupted:'执行中断'};
    let text=confirmed?(action==='sign'?'已验证可签到':site.visit_method==='api'?'API 访问已验证':'登录网页访问已验证'):'尚未验证';
    if(r?.status==='unsupported')text='尚无适配，不能判定站点不支持';
    else if(errors[r?.status])text+=(confirmed?' · ':' · 本次未通过：')+errors[r.status];
    else if(!confirmed&&r)text='已检查，尚未确认可用';
    return text;
  }
  function card(site){
    const draft=drafts.get(site.site_id),cfg=draft||structuredClone(site.config);drafts.set(site.site_id,cfg);
    const root=el('article',null,'maintenance-card'),head=el('div',null,'maintenance-head');
    if(site.icon_url===base+'/icons/'+encodeURIComponent(site.site_id)){const img=el('img');img.src=site.icon_url;img.alt='';img.width=32;img.height=32;img.addEventListener('error',()=>img.remove());head.append(img);}
    head.append(el('h3',site.name),el('span',site.label,'badge '+(site.status==='signed'?'active':['failed','challenge','login_required','uncertain','interrupted','blocked','rate_limited'].includes(site.status)?'failed':'pending')));root.append(head);
    const controls=el('div',null,'maintenance-controls'),check=el('label',null,'inline-check'),enabled=el('input');enabled.type='checkbox';enabled.checked=cfg.enabled;enabled.disabled=!site.usable&&!cfg.enabled;enabled.setAttribute('aria-label',site.name+' 自动维护');enabled.addEventListener('change',()=>{cfg.enabled=enabled.checked;change();});check.append(enabled,el('span','自动维护'));controls.append(check);
    const mode=el('label','维护方式'),select=el('select');select.setAttribute('aria-label',site.name+' 维护方式');for(const[value,text]of[['sign','访问并签到'],['visit','仅访问检查']]){const o=el('option',text);o.value=value;select.append(o);}select.value=cfg.action;select.addEventListener('change',()=>{cfg.action=select.value;change();});mode.append(select);controls.append(mode);root.append(controls);
    const facts=el('dl',null,'maintenance-facts');facts.append(field('签到能力',capability(site,'sign')),field('访问能力',capability(site,'visit')),field('最近签到确认',time(site.last_signed_at)),field('最近成功访问',time(site.last_visited_at)),field('最近检查',time(site.checked_at)),field('下一次自动执行',site.next_at?time(site.next_at):'未启用'));
    for(const[action,label]of[['sign','最近签到结果'],['visit','最近访问结果']]){const r=site.operations?.[action];if(r)facts.append(field(label,time(r.checked_at)+' · '+r.message));}
    root.append(facts,el('p',site.message||'尚未检查。可先检查当前会话，或手动执行今日签到。','site-note'));
    const actions=el('div',null,'actions');const checkBtn=button('检查今日状态',()=>run(site,'check')),sign=button(site.status==='signed'?'今日已签到':['failed','challenge','uncertain','login_required','interrupted'].includes(site.status)?'重试今日签到':'补签今日',()=>run(site,'sign')),visit=button('模拟访问',()=>run(site,'visit'));for(const b of[checkBtn,sign,visit])b.disabled=!site.usable||site.status==='running'||batchBusy();if(site.status==='signed')sign.disabled=true;actions.append(checkBtn,sign,visit);
    if(site.website_url){const a=el('a','打开站点处理 ↗','button secondary');a.href=site.website_url;a.target='_blank';a.rel='noopener noreferrer';a.referrerPolicy='no-referrer';actions.append(a);}root.append(actions);
    if(!site.usable)root.append(el('p','请先在原生站点设置中配置并验证账号。','site-note'));
    const details=el('details',null,'maintenance-rule');details.open=Boolean(opened.get(site.site_id)?.rule);details.addEventListener('toggle',()=>opened.set(site.site_id,{...opened.get(site.site_id),rule:details.open}));details.append(el('summary','签到适配设置'));
    details.append(el('p','常见 NexusPHP 默认检测 attendance.php，仅在页面明确成功时标记已签到。其他 HTML 站点可填写已确认的签到路径和仅在今日已签时出现的 CSS 选择器；API 签到需要专用适配。验证码需手动处理。','site-note'));
    const [pathWrap,path]=input('签到路径（不含域名及参数）',cfg.rule?.path||'');path.placeholder='attendance.php';const[cssWrap,css]=input('今日已签到 CSS 选择器（可选）',cfg.rule?.signed_css||'');css.placeholder='例如 #attendance-today.signed';
    const ruleChanged=()=>{cfg.rule=path.value.trim()?{path:path.value.trim(),signed_css:css.value.trim()}:null;change();};path.addEventListener('input',ruleChanged);css.addEventListener('input',ruleChanged);details.append(pathWrap,cssWrap,el('p','此处只支持 attendance / checkin / signin 页面路径。选择器需要从已登录网页的签到状态元素获取；只填“签到按钮”不能作为成功证据。修改后保存，再手动检查。','site-note'));root.append(details);
    if(site.history?.length){const history=el('details',null,'maintenance-history');history.open=Boolean(opened.get(site.site_id)?.history);history.addEventListener('toggle',()=>opened.set(site.site_id,{...opened.get(site.site_id),history:history.open}));history.append(el('summary','最近执行记录'));const list=el('ul');for(const h of site.history)list.append(el('li',time(h.at)+' · '+h.label+' · '+h.message));history.append(list);root.append(history);}return root;
  }
  function render(){if(!data)return;if(!dirty){$('maint-timezone').value=data.timezone;$('maint-start').value=data.start;$('maint-end').value=data.end;drafts.clear();}
    const usable=data.sites.filter(s=>s.usable),chosen=usable.filter(s=>(drafts.get(s.site_id)||s.config).enabled).length;$('maint-all').checked=usable.length>0&&chosen===usable.length;$('maint-all').indeterminate=chosen>0&&chosen<usable.length;
    batchStatus();$('maint-sites').replaceChildren(...data.sites.map(card));if(!data.sites.length)$('maint-sites').append(el('p','尚未添加站点，请先到“添加站点”接入自己的站点。','notice'));
  }
  async function refresh(force=false){if($('maintenance-pane').hidden&&!force)return;if(loading||(!force&&Date.now()-lastRead<5000))return;
    loading=true;const ticket=++version;try{const next=await api('/maintenance');if(ticket!==version)return;data=next;lastRead=Date.now();batchStatus();if(!dirty)render();else{for(const site of data.sites){/* Preserve edits while the background status updates. */if(!drafts.has(site.site_id))drafts.set(site.site_id,structuredClone(site.config));}}if(!dirty)status('今日 '+data.today+' · 已确认签到 '+data.sites.filter(s=>s.status==='signed').length+' / '+data.sites.length+' 站。'+(data.sites.some(s=>s.status==='running')?'正在执行，请稍候查看结果。':'刷新不会触发访问或签到。'));}catch(e){status(e.message,true);}finally{loading=false;}}
  async function save(randomize=false,stop=false){if(!data||saving)return;const sites={};for(const s of data.sites){const cfg=structuredClone(stop?s.config:(drafts.get(s.site_id)||s.config));if(stop)cfg.enabled=false;sites[s.site_id]=cfg;}
    if(randomize&&!Object.values(sites).some(s=>s.enabled)){status('请至少勾选一个要维护的站点。',true);return;}saving=true;for(const id of['maint-save','maint-randomize','maint-stop'])$(id).disabled=true;
    try{const result=await api('/maintenance/plan',{revision:data.revision,timezone:stop?data.timezone:$('maint-timezone').value,start:stop?data.start:$('maint-start').value,end:stop?data.end:$('maint-end').value,randomize,sites});data=result;dirty=false;render();status(stop?'自动维护已全部暂停。':randomize?'随机计划已生成并保存；每日会重新错开时间，服务器会自动执行。':'计划已保存。');}catch(e){status(e.message,true);}finally{saving=false;for(const id of['maint-save','maint-randomize','maint-stop'])$(id).disabled=false;}}
  $('maint-all').addEventListener('change',event=>{if(!data)return;for(const site of data.sites){const cfg=drafts.get(site.site_id)||structuredClone(site.config);if(site.usable)cfg.enabled=event.currentTarget.checked;drafts.set(site.site_id,cfg);}change();render();});
  for(const id of['maint-timezone','maint-start','maint-end'])$(id).addEventListener('change',change);
  $('maint-both').addEventListener('click',()=>batch('both'));$('maint-sign-all').addEventListener('click',()=>batch('sign'));$('maint-visit-all').addEventListener('click',()=>batch('visit'));
  $('maint-refresh').addEventListener('click',()=>refresh(true));$('maint-save').addEventListener('click',()=>save());$('maint-randomize').addEventListener('click',()=>save(true));$('maint-stop').addEventListener('click',()=>save(false,true));
  window.PTMaintenance={open:()=>refresh(true),refresh:()=>refresh(true),poll:()=>refresh()};
})();
