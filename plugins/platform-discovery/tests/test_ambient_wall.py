"""Only displayed screensaver patches consume bounded, account-scoped candidates."""
import copy
import time
from test_next_wall import NextWallTests
from test_personal import personal

class AmbientWallTests(NextWallTests):
    async def setup_rotation(self, count=3):
        current = await self.service.recommend(self.token, 'ambient-current', limit=12)
        faces = []
        for n in range(3):
            faces.append(await self.service.prepare_wall(self.token, nonce='ambient-next'+str(n), limit=12))
        rotation = dict(ticket=faces[0]['prepared_ticket'], operation='unique-rotation',
            current=[i['detail_ref'] for i in current['items']],
            replace={current['items'][i]['detail_ref']:faces[0]['items'][i]['detail_ref'] for i in range(count)})
        return current, faces, rotation

    async def test_preview_no_exposure_commit_only_used_and_manual_reuses_leftovers(self):
        current, faces, rotation = await self.setup_rotation()
        before = await self.profile()
        stage = copy.deepcopy(await self.service._read(self.scope, 'next-wall'))
        preview = await self.service.rotate_wall(self.token, rotation, preview=True)
        self.assertEqual(before, await self.profile())
        self.assertEqual(stage, await self.service._read(self.scope, 'next-wall'))
        shown = await self.service.rotate_wall(self.token, rotation)
        self.assertEqual(shown['items'], preview['items'])
        after = await self.profile()
        self.assertEqual(set(after['recent'][-1]), {i['id'] for i in shown['items']})
        self.assertFalse(set(i['id'] for i in faces[0]['items'][3:]) & set(after['recent'][-1]))
        self.assertEqual(sum(len(f['items']) for f in shown['prepared_queue']),33)
        self.assertEqual(shown, await self.service.rotate_wall(self.token, rotation))
        self.assertEqual(after, await self.profile(), 'idempotent acknowledgement cannot add exposure')
        manual = await self.service.commit_wall(self.token, faces[0]['prepared_ticket'])
        self.assertEqual(manual['items'], faces[0]['items'][3:]+faces[1]['items'][:3])
        self.assertEqual(len(manual['items']),12)
        self.assertFalse(set(i['detail_ref'] for i in manual['items']) & set(rotation['replace'].values()))
        self.assertEqual(sum(len(f['items']) for f in manual['prepared_queue']),21)
        self.assertEqual(manual, await self.service.commit_wall(self.token, faces[0]['prepared_ticket']))

    async def test_conflicts_account_watch_expiry_and_invalid_refs(self):
        current, faces, rotation = await self.setup_rotation(1)
        other = (await self.service.create_profile())['profile_token']
        with self.assertRaises(personal.PersonalProfileError): await self.service.rotate_wall(other, rotation)
        for changed in (dict(rotation, current=rotation['current']+rotation['current'][:1]),
                        dict(rotation, replace={rotation['current'][0]:'tmdb:movie:999999999'}),
                        dict(rotation, replace={rotation['current'][0]:rotation['current'][1]}),
                        dict(rotation, operation='bad/op')):
            with self.assertRaises(personal.PersonalProfileError): await self.service.rotate_wall(self.token,changed,preview=True)
        await self.service.feedback(self.token,'watch',faces[0]['items'][0])
        with self.assertRaises(personal.PersonalProfileError): await self.service.rotate_wall(self.token,rotation)
        current, faces, rotation = await self.setup_rotation(1)
        stage = await self.service._read(self.scope,'next-wall');stage['queue'][0]['expires']=time.time()-1
        await self.service._write(self.scope,'next-wall',stage)
        with self.assertRaises(personal.PersonalProfileError): await self.service.rotate_wall(self.token,rotation,preview=True)

    async def test_long_running_small_patches_stay_bounded_and_can_resume(self):
        current, faces, rotation = await self.setup_rotation(1)
        for n in range(15):
            stage=await self.service._read(self.scope,'next-wall')
            if not stage['queue']:
                await self.service.prepare_wall(self.token,nonce='refill'+str(n),limit=12)
                stage=await self.service._read(self.scope,'next-wall')
            candidate=stage['queue'][0]['result']['items'][0]
            rotation=dict(ticket=stage['queue'][0]['ticket'],operation='patch-'+str(n),
                current=[i['detail_ref'] for i in current['items']],
                replace={current['items'][n%12]['detail_ref']:candidate['detail_ref']})
            current=await self.service.rotate_wall(self.token,rotation)
            stage=await self.service._read(self.scope,'next-wall')
            self.assertLessEqual(len(stage['queue']),3)
            self.assertLessEqual(sum(len(f['result']['items']) for f in stage['queue']),36)
        resumed=await self.service.resume_wall(self.token,limit=12)
        self.assertEqual([i['detail_ref'] for i in resumed['items']],[i['detail_ref'] for i in current['items']])
        profile=await self.profile()
        self.assertLessEqual(len(profile['batches']),personal.MAX_BATCHES)
        self.assertLessEqual(len(profile['recent']),personal.RECENT_BATCHES)
