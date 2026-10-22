"""Standard-library tests run by the catalog's unittest CI."""
import copy
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor'))
import wecom_notify as notify
from dedupe import Ledger, LIMIT
from uploads import UploadMonitor


class MemoryStore:
    def __init__(self, values=None):
        self.values = values or {}
        self.max_size = 0
    async def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))
    async def set(self, key, value):
        size = len(json.dumps(value, ensure_ascii=False).encode())
        if size > 65536:
            raise ValueError('value exceeds 64 KiB')
        self.max_size = max(self.max_size, size)
        self.values[key] = copy.deepcopy(value)


class SettingsAndTemplates(unittest.TestCase):
    def test_movie_never_has_episodes(self):
        config = notify.Settings(enrich_metadata=False)
        body, _, units = notify.render('subscription.fulfilled', {
            'media': {'id': 1, 'kind': 'movie', 'title': 'Test movie'},
            'units': [{'season': 0, 'episode': 0}],
        }, config)[0]
        self.assertEqual(units, [])
        self.assertNotIn('S00', body['content'])
        self.assertNotIn('第 0 季', body['title'])

    def test_episode_grouping(self):
        self.assertEqual(notify.episode_display([{'season': 1, 'episode': n} for n in [1, 2, 3]]), 'S01E01–E03')

    def test_settings_and_display_name(self):
        schema = notify.Settings.model_json_schema()['properties']
        self.assertNotIn('jellyfin_image_base', schema)
        self.assertNotIn('excluded_events', schema)
        self.assertEqual(notify.apply.title, '企业微信直推')
        self.assertIn('filenames', notify.Settings().upload_text)

    def test_secret_not_in_repr(self):
        self.assertNotIn('secret-value', repr(notify.Settings(corp_secret='secret-value')))


class StorageAndUpload(unittest.IsolatedAsyncioTestCase):
    async def test_migration_growth_and_restart_stay_below_limit(self):
        old = {'event:old': time.time() + 1000}
        store = MemoryStore({'dedupe-v1': old})
        ledger = Ledger(store)
        await ledger.migrate()
        entries = {'event:' + notify.digest(n): time.time() + 1000 for n in range(3000)}
        self.assertGreater(len(json.dumps(entries).encode()), 65536)
        await ledger.remember(entries)
        self.assertLessEqual(store.max_size, LIMIT)
        restarted = Ledger(store)
        await restarted.migrate()
        self.assertTrue(await restarted.contains('event:old'))
        self.assertTrue(all([await restarted.contains(key) for key in entries]))

    async def test_failed_file_not_in_successful_names(self):
        events = [
            {'id': 1, 'event_type': 'progress', 'payload': {'phase': 'uploading', 'message': '读取「good.mkv」算指纹', 'details': {'done': 0, 'failed': 0}}},
            {'id': 2, 'event_type': 'progress', 'payload': {'phase': 'uploading', 'message': '已上传 1 个', 'details': {'done': 1, 'failed': 0}}},
            {'id': 3, 'event_type': 'progress', 'payload': {'phase': 'uploading', 'message': '读取「bad.mkv」算指纹', 'details': {'done': 1, 'failed': 0}}},
            {'id': 4, 'event_type': 'progress', 'payload': {'phase': 'uploading', 'message': '已上传 1 个', 'details': {'done': 1, 'failed': 1}}},
        ]
        class Reader:
            async def call(self, operation, args):
                return {'items': events}
        class Sender:
            enricher = Reader()
        result = await UploadMonitor(Sender()).successful_names('job', 1)
        self.assertEqual(result['filenames'], ['good.mkv'])
        self.assertEqual(result['filenames_missing'], 0)
