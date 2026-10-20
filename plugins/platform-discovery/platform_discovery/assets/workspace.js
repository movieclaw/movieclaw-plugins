"use strict";
// One shared shell. Move existing controls rather than clone them: account state,
// event handlers and native form semantics stay attached to the original nodes.
(()=>{
 const $=id=>document.getElementById(id),nav=document.querySelector('.discovery-modes');if(!nav)return;
 const create=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;};
 const modes=['personal','theater','platforms','random'],heads={},primaries={},secondaries={},statuses={},extras={};
 const heading=create('section','workspace-heading');heading.id='workspace-heading';
 const toolbar=create('section','workspace-toolbar');toolbar.id='workspace-toolbar';toolbar.setAttribute('aria-label','当前板块操作');
 const primary=create('div','workspace-primary'),secondary=create('div','workspace-secondary'),status=create('div','workspace-status');
 const more=create('button','button workspace-more','更多设置');more.type='button';more.id='workspace-more';more.setAttribute('aria-haspopup','dialog');more.setAttribute('aria-controls','workspace-settings');
 nav.after(heading,toolbar);toolbar.append(primary,secondary,status);
 const sheet=create('dialog','shelf-dialog workspace-settings');sheet.id='workspace-settings';sheet.setAttribute('aria-labelledby','workspace-settings-title');
 const top=create('div','shelf-heading'),title=create('h2',null,'页面设置与工具');title.id='workspace-settings-title';
 const close=create('button','button','完成');close.type='button';close.id='workspace-settings-close';top.append(title,close);sheet.append(top);document.body.append(sheet);
 for(const mode of modes){
  const p=create('div','workspace-view-controls'),s=create('div','workspace-view-secondary'),t=create('div','workspace-view-status'),e=create('div','workspace-view-extra');
  for(const element of [p,s,t,e])element.dataset.workspaceView=mode;
  primaries[mode]=p;secondaries[mode]=s;statuses[mode]=t;extras[mode]=e;primary.append(p);secondary.append(s);status.append(t);sheet.append(e);
 }
 primary.append(more);
 const move=(selector,parent)=>{const n=document.querySelector(selector);if(n)parent.append(n);return n;};
 heads.personal=move('.wall-intro',heading);heads.theater=move('.theater-intro',heading);heads.platforms=move('.intro',heading);heads.random=move('.random-intro',heading);
 for(const mode of modes)heads[mode].dataset.workspaceView=mode;
 // Main actions stay in a predictable place on all four pages.
 move('.wall-filters',primaries.personal);move('#wall-preset-shortcuts',secondaries.personal);move('#wall-status',statuses.personal);
 move('.wall-toolbar',extras.personal);move('.wall-learning',extras.personal);move('#taste-history',extras.personal);move('#preset-shortcut-status',extras.personal);
 const personalTools=create('div','workspace-tool-buttons');extras.personal.prepend(personalTools);
 for(const id of ['wall-presets','wall-browse-history','wall-shelf'])move('#'+id,personalTools);
 move('#wall-filter-sync',extras.personal);
 move('.theater-toolbar',primaries.theater);move('#theater-status',statuses.theater);move('#theater-retry',secondaries.theater);move('#theater-filter-open',heads.theater);move('.theater-cache-tools',extras.theater);
 const theaterAdvanced=create('div','workspace-phone-controls');extras.theater.prepend(theaterAdvanced);
 // Keep labels/inputs together when moving less-frequent phone controls.
 const ports=[...primaries.theater.querySelectorAll('label')].slice(2).map(n=>{const anchor=document.createComment('desktop control');n.before(anchor);return {n,anchor,target:theaterAdvanced};});
 move('.filter-line',primaries.platforms);move('#platform-index',secondaries.platforms);move('.intro-note',extras.platforms);move('#proxy-test',heads.platforms);
 const platformAdvanced=create('div','workspace-phone-controls');extras.platforms.prepend(platformAdvanced);
 const kind=document.querySelector('.kind-filter');if(kind){const anchor=document.createComment('desktop kind');kind.before(anchor);ports.push({n:kind,anchor,target:platformAdvanced});}
 // The old sticky wrapper is no longer a second toolbar above the content.
 const oldControls=document.querySelector('.discovery-controls');if(oldControls)oldControls.remove();
 move('#recommendation-tabs',secondaries.random);move('.random-rule',statuses.random);move('#shuffle-recommendations',heads.random);
 const randomHint=create('span','workspace-control-hint','按地区随选 · 新剧老剧一起看');primaries.random.prepend(randomHint);
 extras.random.append(create('p','shelf-note','从下方地区标签选择片单，点击“换一批”继续探索。不限制上映年份；尽量避开近期已经展示的作品。'));
 $('wall-genres').setAttribute('aria-label','国家、年代、题材与探索筛选');
 const wallHint=create('span','workspace-secondary-hint','收藏的筛选方案会显示在这里');secondaries.personal.append(wallHint);
 const theaterHint=create('span','workspace-secondary-hint','媒体库库存 · 可筛选观看状态、题材、地区与年代');secondaries.theater.append(theaterHint);
 let mode='personal';
 function setMode(next){
  const previous=mode;mode=modes.includes(next)?next:'personal';if(sheet.open)sheet.close();if(previous!==mode)window.scrollTo({top:0,left:0,behavior:'instant'});
  for(const key of modes){for(const n of [heads[key],primaries[key],secondaries[key],statuses[key],extras[key]])n.hidden=key!==mode;}
  more.setAttribute('aria-label',({personal:'海报墙',theater:'录像厅',platforms:'平台热榜',random:'随心看剧'}[mode])+'的更多设置');

 }
 const globalTools=create('div','workspace-global-tools');globalTools.append(create('h3',null,'连接与插件'));sheet.append(globalTools);
 for(const n of [$('entry-links-open'),document.querySelector('.app-header nav a[href="/settings/plugins/platform-discovery"]')])if(n){const anchor=document.createComment('desktop header action');n.before(anchor);ports.push({n,anchor,target:globalTools});}
 const phone=matchMedia('(max-width:767px)');
 function adapt(){for(const {n,anchor,target} of ports){if(phone.matches)target.append(n);else anchor.after(n);}document.body.classList.toggle('workspace-phone',phone.matches);}
 phone.addEventListener('change',adapt);adapt();setMode(new URLSearchParams(location.search).get('view')||'personal');
 more.addEventListener('click',()=>{sheet.showModal();sheet.scrollTop=0;});close.addEventListener('click',()=>sheet.close());
 // Close this sheet before an existing control opens another dialog. Display
 // switches stay in place, so several settings can be changed in one visit.
 const dialogButtons=new Set(['wall-presets','wall-browse-history','wall-shelf','theater-cache-open','wall-fullscreen','taste-start','entry-links-open','wall-ambient-settings']);
 sheet.addEventListener('click',event=>{const b=event.target.closest('button');if(b&&dialogButtons.has(b.id))sheet.close();},true);
 sheet.addEventListener('close',()=>{if(!document.querySelector('dialog[open]')&&!document.body.classList.contains('wall-immersive'))more.focus({preventScroll:true});});
 globalThis.MovieClawWorkspace={setMode};document.body.classList.add('workspace-ready');
})();
