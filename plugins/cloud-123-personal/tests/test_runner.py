"""Real SDK Runner ASGI and host proxy; in-process HTTP bridge, simulated 123 only."""
import secrets
import unittest
import httpx
from movieclaw_sdk import drives as dv
from movieclaw_sdk.runner import Runner, RemoteContext
from movieclaw_api.services.plugin_drives import RemoteDriveBackend
from cloud_123_personal.backend import Personal123
from tests.fake123 import Fake123
from tests.store import MemoryStore
from tests.test_backend import fingerprint


class HttpBridge:
    """HTTP harness, not a replacement SDK contract or production adapter."""
    entry_id='cloud-123-personal'
    def __init__(self, client):self.client=client
    async def call(self,cid,payload,timeout=None,kind=None):
        r=await self.client.post(f'/_mc/{kind}/{cid}',json={'payload':payload})
        r.raise_for_status()
        assert r.json()['ok']
        return r.json()['result']


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake=Fake123();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
        self.runner=Runner()
        self.ctx=RemoteContext(self.runner,'cloud-123-personal','test',None,())
        self.runner.ctx=self.ctx
        self.ctx.contribute(dv.DRIVE_BACKENDS,'123-personal',self.backend)
        self.ctx.effect(self.backend.aclose)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.runner.asgi),base_url='http://sdk.test')
        self.remote=RemoteDriveBackend(HttpBridge(self.http),'123-personal',dv.backend_spec(self.backend))
        self.account=dv.DriveAccount('17',{'token':self.fake.token})

    async def asyncTearDown(self):
        await self.ctx.dispose();await self.http.aclose()

    async def test_form_upload_stat_link_delete_through_sdk_wire(self):
        phone='1'+''.join(str(secrets.randbelow(10)) for _ in range(10))
        result=await self.remote.login_with_form({'phone':phone,'password':secrets.token_urlsafe(24)})
        account=dv.DriveAccount(result.user_id,result.credentials)
        step=await self.remote.begin_upload(account,'','wire.bin',fingerprint(b'xyz'),None,None)
        state=await self.remote.upload_part(account,step.state,0,b'xyz')
        remote=await self.remote.finish_upload(account,state)
        self.assertEqual((await self.remote.stat(account,remote.remote_id)).size,3)
        self.assertEqual((await self.remote.direct_link(account,remote.remote_id,'wire-test')).expires_in,300)
        await self.remote.delete(account,[remote.remote_id])
        with self.assertRaises(dv.NotFound):await self.remote.stat(account,remote.remote_id)
        self.assertEqual(self.runner._call_tasks,{})

    async def test_directory_picker_remote_labels_through_real_sdk_wire(self):
        self.fake.row(1,'电影:作品? | "版"',directory=True)
        row=self.fake.row(2,'irrelevant',1,content=b'x');row.pop('FileName')
        result=await self.remote.list_dir(self.account,'')
        self.assertEqual([(r.remote_id,r.name) for r in result],[('1','电影:作品? | "版"')])
        self.assertEqual(await self.remote.list_dir(self.account,'1'),[])

    async def test_scan_failure_survives_wire(self):
        self.fake.error=(200,{'code':0,'data':{'InfoList':[]}})
        with self.assertRaises(dv.ApiChanged):await self.remote.list_tree(self.account,'0')
        self.assertEqual(len(self.fake.calls),1)

    async def test_refresh_credentials_survive_wire_even_after_failure(self):
        self.account.credentials.update(expires_at='1',phone=str(secrets.randbelow(10**11)),password=secrets.token_urlsafe(24))
        old=self.account.credentials
        with self.assertRaises(dv.NotFound):await self.remote.stat(self.account,'99999')
        self.assertIsNot(old,self.account.credentials)
        self.assertEqual(self.account.credentials['token'],self.fake.token)

    async def test_dispose_runs_registered_cleanup(self):
        await self.ctx.dispose()
        self.assertTrue(self.fake.closed)

    async def test_sdk_paces_every_provider_request(self):
        from movieclaw_sdk import net
        class Pacer:
            count=0
            async def pace(self):
                self.count+=1
                return None
        pacer=Pacer();self.runner._services['net']=pacer
        await self.backend.aclose()
        # SDK's own transport wrapper, only inner service transport is fake.
        self.backend=Personal123(net._Paced(self.fake.transport()),store=MemoryStore())
        self.ctx._drives['123-personal']=self.backend
        self.ctx.effect(self.backend.aclose)
        for n in range(1,202):self.fake.row(n,f'dir{n}',directory=True)
        rows=await self.remote.list_dir(self.account,'')
        self.assertEqual(len(rows),201)
        self.assertEqual(pacer.count,3)

    async def test_tree_scan_uses_sdk_budget_for_each_child_request(self):
        from movieclaw_sdk import net
        class Pacer:
            count=0
            async def pace(self):
                self.count+=1
                return None
        pacer=Pacer();self.runner._services['net']=pacer
        await self.backend.aclose();self.backend=Personal123(net._Paced(self.fake.transport()),store=MemoryStore())
        self.ctx._drives['123-personal']=self.backend;self.ctx.effect(self.backend.aclose)
        self.fake.row(1,'sub',directory=True);self.fake.row(2,'x.txt',1,parent=1,content=b'x')
        tree=await self.remote.list_tree(self.account,'0')
        self.assertEqual([f.rel_path for f in tree.files],['sub/x.txt'])
        self.assertEqual(pacer.count,3) # root page, directory stat, directory page

    async def test_tree_scan_budget_failure_is_not_partial_success(self):
        from movieclaw_sdk import net
        class Pacer:
            count=0
            async def pace(self):
                self.count+=1
                return dv.error_dict(dv.RateLimited('test budget')) if self.count==3 else None
        pacer=Pacer();self.runner._services['net']=pacer
        await self.backend.aclose();self.backend=Personal123(net._Paced(self.fake.transport()),store=MemoryStore())
        self.ctx._drives['123-personal']=self.backend;self.ctx.effect(self.backend.aclose)
        self.fake.row(1,'sub',directory=True);self.fake.row(2,'root.txt',1,content=b'x')
        with self.assertRaises(dv.RateLimited):await self.remote.list_tree(self.account,'0')
        self.assertEqual(len(self.fake.calls),2)

    async def test_sdk_budget_failure_does_not_return_partial_directory(self):
        from movieclaw_sdk import net
        class Pacer:
            count=0
            async def pace(self):
                self.count+=1
                return dv.error_dict(dv.RateLimited('test budget')) if self.count==2 else None
        pacer=Pacer();self.runner._services['net']=pacer
        await self.backend.aclose()
        self.backend=Personal123(net._Paced(self.fake.transport()),store=MemoryStore())
        self.ctx._drives['123-personal']=self.backend;self.ctx.effect(self.backend.aclose)
        for n in range(1,202):self.fake.row(n,f'dir{n}',directory=True)
        with self.assertRaises(dv.RateLimited):await self.remote.list_dir(self.account,'')
        self.assertEqual(len(self.fake.calls),1)
