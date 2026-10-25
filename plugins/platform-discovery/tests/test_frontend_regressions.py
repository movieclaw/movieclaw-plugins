"""在宿主 unittest 检查中运行无需浏览器的前端回归。"""
import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendRegressions(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "前端回归需要 Node.js")
    def test_frontend_regressions(self):
        for name in ['test_transport.cjs']:
            with self.subTest(script=name):
                subprocess.run(["node", str(Path(__file__).with_name(name))], check=True)
