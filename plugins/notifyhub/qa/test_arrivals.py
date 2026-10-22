import copy
import pytest
import wecom_notify as n
from test_plugin import context,settings,deliver,CASES,capture


def test_removed_settings_and_single_address():
 fields=n.Settings.model_json_schema()['properties']
 assert 'jellyfin_image_base' not in fields and 'excluded_events' not in fields
 cfg=settings(movieclaw_url='https://media.example',jellyfin_image_base='https://old.example',excluded_events=['subscription.created'])
 assert n.enabled('subscription.created',cfg)
 assert n.jellyfin_image({'id':21,'kind':'movie'},[],cfg).startswith('https://media.example/Items/')

@pytest.mark.asyncio
async def test_batch_arrival_metadata_and_partial_dedupe(capture,monkeypatch):
 async def metadata(self,name,data):return {'overview':data['media']['title']+'简介'}
 monkeypatch.setattr(n.Enricher,'get',metadata)
 ctx,r=await context(settings(movieclaw_url='https://media.example'))
 await deliver(ctx,'library.ingest.completed',eid='prior')
 payload=copy.deepcopy(CASES['library.items.arrived'])
 payload['items'][0]['units'].append({'season':1,'episode':2})
 payload['items'].append({'media':{'id':11,'kind':'movie','title':'电影甲'},'units':[]})
 await deliver(ctx,'library.items.arrived',payload,eid='batch')
 await deliver(ctx,'library.items.arrived',payload,eid='batch')
 assert len(capture)==3
 assert 'S01E02' in capture[1]['content'] and 'S01E01' not in capture[1]['content']
 assert '电影甲简介' in capture[2]['content'] and '测试剧集简介' not in capture[2]['content']
 assert '/item/11' in capture[2]['push_link_url']
 assert '000000000000000b' in capture[2]['push_img_url']
 await ctx.dispose()

@pytest.mark.asyncio
async def test_arrival_switch_independent_of_imports(capture):
 ctx,r=await context(settings(imports=False))
 await deliver(ctx,'library.items.arrived')
 assert len(capture)==1
 await ctx.dispose()
 ctx,r=await context(settings(library_arrivals=False))
 assert 'notifyhub-library-items-arrived' not in ctx._handlers
 await ctx.dispose()
