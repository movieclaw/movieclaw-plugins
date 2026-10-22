import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shortcut_backend import card
from movie_match import candidates


class ShortcutCardsTest(unittest.TestCase):
    def test_identity_and_year_ranking(self):
        local={'items':[{'item':{'kind':'movie','title':'沙丘','year':1984,'tmdb_id':841,'library_id':1,'media_item_id':2}}]}
        remote={'titles':[{'media_type':'movie','title':'沙丘','release_year':2021,'title_ref':'tmdb:movie:438631'},
                          {'media_type':'movie','title':'沙丘','release_year':1984,'title_ref':'tmdb:movie:841','poster_url':'https://image.tmdb.org/t/p/w342/a.jpg'}]}
        result=candidates(local,remote,'沙丘 2021')
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['title_ref'],'tmdb:movie:438631')
        self.assertTrue(result[1]['owned'])
        self.assertTrue(result[1]['poster_url'].startswith('https://'))

    def test_vcard_cannot_inject_contact_fields(self):
        result=card({'title':'名字\nEND:VCARD\nBEGIN:VCARD','release_year':2021},'https://example.test/select?sig=fake','YWJj')
        self.assertEqual(result.count('\r\nBEGIN:VCARD'),0)
        self.assertIn('PHOTO;ENCODING=b;TYPE=JPEG:YWJj',result)
        for line in result.split('\r\n'):self.assertLessEqual(len(line.encode()),72)


if __name__=='__main__':unittest.main()
