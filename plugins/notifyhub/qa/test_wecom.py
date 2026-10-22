import asyncio,json,logging
from pathlib import Path
from types import SimpleNamespace
import httpx,pytest
import wecom_notify as n
from wecom_client import WecomClient,WecomError,card_description,clip_bytes
from enrichment import Enricher


def cfg(**kwargs):
 values=dict(corp_id='test-corp',corp_secret='secret-never-log',agent_id=10,to_user='one',enrich_metadata=False)
 values.update(kwargs);return n.Settings(**values)


@pytest.mark.asyncio
async def test_token_cache_and_refresh(monkeypatch):
 tokens,sent=[],[]
 def respond(request):
  if request.url.path.endswith('gettoken'):
   tokens.append(request);return httpx.Response(200,json={'access_token':'token-'+str(len(tokens)),'expires_in':7200})
  sent.append(request)
  return httpx.Response(200,json={'errcode':42001 if len(sent)==2 else 0})
 monkeypatch.setattr(n.net,'http_transport',lambda service:httpx.MockTransport(respond))
 client=WecomClient(cfg(),logging.getLogger('test'))
 await client.send({'title':'标题','content':'正文'})
 await client.send({'title':'标题2','content':'正文2'})
 assert len(tokens)==2 and len(sent)==3
 assert sent[1].url.params['access_token']=='token-1' and sent[2].url.params['access_token']=='token-2'
 assert tokens[0].url.params['corpsecret']=='secret-never-log'


@pytest.mark.asyncio
async def test_concurrent_token_fetch(monkeypatch):
 calls=[]
 async def respond(request):
  calls.append(request);await asyncio.sleep(.01)
  return httpx.Response(200,json={'access_token':'token','expires_in':7200})
 monkeypatch.setattr(n.net,'http_transport',lambda _:httpx.MockTransport(respond))
 client=WecomClient(cfg(),logging.getLogger('test'))
 assert await asyncio.gather(client.access_token(),client.access_token())==['token','token']
 assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('code',[40013,40001,60020,81013,45009])
async def test_wecom_errors_are_not_success(monkeypatch,code):
 def respond(request):
  if request.url.path.endswith('gettoken'):return httpx.Response(200,json={'access_token':'test-token','expires_in':7200})
  return httpx.Response(200,json={'errcode':code,'errmsg':'secret-never-log test-token'})
 monkeypatch.setattr(n.net,'http_transport',lambda _:httpx.MockTransport(respond))
 with pytest.raises(WecomError) as error:await WecomClient(cfg(),logging.getLogger('test')).send({'title':'标题','content':'正文'})
 assert str(code) in str(error.value) and 'secret-never-log' not in str(error.value) and 'test-token' not in str(error.value)


@pytest.mark.asyncio
async def test_network_exception_redacted(monkeypatch):
 def respond(request):raise httpx.ConnectError(str(request.url),request=request)
 monkeypatch.setattr(n.net,'http_transport',lambda _:httpx.MockTransport(respond))
 with pytest.raises(WecomError) as error:await WecomClient(cfg(),logging.getLogger('test')).access_token()
 assert 'secret-never-log' not in str(error.value) and 'corpsecret' not in str(error.value)


@pytest.mark.asyncio
async def test_refresh_only_once(monkeypatch):
 calls=[]
 def respond(request):
  calls.append(request)
  return httpx.Response(200,json={'access_token':'token','expires_in':7200}) if request.url.path.endswith('gettoken') else httpx.Response(200,json={'errcode':40014})
 monkeypatch.setattr(n.net,'http_transport',lambda _:httpx.MockTransport(respond))
 with pytest.raises(WecomError):await WecomClient(cfg(),logging.getLogger('test')).send({'title':'标题','content':'正文'})
 assert len(calls)==4


def test_card_modes_and_limits():
 client=WecomClient(cfg(),logging.getLogger('test'))
 news=client.message('🎞 标题','正文',image='https://example.com/poster.jpg',link='https://example.com/item')
 assert news['msgtype']=='news' and news['agentid']==10 and news['touser']=='one'
 assert news['news']['articles'][0]['picurl'].endswith('poster.jpg')
 card=client.message('标题','<img src=x>\n详情',link='https://example.com/item')
 assert card['msgtype']=='textcard' and '&lt;img' in card['textcard']['description'] and '<img' not in card['textcard']['description']
 assert len(card_description('&中文'*1000))<=512
 plain=client.message('标题','中文'*2000)
 assert plain['msgtype']=='text' and len(plain['text']['content'].encode())<=2048
 assert client.message('a','b',image='file:///etc/passwd',link='javascript:alert(1)')['msgtype']=='text'
 assert WecomClient(cfg(message_type='text'),logging.getLogger('test')).message('a','b',image='https://example.com/i',link='https://example.com/l')['msgtype']=='text'


def test_user_reference_template_and_missing_fields():
 template_title='🎞 {{ title_year }} {% if season %}{{season_fmt}}{% endif %} 已添加订阅'
 template_text='{% if vote_average %}✨ 评分：{{ vote_average }}/10{% endif %}{% if category %}\n📽 类型：{{ category }}{% endif %}{% if username %}\n👾 用户：{{ username }}{% endif %}\n🔗 链接：{{ __mediainfo__.detail_link }}{% if actors %}\n🤵 参演：{{ actors }}{% endif %}{% if overview %}\n\n🍿 简介：\n{{ overview }}{% endif %}'
 settings=cfg(subscription_title=template_title,subscription_text=template_text,movieclaw_url='https://my.example.com')
 data={'media':{'id':7,'kind':'tv','title':'测试剧集','year':2026},'subscription_id':8,'units':[[2,1]],'_meta':{'vote_average':8.5,'genres':['剧情'],'username':'小明','actors':['演员甲'],'overview':'真实简介'}}
 body,_,_=n.render('subscription.created',data,settings)[0]
 assert '第 2 季' in body['title'] and '8.5/10' in body['content'] and '小明' in body['content'] and '真实简介' in body['content']
 assert body['push_link_url']=='https://my.example.com/subscriptions/8'
 data['_meta']={};body,_,_=n.render('subscription.created',data,settings)[0]
 assert '评分：' not in body['content'] and '用户：' not in body['content'] and 'None' not in body['content']


def test_template_validation_and_secret_schema():
 with pytest.raises(ValueError):cfg(subscription_title='{% if title %}')
 with pytest.raises(ValueError):cfg(api_base='https://user:pass@example.com')
 settings=cfg(to_user=' one | two ')
 assert settings.to_user=='one|two' and 'secret-never-log' not in repr(settings)
 assert n.Settings.model_json_schema()['properties']['corp_secret']['writeOnly']
 assert not n.Settings().configured()
 # Existing user configuration loads without exposing or using old NotifyHub values.
 migrated=n.Settings.model_validate({'url':'http://notify.example.com/api/service/notify','route_id':'route_test','playback':True,'dedupe_minutes':20})
 assert migrated.playback and migrated.dedupe_minutes==20 and not migrated.configured()
 assert 'url' not in n.Settings.model_fields and 'route_id' not in n.Settings.model_fields


class FakeOps:
 def __init__(self,fail=False):self.calls=[];self.fail=fail
 async def client(self,ctx):return self
 async def call(self,operation,args,timeout=None):
  self.calls.append((operation,args))
  if self.fail:raise RuntimeError('unavailable')
  if operation=='discover.get-title-details':return {'title':{'provider_rating':8.4,'genres':['科幻'],'overview':'发现页简介','poster_url':'https://example.com/poster.jpg','backdrop_url':'https://example.com/backdrop.jpg'},'metadata':{'cast':[{'name':'演员甲'}]}}
  if operation=='library.items.get':return {'local_meta':{'rating':9.1,'genres':['剧情'],'plot':'本地简介','actors':[{'name':'演员乙'}]},'poster_url':'/api/v1/libraries/1/artwork'}
  if operation=='subscriptions.get':return {'wanted':[{'season_number':2,'episode_number':1}]}
  if operation=='members.show':return {'nickname':'小明','username':'member1'}


@pytest.mark.asyncio
async def test_actual_metadata_field_mapping():
 ops=FakeOps();ctx=SimpleNamespace(config=cfg(enrich_metadata=True),logger=logging.getLogger('test'),use=lambda key:ops)
 enricher=Enricher(ctx,n.HOST_OPS)
 data={'media':{'id':7,'kind':'tv','tmdb_id':10},'library_id':1,'subscription_id':8,'member_id':3}
 result=await enricher.get('subscription.created',data)
 assert result['vote_average']==9.1 and result['overview']=='本地简介' and result['genres']==['剧情']
 assert result['poster']=='https://example.com/poster.jpg' and result['actors']==['演员乙'] and result['username']=='小明'
 assert result['seasons']==[2]
 assert ('library.items.get',{'library_id':1,'media_item_id':7}) in ops.calls
 assert ('discover.get-title-details',{'title_ref':'tmdb:tv:10'}) in ops.calls


@pytest.mark.asyncio
async def test_metadata_failure_hides_fields():
 ops=FakeOps(fail=True);ctx=SimpleNamespace(config=cfg(enrich_metadata=True),logger=logging.getLogger('test'),use=lambda key:ops)
 data={'media':{'id':7,'kind':'tv','title':'测试','tmdb_id':10}}
 meta=await Enricher(ctx,n.HOST_OPS).get('playback.started',data)
 body,_,_=n.render('playback.started',{**data,'_meta':meta},cfg(playback=True))[0]
 assert '评分：' not in body['content'] and 'push_img_url' not in body


@pytest.mark.asyncio
async def test_partial_recipients_not_retried(monkeypatch,caplog):
 calls=[]
 def respond(request):
  calls.append(request)
  return httpx.Response(200,json={'access_token':'token','expires_in':7200}) if request.url.path.endswith('gettoken') else httpx.Response(200,json={'errcode':0,'invaliduser':'someone','msgid':'msg1'})
 monkeypatch.setattr(n.net,'http_transport',lambda _:httpx.MockTransport(respond))
 result=await WecomClient(cfg(),logging.getLogger('test')).send({'title':'标题','content':'正文'})
 assert result['msgid']=='msg1' and len(calls)==2 and '部分接收人' in caplog.text


@pytest.mark.parametrize("media,units,guid", [
 ({"id":21,"kind":"tv"},[{"season":1,"episode":1}],"4d430400000000000000001500010001"),
 ({"id":21,"kind":"tv"},[],"4d430200000000000000001500000000"),
 ({"id":21,"kind":"movie"},[],"4d430200000000000000001500000000"),
 ({"id":None,"tmdb_id":21},[],None),
 ({"id":-1},[],None),
])
def test_public_jellyfin_images(media,units,guid):
 settings=cfg(movieclaw_url="http://media.example.com/")
 result=n.jellyfin_image(media,units,settings)
 assert result==("http://media.example.com/Items/"+guid+"/Images/Primary?maxWidth=1068" if guid else "")
 assert n.jellyfin_image(media,units,cfg())==""


def test_public_image_without_metadata():
 settings=cfg(movieclaw_url="http://media.example.com")
 body,_,_=n.render("subscription.fulfilled",{"media":{"id":21,"kind":"tv","title":"测试"},"units":[[1,1]]},settings)[0]
 assert "/Items/4d430400000000000000001500010001/Images/Primary" in body["push_img_url"]
 with pytest.raises(ValueError):cfg(movieclaw_url="https://user:password@example.com")
