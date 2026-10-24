"use strict";
(() => {
  const $=id=>document.getElementById(id),base='/api/v1/plugins/pt-site-pack';
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const action=(text,fn)=>{const b=node('button',text,'button secondary');b.type='button';b.addEventListener('click',fn);return b;};
  const labels={collected:'已采集',calculated:'计算值',unrecognized:'未识别',pending:'待采集 / 验证中',request_failed:'请求失败',unconfigured:'未配置账号',paused:'账号暂停',unavailable:'暂不可用',partial:'部分可用',no_tasks:'暂无关联任务'};
  let selected=null,hiddenSites=false,inspectionStale=false,context=null,version=0,controller=null,busy=false,repairFields=[];
  const date=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚无记录';
  const format=(field,value)=>{
    if(value==null)return '—';
    if(field==='age_days')return value.toLocaleString('zh-CN')+' 天 · 约 '+(value/365.2425).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 年';
    if(field==='hr')return value.total?value.known+' / '+value.total+' 项有明确标记':'尚未检查任务标记';
    if(field.endsWith('_bytes')){let n=value,i=0;while(n>=1024&&i<5){n/=1024;i++;}return n.toLocaleString('zh-CN',{maximumFractionDigits:i?2:0})+' '+['B','KiB','MiB','GiB','TiB','PiB'][i]+(['upspeed_bytes','dlspeed_bytes'].includes(field)?'/s':'');}
    return value.toLocaleString('zh-CN',{maximumFractionDigits:2});
  };
  function message(text,error=false){$('statistics-repair-message').textContent=text;$('statistics-repair-message').className='notice'+(error?' error':'');}
  async function request(path,body,signal){const response=await fetch(base+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal});const value=await response.json();if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'操作失败，请刷新后重试');return value.data||value;}
  function canRepair(){return selected&&!hiddenSites&&!inspectionStale&&selected.verification==='active'&&selected.usable&&selected.configured;}
  function renderInspection(){if(!selected)return;const report=selected.collection||{},box=$('statistics-results');box.replaceChildren();
    $('statistics-title').textContent=selected.name+' · 统计采集检查';
    $('statistics-status').textContent=(inspectionStale?'读取失败，当前为旧快照。':'')+'个人统计 '+(report.profile_collected??0)+' / '+(report.profile_total??6)+' 项可用。'+(report.note||'');
    $('statistics-attempt').textContent=(report.attempt_message||selected.verification_label)+' · 最近验证 '+date(report.attempted_at)+(report.rules_updated_at?' · 补充规则更新 '+date(report.rules_updated_at)+'，保存后需重新采集验证':'');
    for(const row of report.fields||[]){const card=node('article',null,'statistics-result'),head=node('div',null,'statistics-result-head');
      head.append(node('strong',row.label),node('span',labels[row.status]||'未知','badge '+(['request_failed','unrecognized'].includes(row.status)?'pending':row.status==='collected'?'active':'')));card.append(head,node('div',format(row.field,row.value),'statistics-value'));
      card.append(node('p',row.source+' · '+date(row.observed_at)+(row.stale?' · 资料已过期 / 快照不完整':''),'site-note'),node('p',row.reason,'site-note'));
      if(row.selector&&row.value==null&&canRepair())card.append(action('补齐此项',()=>openRepair([row.field])));
      box.append(card);
    }
    $('statistics-collect').disabled=!selected.configured||selected.verification==='verifying'||selected.verification==='paused'||inspectionStale;
    $('statistics-repair').disabled=!canRepair();
    $('statistics-message').textContent=hiddenSites?'当前隐藏站点名称；关闭“同时隐藏站点”后才能打开含真实站点名称的配置助手。':inspectionStale?'请先刷新恢复最新站点状态，再进行采集或配置。':!selected.usable?'请先配置或恢复账号验证，再判断字段是否需要适配。':'实时任务字段需检查下载器及任务归属；H&R 请在任务明细中逐种核对。';
  }
  function inspect(site,options={}){selected=site;hiddenSites=Boolean(options.hideSites);inspectionStale=Boolean(options.stale);renderInspection();if(!$('statistics-dialog').open)$('statistics-dialog').showModal();}
  function update(data){inspectionStale=Boolean(data.profile_snapshot_stale);if(selected&&$('statistics-dialog').open){const found=data.sites.find(s=>s.key===selected.key);if(found){selected=found;renderInspection();}else{$('statistics-dialog').close();selected=null;}}}
  function values(){const result={};for(const input of $('statistics-selector-inputs').querySelectorAll('input[data-selector]'))if(input.value.trim())result[input.dataset.selector]=input.value.trim();return result;}
  function fillSelectors(selectors){for(const input of $('statistics-selector-inputs').querySelectorAll('input[data-selector]'))input.value=selectors[input.dataset.selector]||'';}
  function preview(fields){const box=$('statistics-sample-results');box.replaceChildren(node('p','以下仅为样本预览，不是服务器账号实测结果。','site-note'));for(const field of context.fields){const row=node('div',null,'statistics-sample-row');row.append(node('span',field.label),node('strong',format(field.field,fields[field.field])),node('span',fields[field.field]==null?'未命中有效数值':'样本可解析','site-note'));box.append(row);}}
  function buttons(disabled){for(const id of ['statistics-generate','statistics-preview','statistics-save','statistics-rollback'])$(id).disabled=disabled;}
  function leave(){++version;controller?.abort();controller=null;context=null;busy=false;$('statistics-html').value='';$('statistics-model-consent').checked=false;$('statistics-selector-inputs').replaceChildren();$('statistics-sample-results').replaceChildren();$('statistics-repair-title').textContent='补齐统计';$('statistics-repair-context').textContent='';$('statistics-repair-form').hidden=true;message('');}
  async function openRepair(fields){if(!canRepair())return;const sid=selected.site_id;repairFields=fields||selected.profile.missing_fields||[];
    $('statistics-dialog').close();leave();const current=version;window.PTPanel.showStatistics();message('正在读取本站统计补充规则…');
    try{const result=await request('/statistics/'+encodeURIComponent(sid));if(current!==version)return;context=result;
      $('statistics-repair-title').textContent=result.name+' · 补齐统计';$('statistics-repair-context').textContent=result.reason;$('statistics-repair-form').hidden=!result.supported;
      if(!result.supported){message('此类站点请向适配维护者提供脱敏的接口字段结构或错误信息；不要发送 Cookie、API Key 或其他凭据。');return;}
      const inputs=$('statistics-selector-inputs');for(const field of result.fields){const wrap=node('div',null,'statistics-selector-row'),label=node('label'),checkLabel=node('label',null,'inline-check'),check=node('input');check.type='checkbox';check.dataset.field=field.field;check.checked=!repairFields.length||repairFields.includes(field.field);checkLabel.append(check,node('span','生成目标 · '+field.label));
        const input=node('input');input.type='text';input.maxLength=500;input.spellcheck=false;input.dataset.selector=field.selector;input.setAttribute('aria-label',field.label+' CSS 选择器');input.placeholder='可留空沿用原适配；例如 #my-bonus::text';label.append(node('span',field.label+' CSS 选择器'),input);wrap.append(checkLabel,label);inputs.append(wrap);}
      fillSelectors(result.selectors);buttons(false);$('statistics-rollback').disabled=!result.history_count;
      message('样本不会保存。请先预览新增规则，保存后再重新采集；清空选择器可恢复原适配读取方式。');
    }catch(error){if(current===version)message(error.message,true);}
  }
  async function run(kind){if(!context||!context.supported||busy)return;const current=version,sid=context.site_id,aborter=new AbortController();busy=true;buttons(true);controller=aborter;const timer=setTimeout(()=>aborter.abort(),kind==='draft'?110000:25000);
    try{const payload={html:$('statistics-html').value,selectors:values(),revision:context.revision};
      if(kind==='draft'){payload.allow_model=$('statistics-model-consent').checked;payload.fields=Array.from($('statistics-selector-inputs').querySelectorAll('input[data-field]:checked')).map(i=>i.dataset.field);message('正在生成统计选择器草稿，请等待…');}
      else message(kind==='preview'?'正在解析样本…':kind==='save'?'正在保存统计规则…':'正在恢复上一版规则…');
      const result=await request('/statistics/'+encodeURIComponent(sid)+'/'+kind,payload,aborter.signal);if(current!==version)return;
      if(result.selectors)fillSelectors(result.selectors);if(result.sample_fields)preview(result.sample_fields);
      if(kind==='save'||kind==='rollback'){const fresh=await request('/statistics/'+encodeURIComponent(sid),undefined,aborter.signal);if(current!==version)return;context=fresh;fillSelectors(context.selectors);if(kind==='rollback')$('statistics-sample-results').replaceChildren();}
      message(result.message||'样本预览完成；只有能解析的规则才能保存，未命中项请调整或留空。');
    }catch(error){if(current===version)message(error.name==='AbortError'?'操作超时或已离开页面，请刷新确认状态；不要反复生成模型草稿。':error.message,true);}
    finally{clearTimeout(timer);if(current===version){busy=false;controller=null;buttons(false);$('statistics-rollback').disabled=!context?.history_count;}}
  }
  $('statistics-close').addEventListener('click',()=>$('statistics-dialog').close());
  $('statistics-repair').addEventListener('click',()=>openRepair());
  $('statistics-tasks').addEventListener('click',()=>{if(!selected)return;$('statistics-dialog').close();window.PTOverview.tasks(selected.key);});
  $('statistics-collect').addEventListener('click',async()=>{if(!selected)return;const key=selected.key;const button=$('statistics-collect');button.disabled=true;
    try{const result=await request('/overview/collect?'+new URLSearchParams({site_key:key}),{});if(selected?.key===key)$('statistics-message').textContent=result.message;setTimeout(()=>window.PTOverview.refresh(),5000);}
    catch(error){if(selected?.key===key)$('statistics-message').textContent=error.message;}finally{if(selected?.key===key)button.disabled=selected.verification==='verifying';}
  });
  $('statistics-back').addEventListener('click',()=>window.PTPanel.overview());
  for(const [id,kind]of [['statistics-generate','draft'],['statistics-preview','preview'],['statistics-save','save'],['statistics-rollback','rollback']])$(id).addEventListener('click',()=>run(kind));
  $('statistics-html').addEventListener('input',()=>$('statistics-sample-results').replaceChildren());
  $('statistics-selector-inputs').addEventListener('input',event=>{if(event.target.dataset.selector)$('statistics-sample-results').replaceChildren();});
  window.PTStatistics={inspect,update,leave,closeForPrivacy:()=>{$('statistics-dialog').close();selected=null;for(const id of ['statistics-title','statistics-status','statistics-attempt','statistics-results','statistics-message'])$(id).replaceChildren();leave();}};
})();
