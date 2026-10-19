import unittest
import copy
from test_personal import personal, FakeDiscovery, FakePrivateSDK

class ResumeWallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service=personal.PersonalRecommendations(FakeDiscovery(), private_store=FakePrivateSDK())
        self.token=(await self.service.create_profile())["profile_token"]
        self.scope=self.service._scope(self.token)
    async def test_resume_same_account_is_read_only_and_not_new_exposure(self):
        self.assertEqual((await self.service.resume_wall(self.token,limit=12))["items"],[])
        shown=await self.service.recommend(self.token,"shown",limit=12)
        before=copy.deepcopy(await self.service._profile(self.scope))
        calls=len(self.service.discovery.calls)
        resumed=await self.service.resume_wall(self.token,limit=12)
        self.assertEqual([i["detail_ref"] for i in resumed["items"]],[i["detail_ref"] for i in shown["items"]])
        self.assertTrue(resumed["cache"]["resume"])
        self.assertTrue(all(not i["metadata"]["is_new_to_recent"] for i in resumed["items"]))
        self.assertEqual(await self.service._profile(self.scope),before)
        self.assertEqual(len(self.service.discovery.calls),calls)
        other=(await self.service.create_profile())["profile_token"]
        self.assertEqual((await self.service.resume_wall(other,limit=12))["items"],[])
    async def test_changed_feedback_filters_size_and_questionnaire_cannot_restore_old_wall(self):
        for action in ["watch","hide","save","like"]:
            shown=await self.service.recommend(self.token,action,limit=12)
            await self.service.feedback(self.token,action,shown["items"][0])
            self.assertEqual((await self.service.resume_wall(self.token,limit=12))["items"],[])
        await self.service.recommend(self.token,"new",limit=12,media_type="tv",era="classic")
        self.assertEqual((await self.service.resume_wall(self.token,limit=12))["items"],[])
        self.assertEqual((await self.service.resume_wall(self.token,limit=24,media_type="tv",era="classic"))["items"],[])
        self.assertEqual(len((await self.service.resume_wall(self.token,limit=12,media_type="tv",era="classic"))["items"]),12)
        await self.service.submit(self.token,copy.deepcopy(personal.DEFAULT_ANSWERS))
        self.assertEqual((await self.service.resume_wall(self.token,limit=12,media_type="tv",era="classic"))["items"],[])
