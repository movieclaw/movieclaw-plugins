"""适配宿主插件数据的64KB单值限制；分片先写、索引最后原子替换。"""
import json
import uuid

_TAG="p115-chunks-v1"


class ChunkedStore:
    def __init__(self,store):self.store=store

    async def get(self,key,default=None):
        value=await self.store.get(key,default=default)
        if isinstance(value,dict) and value.get("_format")==_TAG:
            pieces=[]
            for part in value["parts"]:
                chunk=await self.store.get(part,default=None)
                if not isinstance(chunk,str):
                    raise RuntimeError("导入检查点分片缺失，已停止以免重复执行")
                pieces.append(chunk)
            return json.loads("".join(pieces))
        return value

    async def set(self,key,value,secret=False):
        previous=await self.store.get(key,default=None)
        text=json.dumps(value,ensure_ascii=False,separators=(",",":"))
        chunks=[]
        if len(json.dumps(value,ensure_ascii=False).encode())>48*1024:
            # 中文JSON再编码时可能增加转义，按字符5000分片，即使全为控制字符也<64KB。
            generation=uuid.uuid4().hex
            for offset in range(0,len(text),5000):
                part="chunk:"+generation+":"+str(offset//5000)
                await self.store.set(part,text[offset:offset+5000],secret=secret)
                chunks.append(part)
            manifest={"_format":_TAG,"parts":chunks}
            await self.store.set(key,manifest,secret=secret)
        else:
            await self.store.set(key,value,secret=secret)
        # 切换索引后再清理旧分片，失败不能使已提交的新检查点失效。
        if isinstance(previous,dict) and previous.get("_format")==_TAG:
            for part in previous["parts"]:
                try:await self.store.delete(part)
                except Exception:pass

    async def delete(self,key):
        previous=await self.store.get(key,default=None)
        result=await self.store.delete(key)
        if isinstance(previous,dict) and previous.get("_format")==_TAG:
            for part in previous["parts"]:
                await self.store.delete(part)
        return result
