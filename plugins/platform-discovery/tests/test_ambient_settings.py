import copy
import unittest
from test_personal import FakePrivateSDK, FakeDiscovery, personal


class AmbientSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakePrivateSDK()
        self.service = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.token = (await self.service.create_profile())["profile_token"]

    async def asyncTearDown(self):
        await self.service.close()

    async def test_persist_isolate_and_do_not_change_recommendation_preferences(self):
        profile_before = await self.service.get_profile(self.token)
        baseline = copy.deepcopy(self.store.values)
        default = await self.service.get_ambient_settings(self.token)
        self.assertEqual(self.store.values, baseline, 'reading defaults never writes')
        settings = {**default['settings'], 'fade_out': 3200, 'gap': 500}
        result = await self.service.set_ambient_settings(self.token, {'settings': settings, 'revision': ''})
        self.assertEqual((await self.service.get_ambient_settings(self.token)), result)
        self.assertEqual(await self.service.get_profile(self.token), profile_before)
        for key, value in baseline.items(): self.assertEqual(self.store.values[key], value)
        other = (await self.service.create_profile())['profile_token']
        self.assertEqual(await self.service.get_ambient_settings(other), default)
        restarted = personal.PersonalRecommendations(FakeDiscovery(), private_store=self.store)
        self.assertEqual(await restarted.get_ambient_settings(self.token), result)
        await restarted.close()
        self.assertTrue(self.store.writes[-1]['secret'])

    async def test_validation_conflict_and_reset(self):
        default = await self.service.get_ambient_settings(self.token)
        settings = default['settings']
        for key, value in [('fade_out', 0), ('fade_in', 6001), ('gap', True), ('variation', 46), ('overlap', float('nan'))]:
            with self.subTest(key=key), self.assertRaises(personal.PersonalProfileError):
                await self.service.set_ambient_settings(self.token, {'settings': {**settings, key: value}, 'revision': ''})
        changed = await self.service.set_ambient_settings(self.token, {'settings': {**settings, 'gap': 200}, 'revision': ''})
        with self.assertRaises(personal.PersonalProfileError) as conflict:
            await self.service.set_ambient_settings(self.token, {'settings': {**settings, 'gap': 300}, 'revision': ''})
        self.assertEqual(conflict.exception.status, 409)
        restored = await self.service.set_ambient_settings(self.token, {'settings': settings, 'revision': changed['revision']})
        self.assertEqual(restored['settings'], settings)
        self.assertEqual(restored, await self.service.set_ambient_settings(self.token, {'settings': settings, 'revision': ''}), 'idempotent retry')
