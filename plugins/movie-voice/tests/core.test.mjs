import test from 'node:test';
import assert from 'node:assert/strict';
import {parseQuery,mergeCandidates,nativePath,shortcutURL} from '../web/core.mjs';
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
  assert.equal(result.length,3);assert.equal(result[0].title_ref,'tmdb:movie:438631');
  assert.equal(result.find(r=>r.title_ref==='tmdb:movie:841').owned,true);
  assert.equal(result.find(r=>r.title_ref==='douban:123').owned,false);
});
test('库存观看地址使用库和条目编号，拒绝无效来源引用',()=>{
  assert.equal(nativePath({library_id:1,media_item_id:2}),'/library/1/item/2');
  assert.equal(nativePath({title_ref:'tmdb:movie:841'}),'/media/movie/841');
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
