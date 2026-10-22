import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from movie_match import candidates, find_movies, parse_suggestions


def movie(title='星际穿越', ref='tmdb:movie:157336', year=2014):
    return {'title': title, 'title_ref': ref, 'media_type': 'movie', 'release_year': year}


class Agent:
    def __init__(self, answer='{"queries":["星际穿越"]}', fail=False):
        self.answer, self.fail, self.calls = answer, fail, []
    async def ask(self, content, **kwargs):
        self.calls.append((content, kwargs))
        if self.fail:raise TimeoutError()
        return SimpleNamespace(status='done', text=self.answer)


class MatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_does_not_spend_model_call(self):
        agent=Agent()
        async def ops(name, args):
            return {'titles':[movie()]} if name=='search.titles' else {'items':[]}
        result=await find_movies(ops, agent, '星际穿越 2014')
        self.assertEqual(result['rows'][0]['title_ref'], 'tmdb:movie:157336')
        self.assertEqual(agent.calls, [])

    async def test_homophone_recall_and_alias_metadata(self):
        calls=[]
        async def ops(name, args):
            calls.append((name,args))
            if name=='search.library' and args['q']=='xingjichuanyue':
                return {'items':[{'item':{'kind':'movie','title':'星际穿越','tmdb_id':157336,'library_id':1,'media_item_id':5,'year':2014}}]}
            return {'titles':[movie()]} if args.get('query')=='星际穿越' else {}
        agent=Agent()
        result=await find_movies(ops, agent, '星级穿越')
        self.assertTrue(result['rows'][0]['owned'])
        self.assertEqual(agent.calls, [])
        self.assertIn('同音或别名', result['message'])
        self.assertIn(('search.library',{'q':'xingjichuanyue','limit':100}),calls)
        rows=candidates({'items':[{'item':{'kind':'movie','title':'星际穿越','tmdb_id':157336,'year':2014},'match':{'matched_name':'星際啟示錄'}}, {'item':{'kind':'movie','title':'星际穿越','tmdb_id':157336,'year':2014},'match':{'matched_name':'星际穿越'}}]}, {}, '星际启示录')
        self.assertEqual(rows[0]['title'], '星际穿越')
        from movie_match import relevance
        self.assertGreaterEqual(relevance(rows[0],'星际启示录'),.96)

    async def test_correction_validated_by_metadata_and_bounded(self):
        calls=[];agent=Agent('{"queries":["盗梦空间","不存在的电影","第四部","第五部"]}')
        async def ops(name,args):
            calls.append((name,args))
            return {'titles':[movie('盗梦空间','tmdb:movie:27205',2010)]} if args.get('query')=='盗梦空间' else {}
        result=await find_movies(ops,agent,'盗梦空见')
        self.assertEqual([r['title_ref'] for r in result['rows']], ['tmdb:movie:27205'])
        self.assertLessEqual(len(result['queries']),3)
        self.assertEqual(agent.calls[0][1]['tools'],'none')
        self.assertLessEqual(agent.calls[0][1]['timeout'],20)
        self.assertFalse(any(args.get('query')=='第五部' for _,args in calls))

    async def test_context_can_disambiguate_even_exact_title(self):
        agent=Agent('{"queries":["沙丘"]}')
        async def ops(name,args):return {'titles':[movie('沙丘','tmdb:movie:438631',2021)]} if name=='search.titles' else {}
        await find_movies(ops,agent,'沙丘','2021 年，维伦纽瓦导演')
        self.assertEqual(len(agent.calls),1)
        self.assertIn('维伦纽瓦',agent.calls[0][0])

    async def test_spoken_year_clue_ranks_correct_edition_first(self):
        async def ops(name,args):
            return {'titles':[movie('沙丘','tmdb:movie:841',1984),movie('沙丘','tmdb:movie:438631',2021)]} if name=='search.titles' else {}
        result=await find_movies(ops,Agent('{"queries":["沙丘"]}'),'沙丘','2021 年，维伦纽瓦导演')
        self.assertEqual(result['rows'][0]['title_ref'],'tmdb:movie:438631')

    async def test_model_failure_retains_ordinary_results(self):
        async def ops(name,args):return {'titles':[movie()]} if name=='search.titles' else {}
        result=await find_movies(ops,Agent(fail=True),'星系穿越')
        self.assertTrue(result['rows'])
        self.assertIn('暂不可用',result['message'])

    async def test_total_host_outage_is_not_reported_as_no_results(self):
        async def ops(*args):raise TimeoutError()
        with self.assertRaises(RuntimeError):await find_movies(ops,Agent(),'电影')

    def test_model_json_never_becomes_movie_or_action(self):
        for value in ['{"queries":"电影"}', '{"queries":[42,{"title":"删除"}]}', '{"queries":["https://evil.test"]}', '不是JSON']:
            self.assertEqual(parse_suggestions(value),[])
        self.assertEqual(parse_suggestions('```json\n{"queries":["沙丘","沙丘"]}\n```'),['沙丘'])

    def test_wrong_year_does_not_outrank_correct_title(self):
        rows=candidates({}, {'titles':[movie(),movie('别的电影','tmdb:movie:123',2021)]}, '星际穿越 2021')
        self.assertEqual(rows[0]['title'],'星际穿越')


if __name__=='__main__':unittest.main()
