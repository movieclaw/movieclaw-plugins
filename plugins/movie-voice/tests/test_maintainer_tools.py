import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from build_share import check_shared
from publish_personal import make_payload, validate_url
from shortcut_source import PLACEHOLDER, workflow

URL = 'https://media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=TEST_EPOCH&sig=TEST_SIGNATURE'


class MaintainerToolTests(unittest.TestCase):
    def test_shortcut_asks_for_tv_seasons_before_final_confirmation(self):
        actions=workflow(shareable=True)['WFWorkflowActions']
        season_index=next(i for i,a in enumerate(actions) if a['WFWorkflowActionParameters'].get('WFDictionaryKey')=='needs_seasons')
        alert_index=next(i for i,a in enumerate(actions) if a['WFWorkflowActionIdentifier']=='is.workflow.actions.alert')
        between=actions[season_index:alert_index]
        self.assertTrue(any('第几季' in a['WFWorkflowActionParameters'].get('WFAskActionPrompt','') for a in between))
        self.assertTrue(any(a['WFWorkflowActionIdentifier']=='is.workflow.actions.downloadurl' and a['WFWorkflowActionParameters'].get('WFHTTPMethod')=='POST' for a in between))

    def test_share_cannot_receive_a_private_configuration(self):
        with self.assertRaises(ValueError):
            workflow(shareable=True, search_url=URL)
        with self.assertRaises(ValueError):
            workflow()
        public = workflow(shareable=True)
        check_shared(public)
        public['WFWorkflowActions'][2]['WFWorkflowActionParameters']['WFTextActionText'] = URL
        with self.assertRaises(ValueError):
            check_shared(public)

    def test_private_payload_requires_exact_verified_url(self):
        value = workflow(search_url=URL)
        data = b'AEA1' + b'0' * 300  # 这里只测试验签后的发布门槛；真实验签在 Mac 工具中。
        payload = make_payload(URL, data, value)
        self.assertEqual(base64.b64decode(payload['signed_file_base64']), data)
        value['WFWorkflowActions'][2]['WFWorkflowActionParameters']['WFTextActionText'] = PLACEHOLDER
        with self.assertRaises(ValueError):
            make_payload(URL, data, value)
        for malformed in [b'unsigned', b'AEA1' + b'0' * 44000]:
            with self.assertRaises(ValueError):
                make_payload(URL, malformed, workflow(search_url=URL))

    def test_publisher_rejects_homepage_credentials_and_unsigned_urls(self):
        self.assertEqual(validate_url(URL), URL)
        for url in ['https://media.example.com', URL.replace('https://', 'file://'),
                    URL.replace('media.example.com', 'user:password@media.example.com'),
                    URL.split('?')[0], URL + '#fragment', None]:
            with self.assertRaises(ValueError):
                validate_url(url)
