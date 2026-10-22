"""管理员个人版的私密分发。已签名文件只存插件加密数据，绝不写入 web/。"""
import base64
import hashlib
import plistlib
import struct
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

KEY = 'personal-shortcut-v1'
HEADERS = {'Cache-Control':'private, no-store', 'Pragma':'no-cache', 'X-Content-Type-Options':'nosniff'}


class PreparedPersonal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    configuration_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    file_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    signed_file_base64: str = Field(min_length=100, max_length=59000)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def signed_container(data):
    # 签名及内嵌配置由 Mac 发布工具用 Apple Archive 验证；服务端再检查封装及传输摘要。
    if not 256 <= len(data) <= 44000 or data[:4] != b'AEA1':
        return False
    size = struct.unpack('<I', data[8:12])[0]
    if not 1 <= size < len(data)-12:
        return False
    try:
        metadata = plistlib.loads(data[12:12+size])
        return isinstance(metadata, dict) and bool(metadata.get('SigningCertificateChain'))
    except Exception:
        return False


def mount_personal(router, store, signed):
    async def current():
        return digest(await signed('/shortcut/search'))

    async def ready():
        record = await store.get(KEY)
        if not isinstance(record, dict) or record.get('configuration_sha256') != await current():
            return None
        try:
            data = base64.b64decode(record['signed_file_base64'], validate=True)
        except Exception:
            return None
        if hashlib.sha256(data).hexdigest() != record.get('file_sha256') or not signed_container(data):
            return None
        return record, data

    @router.get('/shortcut/personal/status', operation_id='plugins.movie-voice.personal-status', summary='查看当前服务器已签名个人版是否就绪')
    async def status():
        found = await ready()
        return JSONResponse({'available':bool(found), 'prepared_at':found[0]['prepared_at'] if found else None,
                             'message':'已配置当前服务器，下载后直接添加即可。' if found else '当前服务器尚未准备已签名个人版，可先使用通用分享版。'}, headers=HEADERS)

    @router.post('/shortcut/personal/prepare', operation_id='plugins.movie-voice.personal-prepare', summary='保存 Mac 已签名、已核对配置的个人版，仅限管理员')
    async def prepare(payload:PreparedPersonal, request:Request):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '请使用自己的签名工具')
        if payload.configuration_sha256 != await current():
            raise HTTPException(409, '服务器地址或授权已变化，请重新生成并签名')
        try:
            data = base64.b64decode(payload.signed_file_base64, validate=True)
        except Exception:
            raise HTTPException(400, '文件编码不正确') from None
        if not signed_container(data) or hashlib.sha256(data).hexdigest() != payload.file_sha256:
            raise HTTPException(400, '签名文件封装或传输摘要不正确')
        record = {**payload.model_dump(), 'prepared_at':datetime.now(timezone.utc).isoformat()}
        await store.set(KEY, record, secret=True)
        return JSONResponse({'available':True,'prepared_at':record['prepared_at']},headers=HEADERS)

    @router.get('/shortcut/personal/download', operation_id='plugins.movie-voice.personal-download', summary='下载已配置当前服务器的私密个人版快捷指令')
    async def download():
        found = await ready()
        if not found:
            raise HTTPException(409, '个人版未就绪或授权已变化，需要重新签名；通用分享版仍可使用',headers=HEADERS)
        filename=quote('找电影·个人版.shortcut')
        return Response(found[1], media_type='application/octet-stream', headers={**HEADERS,
            'Content-Disposition':f"attachment; filename=movie-voice-personal.shortcut; filename*=UTF-8''{filename}"})
