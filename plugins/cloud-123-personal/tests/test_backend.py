import asyncio
import hashlib
import json
import secrets
import unittest

import httpx
from movieclaw_sdk import drives as dv
from cloud_123_personal.backend import Personal123, Credentials
from cloud_123_personal.client import checked, secure_url
from tests.fake123 import Fake123
from tests.store import MemoryStore


def fingerprint(data):
    return dv.Fingerprint(len(data),hashlib.sha1(data).hexdigest().upper(),hashlib.md5(data).hexdigest().upper(),hashlib.sha1(data[:131072]).hexdigest().upper())


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = Fake123()
        self.backend = Personal123(self.fake.transport(),store=MemoryStore())
        self.account = dv.DriveAccount('17',{'token':self.fake.token})

    async def asyncTearDown(self):
        await self.backend.aclose()

    async def test_login_uses_real_library_request_and_identity(self):
        phone='1'+''.join(str(secrets.randbelow(10)) for _ in range(10))
        password=secrets.token_urlsafe(32)
        result=await self.backend.login_with_form({'phone':phone,'password':password})
        self.assertEqual(result.user_id,'17')
        self.assertEqual(result.credentials['token'],self.fake.token)
        self.assertEqual([x[0] for x in self.fake.calls],['/api/user/sign_in','/api/user/info'])
        self.assertEqual(self.fake.calls[0][1]['passport'],phone)
        self.assertEqual(self.fake.calls[0][1]['password'],password)
        self.assertNotIn(password,repr(Credentials(phone=phone,password=password)))

    async def test_invalid_form_never_leaks_inputs(self):
        value=secrets.token_urlsafe(32)
        with self.assertRaises(ValueError) as exc:
            await self.backend.login_with_form({'phone':value,'password':value})
        self.assertNotIn(value,str(exc.exception))
        self.assertEqual(self.fake.calls,[])

    async def test_refresh_assigns_credentials(self):
        self.account.credentials.update(expires_at='1',phone=str(secrets.randbelow(10**11)),password=secrets.token_urlsafe(32))
        old=self.account.credentials
        await self.backend.account_info(self.account)
        self.assertIsNot(old,self.account.credentials)
        self.assertEqual(self.account.credentials['token'],self.fake.token)

    async def test_refresh_identity_mismatch_preserves_credentials(self):
        self.account.user_id='18'
        self.account.credentials.update(expires_at='1',phone=str(secrets.randbelow(10**11)),password=secrets.token_urlsafe(32))
        old=dict(self.account.credentials)
        with self.assertRaises(dv.AuthExpired):await self.backend.account_info(self.account)
        self.assertEqual(old,self.account.credentials)

    async def test_unexpired_no_login(self):
        self.account.credentials['expires_at']='9999999999'
        await self.backend.account_info(self.account)
        self.assertEqual(len(self.fake.calls),1)

    async def test_empty_tree_is_an_actual_complete_listing(self):
        tree=await self.backend.list_tree(self.account,'0')
        self.assertEqual((tree.files,tree.dir_ids),((),()))
        self.assertEqual([p for p,_,_ in self.fake.calls],['/api/file/list'])

    async def test_direct_link_300_seconds_and_ua(self):
        self.fake.row(1,'video.mkv',4,content=b'abcd')
        link=await self.backend.direct_link(self.account,'1','test-player')
        self.assertEqual(link.expires_in,300)
        self.assertEqual(self.fake.calls[-1][2]['user-agent'],'test-player')
        self.assertEqual(self.fake.calls[-1][2]['platform'],'android')
        self.assertTrue(link.url.startswith('https://'))

    async def test_stat_never_invents_sha1_from_md5(self):
        self.fake.row(1,'video.mkv',4,content=b'abcd')
        self.assertIsNone((await self.backend.stat(self.account,'1')).sha1)
        self.assertFalse(self.backend.capabilities.list_hashes)

    async def test_missing_stat(self):
        with self.assertRaises(dv.NotFound):await self.backend.stat(self.account,'404')

    async def test_directory_pagination_and_nonvideo(self):
        for n in range(1,205):self.fake.row(n,f'dir{n}',directory=True)
        self.fake.row(205,'notes.txt',1,content=b'a')
        rows=await self.backend.list_dir(self.account,'')
        self.assertEqual(len(rows),204)
        self.assertEqual(len(self.fake.calls),3)

    async def test_ensure_path_reuses_and_empty_preserves(self):
        self.fake.row(1,'a',directory=True)
        self.assertEqual(await self.backend.ensure_dir(self.account,'',''),'')
        result=await self.backend.ensure_dir(self.account,'','a/b')
        self.assertEqual(self.fake.rows[result]['FileName'],'b')
        self.assertEqual(self.fake.rows[result]['ParentFileId'],1)

    async def test_directory_collision(self):
        self.fake.row(1,'a',1,content=b'a')
        with self.assertRaises(dv.Conflict):await self.backend.ensure_dir(self.account,'','a')

    async def test_bad_path_no_write(self):
        for path in ('../a','a//b','/a','a/','a/..'):
            with self.assertRaises(ValueError):await self.backend.ensure_dir(self.account,'',path)
        self.assertFalse(self.fake.calls)

    async def test_delete_recycles_and_is_idempotent(self):
        self.fake.row(1,'a',1,content=b'a')
        await self.backend.delete(self.account,['1','1','2'])
        self.assertFalse(self.fake.rows)
        self.assertEqual(sum(p=='/api/file/trash' for p,_,_ in self.fake.calls),1)
        self.assertFalse(any(p.endswith('/delete') for p,_,_ in self.fake.calls))

    async def test_same_name_metadata_needs_reuse_ack(self):
        self.fake.row(1,'a',3,content=b'abc')
        with self.assertRaises(dv.ApiChanged):
            await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)

    async def test_same_size_different_md5_conflicts(self):
        self.fake.row(1,'a',3,content=b'xyz')
        with self.assertRaises(dv.Conflict):await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)

    async def test_single_upload_and_wire_roundtrip(self):
        body=b'abc'
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(body),None,None)
        state=dv.upload_step_from(json.loads(json.dumps(dv.upload_step_dict(step)))).state
        state=await self.backend.upload_part(self.account,state,0,body)
        result=await self.backend.finish_upload(self.account,state)
        self.assertEqual((result.size,result.sha1),(3,None))
        put=next(x for x in self.fake.calls if x[0].startswith('/part/'))
        self.assertNotIn('authorization',put[2])
        self.assertNotIn(self.fake.token,json.dumps(state))
        self.assertFalse(any('secret' in k.lower() or 'token' in k.lower() or 'url' in k.lower() for k in state['upload']))
        self.assertFalse(self.fake.calls[-2][1]['ismultipart'])

    async def test_multipart_uninterrupted(self):
        body=b'abcdefghij';fp=fingerprint(body)
        step=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        state=step.state
        for index,part in enumerate((body[:4],body[4:8],body[8:])):
            state=await self.backend.upload_part(self.account,state,index,part)
        result=await self.backend.finish_upload(self.account,state)
        self.assertEqual(result.size,10)
        complete=next(p for path,p,_ in self.fake.calls if path.endswith('upload_complete/v2'))
        self.assertEqual(complete['filesize'],10)
        self.assertTrue(complete['ismultipart'])

    async def test_restart_never_guesses_progress_or_completion(self):
        for body in (b'abc',b'abcdefgh'):
            await self.backend.aclose();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
            step=await self.backend.begin_upload(self.account,'','a',fingerprint(body),None,None)
            await self.backend.upload_part(self.account,step.state,0,body[:4])
            await self.backend.aclose();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
            before=len(self.fake.calls)
            with self.assertRaises(dv.ApiChanged):
                await self.backend.begin_upload(self.account,'','a',fingerprint(body),step.state,None)
            with self.assertRaises(dv.ApiChanged):
                await self.backend.upload_part(self.account,step.state,0,body[:4])
            with self.assertRaises(dv.ApiChanged):
                await self.backend.finish_upload(self.account,{**step.state,'next':1})
            self.assertEqual(len(self.fake.calls),before)

    async def test_cross_account_and_file_resume_rejected(self):
        fp=fingerprint(b'abcdefgh')
        step=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        with self.assertRaises(dv.Conflict):await self.backend.begin_upload(dv.DriveAccount('18',self.account.credentials),'','a',fp,step.state,None)
        with self.assertRaises(dv.Conflict):await self.backend.begin_upload(self.account,'','b',fp,step.state,None)

    async def test_wrong_part_and_premature_finish(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abcdefgh'),None,None)
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,step.state,1,b'abcd')
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,step.state,0,b'abc')
        with self.assertRaises(dv.Conflict):await self.backend.finish_upload(self.account,step.state)

    async def test_storage_403_does_not_expire_account_or_retry(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        self.fake.put_error=403
        with self.assertRaises(dv.Transient):await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.assertEqual(sum(p.startswith('/part/') for p,_,_ in self.fake.calls),1)
        self.assertEqual(step.state['next'],0)

    async def test_resume_uses_durable_ack_not_remote_candidate_shape(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abcdefgh'),None,None)
        await self.backend.upload_part(self.account,step.state,0,b'abcd')
        before=len(self.fake.calls)
        resumed=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abcdefgh'),step.state,None)
        self.assertEqual(resumed.next_part,1)
        self.assertEqual(len(self.fake.calls),before)

    async def test_duplicate_acknowledged_part_and_finish_do_not_resend(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        before=len(self.fake.calls)
        self.assertEqual(await self.backend.upload_part(self.account,step.state,0,b'abc'),state)
        self.assertEqual(len(self.fake.calls),before)
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,step.state,0,b'xyz')
        first=await self.backend.finish_upload(self.account,state)
        before=len(self.fake.calls)
        self.assertEqual(await self.backend.finish_upload(self.account,state),first)
        self.assertEqual([p for p,_,_ in self.fake.calls[before:]],['/api/file/info'])

    async def test_forged_next_cannot_skip_unacknowledged_parts(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abcdefgh'),None,None)
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,{**step.state,'next':1},1,b'efgh')
        with self.assertRaises(dv.Conflict):await self.backend.finish_upload(self.account,{**step.state,'next':2})

    async def test_lost_put_response_retries_identical_bytes(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        self.fake.lose_put=True
        with self.assertRaises(dv.Transient):await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.assertEqual(self.fake.parts[1],b'abc')
        with self.assertRaises(dv.Conflict):await self.backend.upload_part(self.account,step.state,0,b'xyz')
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.assertEqual((await self.backend.finish_upload(self.account,state)).size,3)

    async def test_lost_complete_response_never_replayed_or_metadata_assumed(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.lose_complete=True
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        self.assertIn('999',self.fake.rows)  # service committed, acknowledgement was lost
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.backend.finish_upload(self.account,state)
        self.assertEqual(len(self.fake.calls),before)

    async def test_complete_ack_then_stat_failure_retries_only_stat(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.lose_stat=True
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        self.assertEqual((await self.backend.finish_upload(self.account,state)).size,3)
        self.assertEqual(sum(p.endswith('upload_complete/v2') for p,_,_ in self.fake.calls),1)

    async def test_complete_ack_wrong_parent_not_success(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.complete_parent=7
        with self.assertRaises(dv.Conflict):await self.backend.finish_upload(self.account,state)

    async def test_malformed_complete_ack_blocks_replay(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.complete_response={'code':0,'data':[]}
        with self.assertRaises(dv.ApiChanged):await self.backend.finish_upload(self.account,state)
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.backend.finish_upload(self.account,state)
        self.assertEqual(len(self.fake.calls),before)

    async def test_lost_init_response_prevents_second_initialization(self):
        self.fake.lose_init=True
        with self.assertRaises(dv.Transient):
            await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        with self.assertRaises(dv.ApiChanged):
            await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)
        await self.backend.aclose();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
        with self.assertRaises(dv.ApiChanged):
            await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        self.assertFalse(self.fake.parts)

    async def test_cancelled_complete_remains_uncertain(self):
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.cancel_complete=True
        with self.assertRaises(asyncio.CancelledError):await self.backend.finish_upload(self.account,state)
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.backend.finish_upload(self.account,state)
        self.assertEqual(len(self.fake.calls),before)

    async def test_server_confirmed_reuse_requires_remote_md5_and_parent(self):
        self.fake.row(1,'a',3,content=b'abc');self.fake.reuse_id=1
        step=await self.backend.begin_upload(self.account,'','a',fingerprint(b'abc'),None,None)
        self.assertEqual(step.file.remote_id,'1')
        self.fake.row(2,'b',3,parent=7,content=b'abc');self.fake.reuse_id=2
        with self.assertRaises(dv.Conflict):
            await self.backend.begin_upload(self.account,'','b',fingerprint(b'abc'),None,None)

    async def _complete_small_upload(self):
        fp=fingerprint(b'abc')
        step=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        result=await self.backend.finish_upload(self.account,state)
        return fp,state,result

    async def test_begin_after_confirmed_upload_revalidates_and_reuses(self):
        fp,state,result=await self._complete_small_upload()
        for _ in range(2):
            before=len(self.fake.calls)
            again=await self.backend.begin_upload(self.account,'','a',fp,None,None)
            self.assertEqual(again.file,result)
            self.assertEqual([p for p,_,_ in self.fake.calls[before:]],['/api/file/info'])
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)

    async def test_begin_after_confirmed_rapid_upload_revalidates_and_reuses(self):
        self.fake.row(1,'a',3,content=b'abc');self.fake.reuse_id=1
        fp=fingerprint(b'abc')
        first=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        before=len(self.fake.calls)
        again=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        self.assertEqual(again.file,first.file)
        self.assertEqual([p for p,_,_ in self.fake.calls[before:]],['/api/file/info'])

    async def test_confirmed_reuse_rejects_remote_name_parent_size_md5_or_type_changes(self):
        fp,_,_=await self._complete_small_upload()
        original=dict(self.fake.rows['999'])
        for field,value in [('FileName','changed'),('ParentFileId',777),('Size',4),('Etag','0'*32),('Type',1)]:
            with self.subTest(field=field):
                self.fake.rows['999']={**original,field:value}
                before=len(self.fake.calls)
                with self.assertRaises(dv.Conflict):
                    await self.backend.begin_upload(self.account,'','a',fp,None,None)
                self.assertEqual([p for p,_,_ in self.fake.calls[before:]],['/api/file/info'])
        self.fake.rows['999']=original
        self.assertEqual((await self.backend.begin_upload(self.account,'','a',fp,None,None)).file.remote_id,'999')

    async def test_confirmed_reuse_does_not_hide_deleted_or_trashed_file(self):
        fp,_,_=await self._complete_small_upload()
        self.fake.rows['999']['Trashed']=True
        with self.assertRaises(dv.NotFound):await self.backend.begin_upload(self.account,'','a',fp,None,None)
        self.fake.rows.pop('999')
        with self.assertRaises(dv.NotFound):await self.backend.begin_upload(self.account,'','a',fp,None,None)
        self.assertEqual(sum(p.endswith('upload_request') for p,_,_ in self.fake.calls),1)

    async def test_begin_after_uncertain_complete_still_refuses_without_probe_or_init(self):
        fp=fingerprint(b'abc')
        step=await self.backend.begin_upload(self.account,'','a',fp,None,None)
        state=await self.backend.upload_part(self.account,step.state,0,b'abc')
        self.fake.lose_complete=True
        with self.assertRaises(dv.Transient):await self.backend.finish_upload(self.account,state)
        before=len(self.fake.calls)
        with self.assertRaises(dv.ApiChanged):await self.backend.begin_upload(self.account,'','a',fp,None,None)
        self.assertEqual(len(self.fake.calls),before)

    async def test_restart_loses_completion_confirmation_and_cannot_reuse_metadata(self):
        fp,_,_=await self._complete_small_upload()
        await self.backend.aclose();self.backend=Personal123(self.fake.transport(),store=MemoryStore())
        # Simulator returns Reuse=False: matching metadata alone remains insufficient.
        with self.assertRaises(dv.ApiChanged):await self.backend.begin_upload(self.account,'','a',fp,None,None)

    async def test_http_errors_and_no_retry(self):
        for status,expected in ((429,dv.RateLimited),(401,dv.AuthExpired),(403,dv.RiskControl),(500,dv.Transient),(302,dv.ApiChanged)):
            with self.subTest(status=status):
                self.fake.error=(status,{})
                before=len(self.fake.calls)
                with self.assertRaises(expected):await self.backend.account_info(self.account)
                self.assertEqual(len(self.fake.calls),before+1)

    async def test_cancel_propagates(self):
        async def handler(request):raise asyncio.CancelledError
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler),store=MemoryStore())
        with self.assertRaises(asyncio.CancelledError):await self.backend.account_info(self.account)

    async def test_network_exception_redacted(self):
        secret=secrets.token_urlsafe(32)
        async def handler(request):raise httpx.ConnectError(secret)
        await self.backend.aclose();self.backend=Personal123(httpx.MockTransport(handler),store=MemoryStore())
        with self.assertRaises(dv.Transient) as caught:await self.backend.account_info(self.account)
        self.assertNotIn(secret,str(caught.exception))

    async def test_close_releases_transport(self):
        await self.backend.aclose()
        self.assertTrue(self.fake.closed)


class PureTests(unittest.TestCase):
    def test_business_error_classification(self):
        cases=[(401,'',dv.AuthExpired),(429,'',dv.RateLimited),(5066,'',dv.NotFound),(1,'',dv.Transient),(500,'',dv.Transient),(999,'验证码',dv.RiskControl),(999,'容量不足',dv.QuotaExceeded),(999,'同名',dv.Conflict),(999,'',dv.ApiChanged)]
        for code,message,expected in cases:
            with self.subTest(code=code,message=message):
                secret=secrets.token_urlsafe(32)
                with self.assertRaises(expected) as caught:checked({'code':code,'message':message+secret})
                self.assertNotIn(secret,str(caught.exception))

    def test_bad_json_schema(self):
        for value in (None,[],{}, {'code':False,'data':{}},{'code':0,'data':[]}):
            with self.assertRaises(dv.ApiChanged):checked(value)

    def test_unsafe_links(self):
        for url in ('http://example.test/file','file:///tmp/a','https://user:pass@example.test/a',None):
            with self.assertRaises(dv.ApiChanged):secure_url(url)

    def test_real_sdk_spec(self):
        spec=dv.backend_spec(Personal123)
        self.assertEqual(spec['type'],'123-personal')
        for key in ('phone','password'):
            self.assertTrue(spec['login']['form']['properties'][key]['writeOnly'])
        self.assertEqual(spec['capabilities']['rapid_upload_hashes'],['md5'])

class ExtraTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_success_only_allowed_for_acknowledgements(self):
        self.assertEqual(checked({'code':0,'data':None},allow_empty=True),{})
        with self.assertRaises(dv.ApiChanged):checked({'code':0,'data':None})

    async def test_http_debug_does_not_log_secrets_or_signed_urls(self):
        import logging,io
        stream=io.StringIO();handler=logging.StreamHandler(stream)
        logger=logging.getLogger('httpx');old=logger.level
        logger.addHandler(handler);logger.setLevel(logging.DEBUG)
        fake=Fake123();backend=Personal123(fake.transport(),store=MemoryStore())
        try:
            await backend.account_info(dv.DriveAccount('17',{'token':fake.token}))
            self.assertEqual(stream.getvalue(),'')
        finally:
            await backend.aclose();logger.removeHandler(handler);logger.setLevel(old)

    async def test_zero_byte_upload(self):
        fake=Fake123();backend=Personal123(fake.transport(),store=MemoryStore());account=dv.DriveAccount('17',{'token':fake.token})
        try:
            step=await backend.begin_upload(account,'','empty',fingerprint(b''),None,None)
            state=await backend.upload_part(account,step.state,0,b'')
            self.assertEqual((await backend.finish_upload(account,state)).size,0)
        finally:await backend.aclose()

    async def test_visible_pending_metadata_is_not_completion_proof(self):
        fake=Fake123();backend=Personal123(fake.transport(),store=MemoryStore());account=dv.DriveAccount('17',{'token':fake.token})
        try:
            fp=fingerprint(b'abcdefgh')
            step=await backend.begin_upload(account,'','a',fp,None,None)
            fake.row(999,'a',8,content=b'abcdefgh')
            resumed=await backend.begin_upload(account,'','a',fp,step.state,None)
            self.assertIsNone(resumed.file)
            self.assertEqual(resumed.next_part,0)
        finally:await backend.aclose()

    async def test_account_headers_and_cookies_do_not_cross_sessions(self):
        seen=[]
        async def handler(request):
            seen.append(dict(request.headers))
            return httpx.Response(200,json={'code':0,'data':{'UID':17}},headers={'set-cookie':'test-cookie=opaque; Path=/'})
        backend=Personal123(httpx.MockTransport(handler),store=MemoryStore())
        first,second=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        try:
            await backend.account_info(dv.DriveAccount('17',{'token':first}))
            await backend.account_info(dv.DriveAccount('17',{'token':second}))
            self.assertEqual(seen[0]['authorization'],'Bearer '+first)
            self.assertEqual(seen[1]['authorization'],'Bearer '+second)
            self.assertNotIn('cookie',seen[1])
            self.assertFalse(backend.client.http.cookies)
        finally:await backend.aclose()
