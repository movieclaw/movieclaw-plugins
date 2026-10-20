"""Pins the real upstream contract; this does not declare semantic compliance."""
import ast
import hashlib
import inspect
import json
from pathlib import Path
import unittest
import tomllib
from movieclaw_sdk import drives as dv
from cloud_123_personal.backend import Personal123

class ContractTests(unittest.TestCase):
    def test_sdk_source_matches_reviewed_baseline(self):
        baseline=json.loads((Path(__file__).parent/'contract_baseline.json').read_text())
        self.assertEqual(hashlib.sha256(Path(dv.__file__).read_bytes()).hexdigest(),baseline['sha256'])
        self.assertEqual(str(dv.DRIVE_BACKENDS.version),'1.5')

    def test_enabled_method_parameter_names_match_real_sdk(self):
        manifest=tomllib.loads((Path(__file__).parents[1]/"movieclaw-plugin.toml").read_text())["plugin"]
        self.assertEqual(manifest["id"], "cloud-123-personal")
        self.assertEqual(manifest["version"], "0.1.0-beta.1")
        self.assertEqual(manifest["title"], "123 云盘个人账号")
        self.assertEqual(Personal123.title, manifest["title"])
        for name in ('login_with_form','account_info','list_dir','list_tree','ensure_dir','begin_upload','upload_part','finish_upload','delete','stat','direct_link'):
            with self.subTest(method=name):
                self.assertEqual(list(inspect.signature(getattr(dv.DriveBackend,name)).parameters),list(inspect.signature(getattr(Personal123,name)).parameters))
        self.assertFalse(Personal123.login.scan)
        self.assertFalse(Personal123.login.cookie_domains)
        self.assertFalse(Personal123.capabilities.change_feed)

    def test_production_imports_only_public_sdk_and_dependencies(self):
        root=Path(__file__).parents[1]/'cloud_123_personal'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom) and node.module:
                    if node.module == 'movieclaw_api.plugins.keys':
                        self.assertEqual([alias.name for alias in node.names],['PLUGIN_DATA'])
                    else:
                        self.assertFalse(node.module.startswith(('movieclaw_api','movieclaw_db','movieclaw_kernel')))
        self.assertNotIn('p123disk',(root/'client.py').read_text().lower())
