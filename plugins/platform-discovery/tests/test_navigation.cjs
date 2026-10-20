'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assets=path.join(__dirname,'../platform_discovery/assets');
const context={URL,URLSearchParams,document:{getElementById:()=>null}};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(assets,'navigation.js'),'utf8'),context);
for(const origin of ['http://192.168.1.100:3000','https://media.example.org:8443','http://[::1]:3000/'])assert.equal(context.MovieClawNavigation.entry(origin),new URL(origin).origin+'/api/v1/plugins/platform-discovery/panel?view=personal');
for(const origin of ['https://u:p@example.com','javascript:alert(1)','https://example.com/path','https://example.com?token=secret','https://example.com/#abc','//example.com','http://evil\\example.com','http://bad host:3000'])assert.throws(()=>context.MovieClawNavigation.entry(origin));
let redirected='',parsed=false;
const transport={URLSearchParams,AbortController,setTimeout,clearTimeout,TypeError,location:{protocol:'https:',pathname:'/api/v1/plugins/platform-discovery/panel',search:'?view=personal&next=https://evil.test',replace:url=>redirected=url},fetch:async()=>({status:401,json:async()=>{parsed=true;throw Error('non-JSON');}})};
vm.createContext(transport);vm.runInContext(fs.readFileSync(path.join(assets,'transport.js'),'utf8'),transport);
(async()=>{
 await assert.rejects(transport.MovieClawTransport.request('/api/v1/auth/me'),e=>e.status===401);assert.equal(parsed,false);assert.equal(decodeURIComponent(redirected),'/login?next=/api/v1/plugins/platform-discovery/panel?view=personal');
 assert.equal(transport.MovieClawTransport.loginUrl({pathname:'/apps/platform-discovery/',search:''}),'/login?next=%2Fapps%2Fplatform-discovery%2F');
 const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../web/manifest.webmanifest'),'utf8'));assert.equal(manifest.short_name,'映屿');assert.equal(manifest.start_url,'.');assert.ok(fs.existsSync(path.join(__dirname,'../web/index.html')));
 console.log('PASS: LAN/public/IPv6 generated links, unsafe address rejection, 401 before JSON parsing, same-origin login return and standard native-page manifest');
})().catch(e=>{console.error(e);process.exitCode=1;});
