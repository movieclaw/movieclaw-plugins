// 用来源编号合并库存与资料；同名、同年份也不能代替稳定编号。
export function parseQuery(raw) {
  const value = raw.trim().slice(0, 180);
  const match = value.match(/^(.*?)\s*[（(\s]((?:18|19|20|21)\d{2})[）)]?\s*$/u);
  return match && match[1].trim() ? {query:match[1].trim(), year:Number(match[2])} : {query:value, year:null};
}
export function titleKey(row) { return row.title_ref || `library:${row.library_id}:${row.media_item_id}`; }
export function nativePath(row) {
  if (row.library_id && row.media_item_id) return `/library/${Number(row.library_id)}/item/${Number(row.media_item_id)}`;
  const parts = /^(tmdb):(movie):([1-9]\d*)$/.exec(row.title_ref || '');
  if (parts) return `/media/movie/${parts[3]}`;
  const douban = /^douban:([1-9]\d*)$/.exec(row.title_ref || '');
  return douban ? `/media/douban/${douban[1]}` : '/';
}
export function mergeCandidates(library, titles, raw) {
  const {query, year} = parseQuery(raw), rows = new Map();
  for (const hit of library || []) {
    const item = hit.item;
    if (item?.kind !== 'movie') continue;
    const row = {...item, title_ref:item.tmdb_id ? `tmdb:movie:${item.tmdb_id}` : null,
      release_year:item.year, owned:true, library_id:item.library_id || hit.library_ids?.[0]};
    rows.set(titleKey(row), row);
  }
  for (const item of titles || []) {
    if (item.media_type !== 'movie') continue;
    const key = titleKey(item), previous = rows.get(key);
    rows.set(key, previous ? {...item, ...previous} : {...item, owned:false});
  }
  const normalized = s => String(s || '').normalize('NFKC').toLowerCase().replace(/[\s\p{P}]/gu, '');
  const score = row => (year && row.release_year === year ? 1000 : 0)
    + (normalized(row.title) === normalized(query) ? 200 : 0) + (row.owned ? 20 : 0);
  return [...rows.values()].sort((a,b) => score(b)-score(a));
}

// 仅接受插件专用签名搜索链接，绝不将管理员令牌或其他 URL 当作配置。
export function shortcutURL(value) {
  if (typeof value !== 'string') throw new Error('服务器没有返回 SEARCH_URL，请稍后重试');
  let url;
  try { url = new URL(value); } catch { throw new Error('服务器返回的 SEARCH_URL 格式不正确'); }
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.hash ||
      url.pathname !== '/api/v1/plugins/movie-voice/shortcut/search' ||
      !url.searchParams.get('epoch') || !url.searchParams.get('sig'))
    throw new Error('服务器返回的 SEARCH_URL 格式不正确');
  return value;
}
