"""只认成功导入任务保存的文件 ID；不按同名或体积猜测文件身份。"""
import posixpath
import re
from movieclaw_sdk.jobs import JobBlocked
import p115batch as batch

async def inspect_names(pan, plan, state, result):
    if not result.get("inventory_verified"):
        raise JobBlocked("原任务没有逐文件库存核验结果，不能自动修名")
    listings={}
    out=[]
    evidence={row["path"]:row for row in result.get("moved",[])}
    for ident,cp in (state.get("items") or {}).items():
        moved=(state.get("moved") or {}).get(ident) or {}
        old=moved.get("name") or ""
        canonical=re.sub(r" - p115-[0-9a-f]{8}(?=\.[^.]+$)","",old)
        if canonical==old:
            continue
        if moved.get("path") not in evidence or not cp.get("moved") or not cp.get("remote_id") or not cp.get("target_id"):
            raise JobBlocked("检查点与成功记录不一致，未改名")
        cid=cp["target_id"]
        if cid not in listings:
            listings[cid]=await pan.list_dir(cid)
        rows=listings[cid]
        entry=next((e for e in rows if e.remote_id==cp["remote_id"] and not e.is_dir),None)
        if entry is None or int(entry.size)!=int(moved.get("size") or 0):
            raise JobBlocked("远端文件 ID 或体积与成功记录不一致，未改名")
        if entry.name not in (old,canonical):
            raise JobBlocked("文件已被外部改成其他名字，未修改")
        if entry.name!=canonical and any(e.name==canonical and e.remote_id!=entry.remote_id for e in rows):
            raise JobBlocked("规范名已被其他文件占用，未覆盖")
        out.append({"remote_id":entry.remote_id,"old_name":old,"current_name":entry.name,
                    "canonical_name":canonical,"needs_rename":entry.name!=canonical,
                    "size":entry.size,"target_id":cid,"canonical_path":posixpath.join(posixpath.dirname(moved["path"]),canonical)})
    return out

async def repair_names(pan, rows, context=None):
    operations=[{"remote_id":row["remote_id"],"source_id":str(row["target_id"]),
                 "from_name":row["old_name"],"name":row["canonical_name"],"size":row["size"]}
                for row in rows if row["needs_rename"]]
    verified=await batch.renamed(pan,operations,context=context)
    return [row["remote_id"] for row in verified]
