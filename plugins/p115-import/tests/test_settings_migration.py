"""工作台配置迁移、加密存储、热更新和失败回滚测试；不访问真实账号。"""
import asyncio
import copy
import unittest
from types import SimpleNamespace

from movieclaw_sdk.config_schema import describe_config
import p115_import as p


class Store:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.fail = False

    async def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    async def set(self, key, value, secret=False):
        if self.fail:
            raise RuntimeError("模拟保存失败")
        self.data[key] = copy.deepcopy(value)
        self.writes.append((key, secret))


class Migration(unittest.IsolatedAsyncioTestCase):
    def legacy(self):
        return {"app_id": "123456", "login_app": "鸿蒙端", "offline_enabled": True,
                "offline_retry": False, "wecom_entry": True, "wecom_user": "test-user",
                "wecom_push_url": "https://fake.example/push/only-for-test", "dry_run": False}

    async def test_plugin_has_no_host_settings_form(self):
        self.assertIsNone(p.apply.config)
        self.assertIsNone(describe_config(p.apply.config))

    async def test_migrate_once_keep_every_setting_and_never_return_secret(self):
        store = Store()
        cfg = await p.SettingsManager.load(store, self.legacy())
        self.assertEqual(p.SettingsManager.serialize(cfg.current), self.legacy())
        self.assertEqual(store.writes, [(p.SettingsManager.KEY, True)])
        view = cfg.view()
        self.assertNotIn("wecom_push_url", view["values"])
        self.assertNotIn("wecom_user", view["values"])
        self.assertNotIn("wecom_push_url", view["schema"]["properties"])
        self.assertNotIn("wecom_user", view["schema"]["properties"])
        self.assertEqual(view["secrets_set"], [])
        self.assertNotIn("only-for-test", repr(view))
        # 新配置优先；旧通知信息加密留作回退，不再对外显示或参与通知。
        await cfg.update({"app_id": "987"})
        again = await p.SettingsManager.load(store, self.legacy())
        self.assertEqual(again.current.app_id, "987")
        self.assertEqual(again.current.wecom_push_url.get_secret_value(), "https://fake.example/push/only-for-test")
        self.assertEqual(again.view()["secrets_set"], [])
        self.assertTrue(all(secret for _, secret in store.writes))

    async def test_context_tracks_live_changes_and_validates(self):
        cfg = await p.SettingsManager.load(Store(), self.legacy())
        context = p.SettingsContext(SimpleNamespace(entry_id="p115-import"), cfg)
        self.assertEqual(context.entry_id, "p115-import")
        self.assertFalse(context.config.dry_run)
        await cfg.update({"dry_run": True})
        self.assertTrue(context.config.dry_run)
        for bad in ({"app_id": "1-not-digits"}, {"dry_run": "true"},
                    {"login_app": "not-a-client"}, {"unexpected": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await cfg.update(bad)
        self.assertEqual(cfg.current.app_id, "123456")
        self.assertTrue(cfg.current.dry_run)

    async def test_failed_save_preserves_in_memory_and_durable_state(self):
        store = Store()
        cfg = await p.SettingsManager.load(store, self.legacy())
        before = copy.deepcopy(store.data)
        store.fail = True
        with self.assertRaises(RuntimeError):
            await cfg.update({"app_id": "987"})
        self.assertEqual(store.data, before)
        self.assertEqual(cfg.current.app_id, "123456")

    async def test_concurrent_partial_updates_do_not_clobber_each_other(self):
        store = Store()
        cfg = await p.SettingsManager.load(store, self.legacy())
        await asyncio.gather(cfg.update({"app_id": "123"}), cfg.update({"offline_retry": True}))
        self.assertEqual(cfg.current.app_id, "123")
        self.assertTrue(cfg.current.offline_retry)
        restarted = await p.SettingsManager.load(store, {})
        self.assertEqual(restarted.current.app_id, "123")
        self.assertTrue(restarted.current.offline_retry)


if __name__ == "__main__":
    unittest.main()
