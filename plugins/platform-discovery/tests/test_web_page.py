import importlib.util
import json
import unittest
import xml.etree.ElementTree as ET
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
        self.assertIn('href="/api/v1/plugins/platform-discovery/assets/yingyu-icon.svg"', native)
        self.assertIn('src="/api/v1/plugins/platform-discovery/assets/yingyu-icon.svg"', native)

    def test_stable_official_menu_contract(self):
        manifest = json.loads((ROOT / 'web/manifest.webmanifest').read_text())
        self.assertEqual(manifest['short_name'], '映屿')
        self.assertEqual(manifest.get('shortcuts', []), [])
        for item in [manifest] + manifest.get('shortcuts', []):
            for icon in item['icons']:
                self.assertTrue((ROOT / 'web' / icon['src']).is_file())

    def test_v041_host_exposes_one_plugin_entry(self):
        try:
            from movieclaw_api.services import plugin_web
        except ImportError:
            self.skipTest('Native menu parser requires MovieClaw 0.41.0')
        plugin = plugin_web.WebPlugin(id='platform-discovery', title='映屿', source='package', root=ROOT / 'web', consent=None)
        manifest = plugin_web.read_manifest(plugin)
        self.assertEqual([entry.label for entry in manifest.entries], ['映屿'])
        self.assertEqual([entry.path for entry in manifest.entries], [''])
        self.assertTrue(all(entry.monochrome for entry in manifest.entries))

    def test_brand_assets_share_the_mark_and_have_no_active_or_external_content(self):
        mono = ET.parse(ROOT / 'web/icon.svg').getroot()
        color = ET.parse(ROOT / 'web/icon-color.svg').getroot()
        ns = {'s': 'http://www.w3.org/2000/svg'}
        self.assertEqual([e.attrib['d'] for e in mono.findall('.//s:path', ns)],
                         [e.attrib['d'] for e in color.findall('.//s:path', ns)])
        self.assertEqual((ROOT / 'web/icon-color.svg').read_bytes(),
                         (ROOT / 'platform_discovery/assets/yingyu-icon.svg').read_bytes())
        for root in (mono, color):
            for element in root.iter():
                self.assertNotIn(element.tag.rsplit('}', 1)[-1], {'script', 'foreignObject', 'image'})
                for key, value in element.attrib.items():
                    self.assertFalse(key.lower().startswith('on'))
                    self.assertFalse(key.rsplit('}', 1)[-1] == 'href' and not value.startswith('#'))
        manifest = json.loads((ROOT / 'web/manifest.webmanifest').read_text())
        self.assertEqual({icon['purpose'] for icon in manifest['icons']}, {'any', 'monochrome'})
        self.assertTrue(json.loads((ROOT / 'store.json').read_text())['icon'].endswith('/web/icon-color.svg'))
