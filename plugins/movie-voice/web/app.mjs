import {parseQuery, mergeCandidates, nativePath, shortcutURL, mediaTypeLabel} from './core.mjs';
const $ = id => document.getElementById(id), dialog = $('detail');
let searchVersion=0, detailVersion=0;
const el = (tag, text, cls) => { const e=document.createElement(tag); if(text)e.textContent=text; if(cls)e.className=cls; return e; };
async function api(path, body) {
  const response = await fetch('/api/v1'+path, {credentials:'same-origin', cache:'no-store',
    headers:{Accept:'application/json', ...(body ? {'Content-Type':'application/json'} : {})},
    ...(body ? {method:'POST',body:JSON.stringify(body)} : {})});
  if(response.status===401) {
    const next=location.pathname+location.search;
    window.top.location.href='/login?next='+encodeURIComponent(next);
    throw new Error('请先登录 MovieClaw');
  }
  const payload=await response.json();
  if(!response.ok || payload.success===false) throw new Error(payload.message || payload.detail || '请求未完成，请重试');
  return payload.data ?? payload;
}
function poster(url, title) {
  const img=el('img',null,'poster'); img.alt=title; img.loading='lazy'; img.referrerPolicy='no-referrer';
  if(url && /^https?:\/\//.test(url)) img.src='/api/v1/images/proxy?url='+encodeURIComponent(url)+'&w=342';
  else if(url?.startsWith('/images/')) img.src='/api/v1'+url;
  img.addEventListener('error',()=>{img.removeAttribute('src');img.alt=title+' · 海报暂不可用';},{once:true});
  return img;
}
function link(label, path, cls='secondary') {
  const a=el('a',label,'button '+cls); a.href=path; a.target='_top'; return a;
}
function action(label, fn, cls) {
  const b=el('button',label,cls); b.type='button'; b.addEventListener('click',async()=>{
    b.disabled=true;
    try {await fn();} catch(e) { const message=el('p',e.message,'warning'); b.after(message); }
    finally {b.disabled=false;}
  });return b;
}
function showOwned(row, root, actions, message) {
  message.textContent=mediaTypeLabel(row)==='剧集'?'此剧集已在媒体服务器上，可能仅有部分季集；可进入详情查看。':'此影片已在媒体服务器上。';
  actions.replaceChildren(link('网页观看',nativePath(row),'secondary'));
  actions.prepend(action('发送到 MovieClaw App',async()=>{
    const state=await api('/push/me');
    const rule=state.events?.find(e=>e.key==='movie-voice.movie.found');
    if(!state.instance_ready || !state.ready_devices || rule?.enabled===false)
      throw new Error('当前没有可收此通知的设备，或通知已关闭。请在官方 App 登录此服务器并允许通知；也可以直接网页观看。');
    const result=await api('/plugins/movie-voice/notify',{library_id:row.library_id,media_item_id:row.media_item_id});
    message.textContent=result.message;
  }));
  root.append(el('p','点击手机上的 MovieClaw 通知会打开这部影片的详情，再点播放。没有安装 App、没有推送权限或设备离线时，可使用网页观看。','note'));
}
async function showDetail(row) {
  const version=++detailVersion;
  const root=$('detail-content');root.replaceChildren();
  const grid=el('div',null,'detail-grid'), info=el('div'), wide=el('div',null,'wide');
  info.append(el('h2',row.title),el('p',`${row.release_year || '年份待确认'} · ${mediaTypeLabel(row)} · ${row.provider==='douban'?'豆瓣':'TMDB / 媒体库'}`,'meta'));
  const overview=el('p',row.overview || '正在核对影片资料与库存……','overview');
  const message=el('p','正在核对库存，请稍候……','result-message'), actions=el('div',null,'actions');
  wide.append(overview,message,actions);grid.append(poster(row.poster_url,row.title),info,wide);root.append(grid);
  if(!dialog.open) dialog.showModal();
  try {
    if(row.owned) {showOwned(row,wide,actions,message);overview.textContent='请核对片名、年份和海报，避免选到同名影片。';return;}
    const details=await api('/discover/titles/'+encodeURIComponent(row.title_ref));
    if(version!==detailVersion)return;
    if(details.title?.title_ref!==row.title_ref)throw new Error('影片身份未能核实，请重新搜索');
    if(!['movie','tv'].includes(details.title.media_type))throw new Error('影片类型暂时无法确认，请前往影片资料页核对');
    row={...row,media_type:details.title.media_type};
    info.querySelector('.meta').textContent=`${details.title.release_year || row.release_year || '年份待确认'} · ${mediaTypeLabel(row)} · ${row.provider==='douban'?'豆瓣':'TMDB / 媒体库'}`;
    overview.textContent=details.title.overview || '暂无简介，请通过海报、片名和年份确认。';
    const directors=details.metadata?.directors;
    if(directors?.length)info.append(el('p','导演：'+directors.join(' / '),'meta'));
    const owned=details.title.library_status, library=details.library_links?.[0];
    if(owned?.file_count>0 && library){showOwned({...row,...library,owned:true},wide,actions,message);return;}
    const isTV=row.media_type==='tv', seasonInputs=[];
    if(isTV) {
      const preview=await api('/subscriptions/title-preview',{title_ref:row.title_ref});
      if(version!==detailVersion)return;
      if(preview.status!=='ready' || preview.media?.kind!=='tv')throw new Error('剧集身份或季数未能唯一确认，请前往影片资料页选择');
      const fieldset=el('fieldset',null,'season-selection');fieldset.append(el('legend','选择要订阅的季数（不自动续订未来新季）'));
      for(const season of preview.seasons || []) {
        if(!Number.isInteger(season.season_number) || season.season_number<0)continue;
        const label=el('label'), input=el('input');input.type='checkbox';input.value=String(season.season_number);
        label.append(input,document.createTextNode(`${season.season_number===0?'特别篇':`第${season.season_number}季`} · 已有${season.owned_count ?? 0}/${season.episode_count ?? '?'}集`));
        fieldset.append(label);seasonInputs.push(input);
      }
      if(!seasonInputs.length)throw new Error('尚无可核验的季数，请前往影片资料页查看');
      wide.insertBefore(fieldset,message);
    }
    message.textContent=isTV?'请核对剧集与季数，再确认订阅。':'尚未在当前账号可见的媒体库找到这部电影。';
    actions.append(action(isTV?'确认所选季，订阅并自动下载':'确认这部电影，订阅并自动下载',async()=>{
      const selected_seasons=seasonInputs.filter(input=>input.checked).map(input=>Number(input.value));
      if(isTV && !selected_seasons.length)throw new Error('请至少选择一季，再确认订阅');
      // 订阅前再查库存，避免等待确认期间已入库后重复订阅。
      const latest=await api('/discover/titles/'+encodeURIComponent(row.title_ref));
      // 用户关闭或切换详情后，等待中的核对不能继续发起订阅写入。
      if(version!==detailVersion)return;
      if(latest.title?.title_ref!==row.title_ref)throw new Error('影片身份未能核实');
      if(latest.title.media_type!==row.media_type)throw new Error('影片类型发生变化，请重新搜索');
      if(latest.title?.library_status?.file_count>0 && latest.library_links?.[0]) {
        showOwned({...row,...latest.library_links[0],owned:true},wide,actions,message);return;
      }
      const result=await api('/subscriptions',{title_ref:row.title_ref,...(isTV?{selected_seasons,follow_future:false}:{})});
      message.textContent='已加入订阅，服务器将按现有规则自动搜索、下载和入库。有新进展时沿用你的 MovieClaw 通知设置。';
      actions.replaceChildren(link('查看订阅进度','/subscriptions/'+result.subscription.id,''));
    }));
    actions.append(link('更多影片资料',nativePath(row)));
    wide.append(el('p','使用服务器已配置的资源站点、默认质量规则与入库路由；未找到合适资源时持续等待，不代表已开始下载。豆瓣条目有身份歧义时，请进入影片资料页再次选择。','note'));
  } catch(e) {if(version===detailVersion) {message.textContent='库存或影片资料核对失败，尚未执行订阅。';actions.replaceChildren(link('前往 MovieClaw 核对',nativePath(row)));wide.append(el('p',e.message,'warning'));}}
}
async function search(raw) {
  const {query}=parseQuery(raw); if(!query)return;
  const version=++searchVersion;
  $('status').textContent='正在搜索媒体库和订阅影片资料……';$('results').replaceChildren();$('warnings').replaceChildren();
  const url=new URL(location.href);url.searchParams.set('q',raw);history.replaceState(null,'',url);
  const [local,remote]=await Promise.allSettled([
    api('/search/library?q='+encodeURIComponent(query)+'&limit=100'),
    api('/search/titles',{query,provider:'all',save_history:false})]);
  if(version!==searchVersion)return;
  const library=local.status==='fulfilled'?local.value.items:[];
  const titles=remote.status==='fulfilled'?remote.value.titles:[];
  const rows=mergeCandidates(library,titles,raw);
  if(local.status==='rejected')$('warnings').append(el('p','媒体库搜索暂不可用：'+local.reason.message,'warning'));
  if(remote.status==='rejected')$('warnings').append(el('p','影片资料搜索暂不可用：'+remote.reason.message,'warning'));
  if(remote.status==='fulfilled')for(const provider of remote.value.providers || [])if(!provider.success)
    $('warnings').append(el('p',`${provider.provider==='tmdb'?'TMDB':'豆瓣'}暂不可用，已保留其他来源结果。`,'warning'));
  if(local.value?.next_cursor)$('warnings').append(el('p','库存结果较多，仅展示前 100 条。可加年份缩小候选；点击影片后仍会精确核对库存。','note'));
  if(local.value?.index_pending)$('warnings').append(el('p','媒体库索引正在更新，当前结果可能不完整，请稍后重试。','note'));
  $('status').textContent=rows.length?`找到 ${rows.length} 个影片候选。请核对类型、海报与年份，点击确认。`:
    (local.status==='rejected' && remote.status==='rejected' ? '搜索暂不可用，尚未能确认媒体库中是否存在此影片。' : '没有找到电影或剧集候选。试试原名、别名，或去掉年份后重搜。');
  for(const row of rows) {
    const card=action('',()=>showDetail(row),'card');card.setAttribute('aria-label',`${row.title} ${row.release_year || ''}`);
    card.append(poster(row.poster_url,row.title),el('h2',row.title),el('p',`${row.release_year || '年份待确认'} · ${mediaTypeLabel(row)} · ${row.provider==='douban'?'豆瓣':'TMDB / 媒体库'}`,'meta'),
      el('p',row.owned?'已在媒体服务器上':'点击核对库存与订阅','badge'));
    $('results').append(card);
  }
}
$('close').onclick=()=>{++detailVersion;dialog.close();};
dialog.addEventListener('cancel',()=>{++detailVersion;});
$('search').onsubmit=e=>{e.preventDefault();search($('query').value.trim());};
const query=new URLSearchParams(location.search).get('q');
if(query){$('query').value=query.slice(0,180);search($('query').value);}

const shortcutInput=$('shortcut-url'), generateShortcut=$('generate-shortcut'), copyShortcut=$('copy-shortcut'), toggleShortcut=$('toggle-shortcut');
function shortcutStatus(message, error=false) {
  $('shortcut-status').textContent=message;
  $('shortcut-status').dataset.error=String(error);
}
function clearShortcut() {
  shortcutInput.value='';shortcutInput.type='password';
  copyShortcut.disabled=true;toggleShortcut.disabled=true;
  toggleShortcut.textContent='显示链接';toggleShortcut.setAttribute('aria-pressed','false');
}
generateShortcut.addEventListener('click',async()=>{
  generateShortcut.disabled=true;clearShortcut();shortcutStatus('正在为当前服务器生成专用链接……');
  try {
    const result=await api('/plugins/movie-voice/shortcut/pair',{});
    shortcutInput.value=shortcutURL(result.search_url);
    copyShortcut.disabled=false;toggleShortcut.disabled=false;
    shortcutStatus('已生成。点击“复制链接”，粘贴到快捷指令顶部的 SEARCH_URL 配置框。');
  } catch(error) {clearShortcut();shortcutStatus(error.message,true);}
  finally {generateShortcut.disabled=false;}
});
copyShortcut.addEventListener('click',async()=>{
  if(!shortcutInput.value)return;
  try {
    if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(shortcutInput.value);
    shortcutStatus('已复制。现在打开分享版快捷指令，替换顶部 SEARCH_URL 占位文字。');
  } catch {
    shortcutInput.type='text';toggleShortcut.textContent='隐藏链接';toggleShortcut.setAttribute('aria-pressed','true');
    shortcutInput.focus();shortcutInput.select();shortcutInput.setSelectionRange(0,shortcutInput.value.length);
    let copied=false;
    try {copied=document.execCommand('copy');} catch {}
    shortcutStatus(copied?'已复制。现在打开分享版快捷指令，替换顶部 SEARCH_URL 占位文字。':'浏览器未允许自动复制，链接已选中。请长按选择复制，或按 ⌘C / Ctrl+C。',!copied);
  }
});
toggleShortcut.addEventListener('click',()=>{
  const show=shortcutInput.type==='password';shortcutInput.type=show?'text':'password';
  toggleShortcut.textContent=show?'隐藏链接':'显示链接';toggleShortcut.setAttribute('aria-pressed',String(show));
});
window.addEventListener('pagehide',()=>{clearShortcut();shortcutStatus('尚未生成。点击生成即可获取当前服务器的配置链接。');});

const personalButton=$('download-personal'), personalStatus=$('personal-status');
async function refreshPersonal() {
  try {
    const result=await api('/plugins/movie-voice/shortcut/personal/status');
    personalButton.disabled=!result.available;
    personalStatus.textContent=result.available?'已配置当前服务器，下载并添加即可运行。含个人授权，请勿分享。':result.message;
  } catch {personalButton.disabled=true;personalStatus.textContent='暂时无法检查个人版；通用分享版仍可使用。';}
}
personalButton.addEventListener('click',async()=>{
  personalButton.disabled=true;personalStatus.dataset.error='false';personalStatus.textContent='正在下载已签名的个人版……';
  try {
    const response=await fetch('/api/v1/plugins/movie-voice/shortcut/personal/download',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok) {
      let result={};try {result=await response.json();} catch {}
      throw new Error(result.detail || result.message || '个人版暂不可用，请刷新页面重试');
    }
    const blob=await response.blob();
    if(blob.size<256 || !response.headers.get('Content-Type')?.includes('application/octet-stream'))throw new Error('未收到个人版快捷指令文件');
    const url=URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url;link.download='找电影·个人版.shortcut';document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    personalStatus.textContent='已开始下载个人版。打开文件并添加即可使用，无需粘贴 SEARCH_URL。请勿分享此文件。';
  } catch(error) {personalStatus.dataset.error='true';personalStatus.textContent=error.message;}
  finally {personalButton.disabled=false;}
});
refreshPersonal();
