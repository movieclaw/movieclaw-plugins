"""Shared test doubles (Store/Files/Drive/Context) for the plugin test suite."""
import asyncio
import copy
import json


class Store:
 def __init__(self):self.data={};self.secrets=set()
 async def get(self,k,default=None):return copy.deepcopy(self.data.get(k,default))
 async def set(self,k,v,secret=False):
  assert len(json.dumps(v).encode())<65536
  self.data[k]=copy.deepcopy(v)
  if secret:self.secrets.add(k)
 async def delete(self,k):return self.data.pop(k,None)
class Files:
 async def open(self,p,m):assert m=='rb';return open(p,m)
class Drive:
 def __init__(self):self.items=[]
 async def list(self,cid,offset=0):return {'items':self.items,'count':len(self.items)}
class Context:
 def task(self,coro,name):return asyncio.create_task(coro)
