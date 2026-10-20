"""Offline subprocess worker: real SDK RPC/store/drive wire, fake host and provider.

Only synthetic journal and provider bytes enter the fixture; credentials stay in RAM.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import httpx
from movieclaw_sdk import drives as dv, net, rpc
from movieclaw_sdk.runner import Runner, RemoteContext
from movieclaw_sdk.services import DataApi, NetApi, StoredValue
from movieclaw_api.plugins.keys import PLUGIN_DATA
from movieclaw_api.services.plugin_drives import RemoteDriveBackend
from cloud_123_personal.backend import Personal123
from tests.fake123 import Fake123
from tests.test_backend import fingerprint
from tests.test_runner import HttpBridge


async def run(path,action):
    fixture=json.loads(path.read_text()) if path.exists() else {'data':{},'provider':{},'paced':0}
    fake=Fake123();provider=fixture['provider']
    fake.rows=provider.get('rows',{})
    fake.parts={int(n):base64.b64decode(b) for n,b in provider.get('parts',{}).items()}
    fake.upload=provider.get('upload')
    def persist():
        fixture['provider']={'rows':fake.rows,'parts':{str(n):base64.b64encode(b).decode() for n,b in fake.parts.items()},'upload':fake.upload}
        path.write_text(json.dumps(fixture))
    async def gateway(request):
        service,method=request.url.path.strip('/').split('/')[-2:]
        spec=rpc.methods(DataApi if service=='data' else NetApi)[method]
        args=spec.decode_args(json.loads(request.content))
        if service=='net':
            fixture['paced']+=1;result=None
        elif method=='get':
            key=args['scope']+'/'+args['key'];result=StoredValue(found=key in fixture['data'],value=fixture['data'].get(key))
        elif method=='set':
            assert args['secret'] is True
            assert len(json.dumps(args['value'],ensure_ascii=False).encode())<=65536
            fixture['data'][args['scope']+'/'+args['key']]=args['value'];result=None
        else:raise AssertionError(method)
        persist()
        return httpx.Response(200,content=json.dumps(spec.encode_result(result)),headers={"content-type":"application/json"})
    async def provider_http(request):
        response=await fake.handle(request)
        persist()
        if action=='crash_put' and request.method=='PUT':os._exit(75)
        if action=='crash_merge' and request.url.path.endswith('upload_complete/v2'):os._exit(76)
        return response
    runner=Runner()
    runner.host=rpc.RpcClient(httpx.AsyncClient(transport=httpx.MockTransport(gateway),base_url='http://fake-host.test'),'/services')
    ctx=RemoteContext(runner,'cloud-123-personal','offline worker',None,('plugin-data',));runner.ctx=ctx
    backend=Personal123(net._Paced(httpx.MockTransport(provider_http)),store=ctx.use(PLUGIN_DATA).scoped(ctx))
    ctx.contribute(dv.DRIVE_BACKENDS,'123-personal',backend);ctx.effect(backend.aclose)
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.asgi),base_url='http://sdk.test')
    remote=RemoteDriveBackend(HttpBridge(http),'123-personal',dv.backend_spec(backend))
    account=dv.DriveAccount('17',{'token':fake.token})
    body=b'abcdefghij';fp=fingerprint(body)
    try:
        if action=='scan':
            fake.row(101,'sub',directory=True);fake.row(102,'empty',parent=101,directory=True)
            fake.row(103,'note.txt',1,parent=101,content=b'x')
            result=await remote.list_tree(account,'0')
            output={'dirs':list(result.dir_ids),'paths':[f.rel_path for f in result.files]}
        elif action=='blocked':
            try:await remote.begin_upload(account,'','process.bin',fp,None,None)
            except dv.ApiChanged:output={'blocked':True}
            else:raise AssertionError('uncertain merge accepted')
        else:
            step=await remote.begin_upload(account,'','process.bin',fp,None,None)
            if action=='complete_again':
                assert step.file is not None;output={'file':step.file.remote_id,'size':step.file.size}
            else:
                assert step.file is None
                state=step.state;start=step.next_part
                if action in ('first','crash_put'):
                    data=body[start*step.part_size:(start+1)*step.part_size]
                    state=await remote.upload_part(account,state,start,data)
                else:
                    total=(len(body)+step.part_size-1)//step.part_size
                    for i in range(start,total):
                        state=await remote.upload_part(account,state,i,body[i*step.part_size:(i+1)*step.part_size])
                    result=await remote.finish_upload(account,state)
                    assert result.size==len(body)
                output={'start':start,'next':state['next']}
        assert fake.token not in json.dumps(fixture)
        print(json.dumps({'pid':os.getpid(),**output}))
    finally:
        await ctx.dispose();await http.aclose();await runner.host.aclose()

if __name__=='__main__':asyncio.run(run(Path(sys.argv[1]),sys.argv[2]))
