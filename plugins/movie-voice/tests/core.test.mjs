import test from 'node:test';
import assert from 'node:assert/strict';
import {parseQuery,mergeCandidates,nativePath,shortcutURL,mediaTypeLabel} from '../web/core.mjs';
test('年份消歧与数字片名',()=>{
  assert.deepEqual(parseQuery('沙丘（2021）'),{query:'沙丘',year:2021});
  assert.deepEqual(parseQuery('2001太空漫游'),{query:'2001太空漫游',year:null});
  assert.deepEqual(parseQuery('1917'),{query:'1917',year:null});
});
test('同名不同编号不合并，跨来源同名不擅自匹配',()=>{
  const local=[{item:{kind:'movie',title:'沙丘',year:1984,tmdb_id:841,media_item_id:10,library_id:1}}];
  const titles=[{title_ref:'tmdb:movie:438631',media_type:'movie',title:'沙丘',release_year:2021},
    {title_ref:'tmdb:movie:841',media_type:'movie',title:'沙丘',release_year:1984},
    {title_ref:'douban:123',media_type:'movie',title:'沙丘',release_year:1984},
    {title_ref:'tmdb:tv:3',media_type:'tv',title:'沙丘',release_year:2021}];
  const result=mergeCandidates(local,titles,'沙丘 2021');
  assert.equal(result.length,4);assert.equal(result[0].title_ref,'tmdb:movie:438631');
  assert.equal(result.find(r=>r.title_ref==='tmdb:movie:841').owned,true);
  assert.equal(result.find(r=>r.title_ref==='douban:123').owned,false);
});
test('库存观看地址使用库和条目编号，拒绝无效来源引用',()=>{
  assert.equal(nativePath({library_id:1,media_item_id:2}),'/library/1/item/2');
  assert.equal(nativePath({title_ref:'tmdb:movie:841'}),'/media/movie/841');
  assert.equal(nativePath({title_ref:'tmdb:tv:841'}),'/media/tv/841');
  assert.equal(nativePath({title_ref:'javascript:alert(1)'}),'/');
});

test('配置只接受完整专用链接，保留原签名参数',()=>{
  const url='https://media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=EXAMPLE_EPOCH&sig=EXAMPLE_SIGNATURE';
  assert.equal(shortcutURL(url),url);
  for(const bad of [undefined,'https://media.example.com','javascript:alert(1)',
    'https://media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=x',
    'https://user:password@media.example.com/api/v1/plugins/movie-voice/shortcut/search?epoch=x&sig=y'])
    assert.throws(()=>shortcutURL(bad));
});

test('库内剧集正常显示，电影与剧集同号也不合并，未入库剧集也参与搜索',()=>{
  const local=[{item:{kind:'tv',title:'库内剧集',year:2026,tmdb_id:101,media_item_id:42,library_id:3}}];
  const titles=[{title_ref:'tmdb:tv:101',media_type:'tv',title:'库内剧集',release_year:2026},
    {title_ref:'tmdb:movie:101',media_type:'movie',title:'同号电影',release_year:2026},
    {title_ref:'tmdb:tv:202',media_type:'tv',title:'未入库剧集',release_year:2026}];
  const rows=mergeCandidates(local,titles,'库内剧集');
  assert.equal(rows.length,3);
  assert.equal(rows[0].title_ref,'tmdb:tv:101');
  assert.equal(rows[0].owned,true);
  assert.equal(mediaTypeLabel(rows[0]),'剧集');
  assert.equal(nativePath(rows[0]),'/library/3/item/42');
  assert.equal(rows.find(row=>row.title_ref==='tmdb:movie:101').owned,false);
});

test('豆瓣轻量结果类型未知时保留，不能误标为电影',()=>{
  const rows=mergeCandidates([],[{title_ref:'douban:123',title:'待核对剧集',media_type:null}], '待核对剧集');
  assert.equal(rows.length,1);assert.equal(mediaTypeLabel(rows[0]),'类型待核对');
});
