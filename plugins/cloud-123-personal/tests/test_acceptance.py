import json
import secrets
import unittest
from cloud_123_personal.acceptance import summarize

class AcceptanceTests(unittest.TestCase):
    def test_allowlist_never_emits_values_or_unknown_keys(self):
        secret=secrets.token_urlsafe(32)
        result=summarize({'code':0,'token':secret,'data':{'Parts':[{'PartNumber':1,'Size':4,'ETag':secret,secret:secret}],secret:secret,'next':secret}})
        self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(result['Parts']['count'],1)
        self.assertFalse(result['progress_verified'])
    def test_empty_known_observation_still_not_resume_proof(self):
        result=summarize({'code':0,'data':{'Parts':None}})
        self.assertEqual(result['Parts']['type'],'NoneType')
        self.assertFalse(result['progress_verified'])
