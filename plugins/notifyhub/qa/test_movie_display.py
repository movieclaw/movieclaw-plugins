import pytest
import templates
from wecom_notify import render, Settings, units_of

@pytest.mark.parametrize('extra',[{'units':[[0,0]]},{'units':[{'season':0,'episode':0}]},{'unit':{'season':0,'episode':0}},{'files':[{'season':0,'episode':0}]},{'units':[{'season':1,'episode':2}]}])
def test_movie_never_renders_episode(extra):
 data={'media':{'id':1,'kind':'movie','title':'星际穿越','year':2014},'_meta':{'seasons':[0,1],'genres':['科幻'],'vote_average':9.4},**extra}
 body,_,units=render('library.ingest.completed',data,Settings())[0]
 assert units==[]
 assert all(x not in body['title']+body['content'] for x in ['季','S00','S01','E00'])
 assert '电影' in body['content'] and '9.4/10' in body['content']

@pytest.mark.parametrize('row',[{'season':0,'episode':0},{'season':1,'episode':None},{'season':True,'episode':1},{'season':-1,'episode':2},{'season':'1','episode':'2'},[1],None])
def test_invalid_units_hidden(row):
 assert units_of({'media':{'kind':'tv'},'units':[row]})==[]

def test_tv_special_and_ranges():
 d={'media':{'id':2,'kind':'tv','title':'剧集'},'units':[[0,1],[1,1],[1,2],[1,3],[1,5],[1,5]]}
 b,_,u=render('library.ingest.completed',d,Settings())[0]
 assert '特别篇' in b['title'] and '第 1 季' in b['title']
 assert 'S00E01' in b['content'] and 'S01E01–E03' in b['content'] and 'S01E05' in b['content']
 assert len(u)==5

def test_saved_defaults_upgrade_custom_template_preserved():
 d={'media':{'kind':'movie','title':'测试电影'},'unit':{'season':0,'episode':0}}
 old=Settings(import_title=templates.PREVIOUS_TITLE,import_text=templates.PREVIOUS_TEXT)
 new=render('library.ingest.completed',d,Settings())[0][0]
 assert render('library.ingest.completed',d,old)[0][0]==new
 custom=Settings(import_title='自定义：{{ title }}',import_text='{% if episode_fmt %}{{episode_fmt}}{% endif %}保留正文')
 body=render('library.ingest.completed',d,custom)[0][0]
 assert body['title']=='自定义：测试电影' and body['content']=='保留正文'
