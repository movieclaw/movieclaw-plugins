import asyncio
import unittest
import httpx
from movieclaw_sdk import drives as dv
from cloud_123_personal.backend import Personal123
from tests.fake123 import Fake123


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake=Fake123();self.backend=Personal123(self.fake.transport())
        self.account=dv.DriveAccount('17',{'token':self.fake.token})
    async def asyncTearDown(self):await self.backend.aclose()

    async def test_nested_all_files_empty_directories_and_relative_paths(self):
        self.fake.row(1,'root',directory=True)
        self.fake.row(2,'sub',parent=1,directory=True)
        self.fake.row(3,'empty',parent=2,directory=True)
        self.fake.row(4,'notes.txt',1,parent=1,content=b'x')
        self.fake.row(5,'film.mkv',2,parent=2,content=b'xx')
        self.fake.row(6,'outside.txt',1,content=b'x')
        tree=await self.backend.list_tree(self.account,'1')
        self.assertEqual(tree.dir_ids,('2','3'))
        self.assertEqual([(f.remote_id,f.rel_path,f.parent_id,f.sha1) for f in tree.files],
                         [('4','notes.txt','1',None),('5','sub/film.mkv','2',None)])

    async def test_scan_pages_forward_real_next_cursor(self):
        for n in range(1,206):self.fake.row(n,f'{n}.txt',1,content=b'x')
        requests=[]
        async def handler(request):
            if request.url.path.endswith('/file/list'):requests.append(dict(request.url.params))
            return await self.fake.handle(request)
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        tree=await self.backend.list_tree(self.account,'0')
        self.assertEqual(len(tree.files),205)
        self.assertEqual([(r['page'],r['next']) for r in requests],[('1','0'),('2','100'),('3','200')])

    async def bad_listing(self, transform, expected=dv.ApiChanged):
        self.fake.row(1,'x',1,content=b'x')
        async def handler(request):
            response=await self.fake.handle(request)
            if request.url.path.endswith('/file/list'):
                obj=response.json();transform(obj['data']);return httpx.Response(200,json=obj)
            return response
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        with self.assertRaises(expected):await self.backend.list_tree(self.account,'0')

    async def test_cycle_to_root(self):
        await self.bad_listing(lambda d:d['InfoList'][0].update(FileId=0),dv.Transient)
    async def test_wrong_parent(self):
        await self.bad_listing(lambda d:d['InfoList'][0].update(ParentFileId=777),dv.Transient)
    async def test_duplicate_id(self):
        await self.bad_listing(lambda d:d['InfoList'].append(dict(d['InfoList'][0])),dv.Transient)
    async def test_duplicate_path(self):
        await self.bad_listing(lambda d:d['InfoList'].append({**d['InfoList'][0],'FileId':2}),dv.Conflict)
    async def test_unsafe_relative_name(self):
        await self.bad_listing(lambda d:d['InfoList'][0].update(FileName='../escape'))
    async def test_unknown_type(self):
        await self.bad_listing(lambda d:d['InfoList'][0].update(Type=7))
    async def test_missing_parent(self):
        await self.bad_listing(lambda d:d['InfoList'][0].pop('ParentFileId'))
    async def test_repeated_cursor(self):
        await self.bad_listing(lambda d:d.update(Next='0'))
    async def test_empty_nonterminal_page(self):
        await self.bad_listing(lambda d:d.update(InfoList=[],Next='10'))
    async def test_bad_cursor_type(self):
        await self.bad_listing(lambda d:d.update(Next=False))

    async def test_failure_in_second_directory_does_not_return_partial_tree(self):
        self.fake.row(1,'sub',directory=True);self.fake.row(2,'root.txt',1,content=b'x')
        async def handler(request):
            if request.url.path.endswith('/file/list') and request.url.params['parentfileid']=='1':
                raise httpx.ReadError('synthetic interrupted scan')
            return await self.fake.handle(request)
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        with self.assertRaises(dv.Transient):await self.backend.list_tree(self.account,'0')

    async def test_directory_rename_between_pages_is_detected(self):
        self.fake.row(1,'sub',directory=True)
        async def handler(request):
            response=await self.fake.handle(request)
            if request.url.path.endswith('/file/info'):
                obj=response.json();obj['data']['infoList'][0]['FileName']='renamed'
                return httpx.Response(200,json=obj)
            return response
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        with self.assertRaises(dv.Transient):await self.backend.list_tree(self.account,'0')

    async def test_duplicate_id_across_directories_rejected(self):
        self.fake.row(1,'sub',directory=True);self.fake.row(2,'root.txt',1,content=b'x')
        self.fake.row(3,'child.txt',1,parent=1,content=b'x')
        async def handler(request):
            response=await self.fake.handle(request)
            if request.url.path.endswith('/file/list') and request.url.params['parentfileid']=='1':
                obj=response.json();obj['data']['InfoList'][0]['FileId']=2
                return httpx.Response(200,json=obj)
            return response
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        with self.assertRaises(dv.Transient):await self.backend.list_tree(self.account,'0')

    async def test_resource_limits_fail_without_partial_return(self):
        self.fake.row(1,'sub',directory=True);self.fake.row(2,'child',parent=1,directory=True)
        for attr,value in [('MAX_SCAN_ENTRIES',1),('MAX_SCAN_PAGES',1),('MAX_DEPTH',1),('MAX_PATH',2)]:
            original=getattr(self.backend,attr);setattr(self.backend,attr,value)
            try:
                with self.assertRaises(dv.ApiChanged):await self.backend.list_tree(self.account,'0')
            finally:setattr(self.backend,attr,original)

    async def test_cancel_mid_scan_propagates(self):
        self.fake.row(1,'sub',directory=True)
        async def handler(request):
            if request.url.path.endswith('/file/list') and request.url.params['parentfileid']=='1':raise asyncio.CancelledError
            return await self.fake.handle(request)
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler))
        with self.assertRaises(asyncio.CancelledError):await self.backend.list_tree(self.account,'0')
