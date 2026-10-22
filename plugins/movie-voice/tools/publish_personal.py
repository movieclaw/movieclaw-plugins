"""在 Mac 为 mclaw 当前连接的服务器签名并私密上传个人版。"""
import argparse
import base64
import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from shortcut_archive import decode
from shortcut_source import workflow


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError('服务器没有返回 SEARCH_URL')
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if (parts.scheme not in ('https', 'http') or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.fragment
            or parts.path != '/api/v1/plugins/movie-voice/shortcut/search'
            or not query.get('sig') or not query.get('epoch')):
        raise ValueError('服务器没有返回有效的专用链接')
    return url


def make_payload(url, data, verified):
    validate_url(url)
    configured = verified['WFWorkflowActions'][2]['WFWorkflowActionParameters']['WFTextActionText']
    if configured != url:
        raise ValueError('签名文件中的服务器配置不一致，未发布')
    if not 256 <= len(data) <= 44000 or data[:4] != b'AEA1':
        raise ValueError('签名文件封装或大小不符合服务器要求，未发布')
    return {'configuration_sha256': hashlib.sha256(url.encode()).hexdigest(),
            'file_sha256': hashlib.sha256(data).hexdigest(),
            'signed_file_base64': base64.b64encode(data).decode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', help='使用 mclaw 已配对的服务器上下文')
    args = parser.parse_args()
    cli = shutil.which('mclaw')
    if not cli:
        raise ValueError('请先安装 mclaw、加入 PATH 并登录自己的服务器')
    prefix = [cli] + (['--context', args.context] if args.context else [])

    def call(operation, payload=None):
        command = prefix + ['plugins', 'movie-voice', operation]
        if payload is not None:
            command += ['--input', '-']
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, check=True, timeout=60)
        body = json.loads(result.stdout)
        if body.get('success') is False:
            raise ValueError('服务器拒绝操作，请检查登录状态和插件状态')
        return body.get('data', body)

    url = validate_url(call('shortcut-pair').get('search_url'))
    with tempfile.TemporaryDirectory(prefix='movie-voice-personal-') as temp:
        folder = Path(temp)
        source, target = folder / 'personal.unsigned.shortcut', folder / 'personal.shortcut'
        source.write_bytes(plistlib.dumps(workflow(search_url=url), fmt=plistlib.FMT_BINARY))
        source.chmod(0o600)
        print('正在通过 macOS 为当前服务器签名个人版……', flush=True)
        subprocess.run(['shortcuts', 'sign', '--mode', 'anyone', '--input', str(source),
                        '--output', str(target)], check=True, capture_output=True, timeout=180)
        payload = make_payload(url, target.read_bytes(), decode(target, folder))
        call('personal-prepare', payload)
    print('个人版已私密保存。刷新寻映页面即可下载；通用分享版未改动。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as error:
        # 不打印 CLI 捕获的响应、URL 或上传内容，避免把授权写入日志。
        raise SystemExit(f'未能准备个人版（{type(error).__name__}），请检查 Mac 工具与服务器状态。') from None
