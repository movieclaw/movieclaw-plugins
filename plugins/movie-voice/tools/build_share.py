"""在 Mac 构建或 --check 验证未配置分享版；绝不读取本机服务器凭据。"""
import argparse
import plistlib
import subprocess
import tempfile
from pathlib import Path

from shortcut_archive import decode
from shortcut_source import PLACEHOLDER, workflow

ASSET = Path(__file__).resolve().parents[1] / 'web/downloads/find-movie-share.shortcut'


def check_shared(value):
    actions = value['WFWorkflowActions']
    if actions[2]['WFWorkflowActionParameters']['WFTextActionText'] != PLACEHOLDER:
        raise ValueError('分享版含已配置的 SEARCH_URL，禁止分发')
    if actions[3]['WFWorkflowActionParameters'].get('WFConditionalActionString') != PLACEHOLDER:
        raise ValueError('分享版缺少首次配置检查')
    help_text = actions[0]['WFWorkflowActionParameters']['WFCommentActionText']
    if '分享版｜未配置' not in help_text or '“寻映”插件页面' not in help_text:
        raise ValueError('分享版配置说明不完整')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只验签检查现有公开文件')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='movie-voice-share-') as temp:
        folder = Path(temp)
        target = ASSET
        if not args.check:
            source = folder / 'share.unsigned.shortcut'
            target = folder / 'share.shortcut'
            source.write_bytes(plistlib.dumps(workflow(shareable=True), fmt=plistlib.FMT_BINARY))
            subprocess.run(['shortcuts', 'sign', '--mode', 'anyone', '--input', str(source),
                            '--output', str(target)], check=True, capture_output=True, timeout=180)
        check_shared(decode(target, folder))
        if not args.check:
            ASSET.parent.mkdir(parents=True, exist_ok=True)
            ASSET.write_bytes(target.read_bytes())
            ASSET.chmod(0o644)
    print('未配置分享版验签与配置检查通过。')


if __name__ == '__main__':
    main()
