import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class NativePageTests(unittest.TestCase):
    def test_generated_page_has_one_source_and_no_nested_frame(self):
        spec = importlib.util.spec_from_file_location('build_native_page', ROOT / 'tools/build_web.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        native = (ROOT / 'web/index.html').read_text()
        self.assertEqual(native, module.render())
        self.assertNotIn('<iframe', native)
        self.assertIn('src="/api/v1/plugins/platform-discovery/navigation.js"', native)
        self.assertIn('href="/api/v1/plugins/platform-discovery/assets/workspace.css"', native)

    def test_stable_official_menu_contract(self):
        manifest = json.loads((ROOT / 'web/manifest.webmanifest').read_text())
        self.assertEqual(manifest['short_name'], '海报墙')
        self.assertEqual([item['short_name'] for item in manifest['shortcuts']], ['录像厅', '平台热榜', '随心看剧'])
        for item in [manifest] + manifest['shortcuts']:
            for icon in item['icons']:
                self.assertTrue((ROOT / 'web' / icon['src']).is_file())

    def test_v041_host_parses_all_four_menu_entries(self):
        try:
            from movieclaw_api.services import plugin_web
        except ImportError:
            self.skipTest('Native menu parser requires MovieClaw 0.41.0')
        plugin = plugin_web.WebPlugin(id='platform-discovery', title='平台放映厅', source='package', root=ROOT / 'web', consent=None)
        manifest = plugin_web.read_manifest(plugin)
        self.assertEqual([entry.label for entry in manifest.entries], ['海报墙', '录像厅', '平台热榜', '随心看剧'])
        self.assertEqual([entry.path for entry in manifest.entries], ['', 'theater', 'platforms', 'random'])
        self.assertTrue(all(entry.monochrome for entry in manifest.entries))
