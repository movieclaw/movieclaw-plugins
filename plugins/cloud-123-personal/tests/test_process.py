"""Fresh OS processes; fixture host persistence, real SDK request serialization.
No production installation, real account, host DB or service network is involved.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'synthetic-service.json'
    def tearDown(self):self.tmp.cleanup()
    def worker(self,action,code=0):
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        root=str(Path(__file__).parents[1]);env['PYTHONPATH']=root+os.pathsep+env.get('PYTHONPATH','')
        p=subprocess.run([sys.executable,'-m','tests.process_worker',str(self.path),action],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,code,p.stderr)
        return json.loads(p.stdout) if code==0 else None
    def test_real_sdk_scan_in_independent_process(self):
        result=self.worker('scan')
        self.assertEqual(result['paths'],['sub/note.txt']);self.assertEqual(result['dirs'],['101','102'])
        self.assertGreaterEqual(json.loads(self.path.read_text())['paced'],3)
    def test_durable_ack_recovery_without_host_checkpoint_across_processes(self):
        first=self.worker('first');second=self.worker('finish');third=self.worker('complete_again')
        self.assertEqual((first['next'],second['start'],second['next'],third['size']),(1,1,3,10))
        self.assertEqual(len({first['pid'],second['pid'],third['pid']}),3)
    def test_hard_exit_after_provider_put_before_journal_ack(self):
        self.worker('first');self.worker('crash_put',75)
        recovered=self.worker('finish');self.assertEqual(recovered['start'],1)
        self.assertEqual(self.worker('complete_again')['size'],10)
    def test_hard_exit_after_merge_never_claims_success(self):
        self.worker('first');self.worker('crash_merge',76)
        fixture=json.loads(self.path.read_text());before=fixture['paced']
        self.assertTrue(self.worker('blocked')['blocked'])
        self.assertEqual(json.loads(self.path.read_text())['paced'],before)
        self.assertEqual(next(iter(fixture['data'].values()))['phase'],'committing')
