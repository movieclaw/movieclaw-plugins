"use strict";
(() => {
  const numeric=n=>typeof n==='number'&&Number.isFinite(n)&&n>=0;
  const value=(s,field)=>{const n=field==='upspeed_bytes'?s.live?.[field]:s.profile?.[field];return numeric(n)?n:null;};
  const total=(rows,field)=>{const values=rows.map(s=>value(s,field)).filter(n=>n!==null);const n=values.reduce((a,b)=>a+b,0);return values.length&&Number.isFinite(n)?n:null;};
  function distribution(rows,field){const known=rows.map((site,index)=>({site,index,value:value(site,field)})).filter(r=>r.value!==null).sort((a,b)=>b.value-a.value||a.index-b.index),sum=total(rows,field);
    const positive=known.filter(r=>r.value>0),slices=positive.slice(0,5).map(r=>({name:r.site.name,value:r.value,key:r.site.key}));
    if(positive.length>5)slices.push({name:'其他 '+(positive.length-5)+' 站',value:positive.slice(5).reduce((n,r)=>n+r.value,0),key:null});
    return {known:known.length,missing:rows.length-known.length,sum,ranking:known.slice(0,8),slices:sum>0?slices.map(r=>({...r,fraction:r.value/sum})):[]};
  }
  function manualAge(date,now=new Date()){
    if(typeof date!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(date))return null;
    const stamp=new Date(date+'T00:00:00Z'),today=Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate());
    if(!Number.isFinite(stamp.valueOf())||stamp.toISOString().slice(0,10)!==date||stamp.getUTCFullYear()<2000||stamp.valueOf()>today)return null;
    return Math.floor((today-stamp.valueOf())/86400000);
  }
  function age(rows,date,now){const manual=manualAge(date,now),known=rows.map(s=>s.profile?.age_days).filter(n=>numeric(n)&&Number.isInteger(n));
    if(!known.length&&manual!==null)return {days:manual,source:'自填入圈日期 · 未经站点验证',known:0,total:rows.length,manual:true};
    return {days:known.length?Math.max(...known):null,source:'已接入站点的最早注册记录',known:known.length,total:rows.length,manual:false};
  }
  function milestone(rows){const n=total(rows,'uploaded_bytes'),levels=[[0,'初抵海岸'],[2**40,'资源旅人'],[10*2**40,'做种领航员'],[100*2**40,'群岛守望者'],[2**50,'远洋传薪人'],[10*2**50,'星海收藏家']];
    if(n===null)return {name:'等待上传记录',value:null,next:null,progress:null};
    let i=0;while(i+1<levels.length&&n>=levels[i+1][0])i++;
    const next=levels[i+1]?.[0]??null;
    return {name:levels[i][1],value:n,next,progress:next===null?1:Math.min(1,Math.max(0,(n-levels[i][0])/(next-levels[i][0])))};
  }
  const core={numeric,value,total,distribution,manualAge,age,milestone};
  if(typeof module!=='undefined'&&module.exports){module.exports=core;return;}
  const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const svg=(tag,attrs={})=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))n.setAttribute(key,String(value));return n;};
  const palette=['#79dfb5','#88b7f3','#c4a0f5','#f2c77d','#f396a5','#7abec7'];
  let current=null,rows=[],privateSites=false,lastSignature='',dateOverride='',temporaryDate='',remember=false;
  const storageKey='pt-island-entry-date-v1';
  try{dateOverride=localStorage.getItem(storageKey)||'';remember=Boolean(dateOverride);}catch{}
  const ageText=n=>n===null?'待采集':n>=365?'约 '+(n/365.2425).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 年':n+' 天';
  function bytes(n,speed=false){if(n===null)return '—';let i=0;while(n>=1024&&i<5){n/=1024;i++;}return n.toLocaleString('zh-CN',{maximumFractionDigits:i?2:0})+' '+['B','KiB','MiB','GiB','TiB','PiB'][i]+(speed?'/s':'');}
  const format=(field,n)=>field==='seeding_count'?n.toLocaleString('zh-CN')+' 个':bytes(n,field==='upspeed_bytes');
  function draw(){if(!current)return;const field=$('ov-chart-metric').value,dist=distribution(rows,field),a=age(rows,dateOverride||temporaryDate),m=milestone(rows);
    const signature=JSON.stringify([field,privateSites,current.stale,rows.map(s=>[s.key,s.name,s.icon_url,value(s,field),s.profile?.age_days,s.profile?.uploaded_bytes]),a]);if(signature===lastSignature)return;lastSignature=signature;
    $('ov-age-value').textContent=a.days===null?'待采集 / 可自填':(a.manual?'':'至少 ')+ageText(a.days);
    $('ov-age-source').textContent=a.manual?a.source:a.source+' · '+a.known+' / '+a.total+' 站有记录';
    $('ov-milestone-value').textContent=m.name;$('ov-milestone-source').textContent=m.value===null?'采集累计上传后点亮':m.next===null?'已达到最高展示里程碑':'距下一站 '+bytes(Math.max(0,m.next-m.value));
    $('ov-milestone-progress').value=m.progress??0;$('ov-milestone-progress').setAttribute('aria-label','上传里程碑进度');
    $('ov-chart-note').textContent=(current.stale?'含旧快照或不完整下载器数据。':'')+'当前筛选 '+rows.length+' 站 · '+dist.known+' 站有数值 · '+dist.missing+' 站缺失。累计量含站点促销加成；里程碑仅供娱乐。';
    const bars=$('ov-rank-chart');bars.replaceChildren();
    if(!dist.ranking.length)bars.append(node('p','这个字段尚无数据，采集后再来看。','site-note'));
    for(const r of dist.ranking){const b=node('button',null,'insight-rank-row');b.type='button';const label=node('span',r.site.name,'insight-rank-name');if(r.site.icon_url){const img=node('img');img.src=r.site.icon_url;img.alt='';img.referrerPolicy='no-referrer';img.addEventListener('error',()=>img.remove());label.prepend(img);}const track=svg('svg',{viewBox:'0 0 100 8','aria-hidden':'true',preserveAspectRatio:'none'});track.append(svg('rect',{width:100,height:8,rx:4,class:'insight-bar-track'}),svg('rect',{width:dist.ranking[0].value>0?Math.max(0,100*r.value/dist.ranking[0].value):0,height:8,rx:4,class:'insight-bar-fill'}));
      b.append(label,track,node('strong',format(field,r.value)));b.title=r.site.name+' · '+format(field,r.value)+'；点击查看统计检查';b.addEventListener('click',()=>window.PTStatistics?.inspect(r.site,{hideSites:privateSites,stale:current.profile_snapshot_stale}));bars.append(b);}
    $('ov-rank-caption').textContent='展示前 '+dist.ranking.length+' / '+dist.known+' 站；点击一行可查看采集结果。';
    const pie=$('ov-pie-chart'),legend=$('ov-pie-legend');pie.replaceChildren();legend.replaceChildren();
    const plot=svg('svg',{viewBox:'0 0 200 200',role:'img','aria-label':dist.sum>0?'当前筛选站点的数值占比':'当前字段暂无可绘制占比'});plot.append(svg('circle',{cx:100,cy:100,r:72,fill:'none',stroke:'currentColor','stroke-width':22,class:'insight-ring-track'}));
    let offset=0;const circumference=2*Math.PI*72;dist.slices.forEach((slice,i)=>{const circle=svg('circle',{cx:100,cy:100,r:72,fill:'none',stroke:palette[i],'stroke-width':22,'stroke-dasharray':slice.fraction*circumference+' '+circumference,'stroke-dashoffset':-offset*circumference,transform:'rotate(-90 100 100)',class:'insight-slice'});const title=svg('title');title.textContent=slice.name+' '+(slice.fraction*100).toFixed(1)+'%';circle.append(title);plot.append(circle);offset+=slice.fraction;
      const item=node('button',null,'insight-legend-item');item.type='button';const swatch=svg('svg',{viewBox:'0 0 10 10','aria-hidden':'true'});swatch.append(svg('circle',{cx:5,cy:5,r:4,fill:palette[i]}));item.append(swatch,node('span',slice.name),node('strong',(slice.fraction*100).toLocaleString('zh-CN',{maximumFractionDigits:1})+'%'));item.title=format(field,slice.value);item.disabled=!slice.key;item.addEventListener('click',()=>{const site=rows.find(s=>s.key===slice.key);if(site)window.PTStatistics?.inspect(site,{hideSites:privateSites,stale:current.profile_snapshot_stale});});legend.append(item);});
    const main=svg('text',{x:100,y:96,'text-anchor':'middle',class:'insight-ring-value'});main.textContent=dist.sum===null?'待采集':dist.sum===0?'全部为零':dist.known+' 站';const sub=svg('text',{x:100,y:120,'text-anchor':'middle',class:'insight-ring-label'});sub.textContent=dist.slices.length?'数值占比':'暂无占比';plot.append(main,sub);pie.append(plot);
    if(!dist.slices.length)legend.append(node('p',dist.sum===0?'有记录的数值均为零，不绘制占比。':'缺失字段不会按零值参与占比。','site-note'));
  }
  function openAge(){const raw=dateOverride||temporaryDate;$('ov-entry-date').value=raw;$('ov-age-remember').checked=remember;$('ov-age-message').textContent='优先使用当前筛选站点的注册记录自动计算；均未采集到时，才使用自填参考日期。自填不会改写站点注册时间；默认不保存，勾选后只保存在本浏览器当前服务器地址下。';$('ov-entry-date').max=new Date().toISOString().slice(0,10);$('ov-age-dialog').showModal();}
  $('ov-age-settings').addEventListener('click',openAge);$('ov-age-close').addEventListener('click',()=>$('ov-age-dialog').close());
  $('ov-age-apply').addEventListener('click',()=>{const raw=$('ov-entry-date').value;if(manualAge(raw)===null){$('ov-age-message').textContent='请填写 2000 年至今天之间的有效日期。';return;}remember=$('ov-age-remember').checked;temporaryDate=raw;dateOverride='';try{if(remember){localStorage.setItem(storageKey,raw);dateOverride=raw;}else localStorage.removeItem(storageKey);}catch{remember=false;}$('ov-age-dialog').close();lastSignature='';draw();});
  $('ov-age-reset').addEventListener('click',()=>{dateOverride='';temporaryDate='';remember=false;try{localStorage.removeItem(storageKey);}catch{}$('ov-age-dialog').close();lastSignature='';draw();});
  $('ov-chart-metric').addEventListener('change',()=>{lastSignature='';draw();});
  window.PTInsights={render:(data,sites,options={})=>{current=data;rows=sites;privateSites=Boolean(options.hideSites);draw();},clear:()=>{current=null;rows=[];lastSignature='';for(const id of ['ov-rank-chart','ov-pie-chart','ov-pie-legend'])$(id).replaceChildren();$('ov-age-value').textContent='—';$('ov-age-source').textContent='';$('ov-milestone-value').textContent='—';$('ov-milestone-source').textContent='';if($('ov-age-dialog').open)$('ov-age-dialog').close();$('ov-entry-date').value='';},summary:sites=>{const a=age(sites,dateOverride||temporaryDate),m=milestone(sites);return {age:a.days===null?'P 龄待采集':(a.manual?'自填 P 龄参考：':a.days>=365?'P 龄参考 · 至少 ':'已知最早注册记录：')+(a.days>=365?ageText(a.days):'不足 1 年'),milestone:m.name};}};
})();
