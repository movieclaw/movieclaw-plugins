"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'..'),base='/api/v1/plugins/platform-discovery/web/';
let moves=[],topTarget='';const location={pathname:base,search:'',protocol:'https:',assign:value=>topTarget=value};
const ctx={URL,URLSearchParams,location,window:{top:{location:{assign:value=>topTarget=value}}},history:{pushState(_,__,url){moves.push(url);const u=new URL(url,'https://media.example.org');location.pathname=u.pathname;location.search=u.search;}},document:{getElementById:()=>null}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(root,'platform_discovery/assets/navigation.js'),'utf8'),ctx);
const nav=ctx.MovieClawNavigation;
for(const [sub,mode] of [['','personal'],['theater','theater'],['platforms','platforms'],['random','random']]){
 const here={pathname:base+sub,search:''};assert.equal(nav.mode(here),mode);assert.equal(nav.modeUrl(mode,here),base+sub);
 assert.equal(nav.entry('https://media.example.org',{native:true,mode}),'https://media.example.org/apps/platform-discovery/'+sub);
}
nav.updateMode('theater');nav.updateMode('theater');assert.equal(moves.length,1,'no duplicate history entry');
nav.updateMode('random');location.pathname=moves[0];assert.equal(nav.mode(),'theater','browser back selects the route, independent of remembered mode');
assert.equal(nav.isNative({pathname:base.slice(0,-1)+'-other/'}),false);assert.equal(nav.mode({pathname:'/panel',search:'?view=platforms'}),'platforms');
nav.hostNavigate('/play/123/s01e02');assert.equal(topTarget,'/play/123/s01e02');for(const bad of ['//evil.test','javascript:alert(1)','/\\evil.test'])assert.throws(()=>nav.hostNavigate(bad));
vm.runInContext(fs.readFileSync(path.join(root,'platform_discovery/assets/transport.js'),'utf8'),ctx);
for(const prefix of [base,'/apps/platform-discovery/'])for(const sub of ['', 'theater/', 'platforms/', 'random/'])assert.equal(decodeURIComponent(ctx.MovieClawTransport.loginUrl({pathname:prefix+sub,search:'?next=https://evil.test'})),'/login?next=/apps/platform-discovery/'+sub.replace(/\/$/,''));
assert.equal(decodeURIComponent(ctx.MovieClawTransport.loginUrl({pathname:'/apps/platform-discovery-other/',search:''})),'/login?next=/api/v1/plugins/platform-discovery/panel');
const manifest=JSON.parse(fs.readFileSync(path.join(root,'web/manifest.webmanifest')));assert.deepEqual(manifest.shortcuts.map(v=>v.url),['theater','platforms','random']);
for(const entry of [manifest,...manifest.shortcuts])for(const icon of entry.icons)assert.ok(fs.existsSync(path.join(root,'web',icon.src)));
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');assert.ok(!/<iframe\b/i.test(html),'host owns the only iframe');
for(const [,url] of html.matchAll(/(?:src|href)="([^"#]+\.(?:css|js))"/g))assert.ok(url.startsWith('/api/v1/plugins/platform-discovery/'),'assets resolve at every nested menu route');
console.log('PASS: four stable native menu routes, history deduplication, current-origin links, exact login return, top-level playback and single-frame assets');
