"""Country/decade constraints apply to sources, cache, queues and restored walls."""
import copy
import unittest
from pathlib import Path
from html.parser import HTMLParser
from test_personal import personal, FakeDiscovery, FakeOps, FakePrivateSDK, raw_row

class WallFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.discovery = FakeDiscovery()
        self.service = personal.PersonalRecommendations(self.discovery, private_store=FakePrivateSDK())
        self.token = (await self.service.create_profile())["profile_token"]
    async def test_country_and_decade_are_combined_hard_constraints_without_changing_taste(self):
        before = await self.service.get_profile(self.token)
        result = await self.service.recommend(self.token, "korean90", limit=12, country="KR", era="1990s", media_type="tv")
        self.assertEqual(len(result["items"]), 12)
        self.assertTrue(all(i["media_type"] == "tv" and 1990 <= i["year"] <= 1999 and "KR" in i["metadata"]["origin_country"] for i in result["items"]))
        self.assertEqual(result["filters"], {"media_type":"tv", "era":"1990s", "country":"KR"})
        self.assertTrue(all(p["with_origin_country"] == "KR" and p["first_air_date.gte"] == "1990-01-01" and p["first_air_date.lte"] == "1999-12-31" for _,p,_ in self.discovery.calls))
        after = await self.service.get_profile(self.token)
        self.assertEqual(after["answers"], before["answers"])
        self.assertEqual(after["revision"], before["revision"])
    async def test_country_separates_nonce_cache_and_prepare_options(self):
        japan = await self.service.recommend(self.token, "same", limit=6, country="JP", era="1980s")
        korea = await self.service.recommend(self.token, "same", limit=6, country="KR", era="1980s")
        self.assertNotEqual(japan["batch_id"], korea["batch_id"])
        self.assertTrue(all("KR" in i["metadata"]["origin_country"] for i in korea["items"]))
        first = await self.service.prepare_wall(self.token, nonce="a", limit=6, country="JP", era="1980s")
        second = await self.service.prepare_wall(self.token, nonce="b", limit=6, country="KR", era="1980s")
        with self.assertRaises(personal.PersonalProfileError):
            await self.service.commit_wall(self.token, first["prepared_ticket"])
        shown = await self.service.commit_wall(self.token, second["prepared_ticket"])
        self.assertTrue(all("KR" in i["metadata"]["origin_country"] for i in shown["items"]))
    async def test_resume_requires_same_country_era_and_size(self):
        shown = await self.service.recommend(self.token,"restore",limit=6,country="JP",era="1970s")
        restored = await self.service.resume_wall(self.token,limit=6,country="JP",era="1970s")
        self.assertEqual(restored["items"][0]["detail_ref"],shown["items"][0]["detail_ref"])
        self.assertEqual((await self.service.resume_wall(self.token,limit=6,country="KR",era="1970s"))["items"],[])
        self.assertEqual((await self.service.resume_wall(self.token,limit=6,country="JP",era="1980s"))["items"],[])
    async def test_boundaries_and_invalid_options(self):
        for era, lower, upper in [("1930s",1930,1939),("2020s",2020,2029),("before1930",1880,1929),("classic",1880,1999),("recent",2010,2100)]:
            result=await self.service.recommend(self.token,era,limit=6,era=era)
            self.assertEqual(len(result["items"]),6)
            self.assertTrue(all(lower <= i["year"] <= upper for i in result["items"]))
        for options in [{"country":"ZZ"},{"country":"jp"},{"country":"JP|KR"},{"era":"1999s"},{"era":"2030s"}]:
            with self.assertRaises(personal.PersonalProfileError):await self.service.recommend(self.token,"bad",**options)
    async def test_host_uses_supported_exact_year_argument_and_country(self):
        ops = FakeOps()
        async def getter():return ops
        service=personal.PersonalRecommendations(FakeDiscovery(token=""),getter,FakePrivateSDK())
        token=(await service.create_profile())["profile_token"]
        result=await service.recommend(token,"host",limit=6,country="JP",era="1980s")
        self.assertTrue(result["items"])
        self.assertTrue(all(i["year"]==1980 and "JP" in i["metadata"]["origin_country"] for i in result["items"]))
        self.assertTrue(all(p["origin_country"] == "JP" and 1980 <= p["year"] <= 1989 for _,p in ops.calls))
    def test_html_options_match_backend_and_decades_are_complete(self):
        class Options(HTMLParser):
            def __init__(self):super().__init__();self.select=None;self.values={"wall-country":["all"],"wall-era":["all"]}
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=="select":self.select=attrs.get("id");self.values[self.select]=[]
                if tag=="input" and attrs.get("name") in {"wall-country-choice","wall-era-choice"}:self.values[attrs["name"].replace("-choice", "")].append(attrs["value"])
                if tag=="option" and self.select:self.values[self.select].append(attrs.get("value"))
            def handle_endtag(self,tag):
                if tag=="select":self.select=None
        parser=Options();parser.feed((Path(__file__).parents[1]/"platform_discovery/assets/page.html").read_text())
        self.assertEqual(set(parser.values["wall-country"]),set(personal.WALL_COUNTRIES))
        self.assertEqual(set(parser.values["wall-era"]),set(personal.WALL_ERAS))
