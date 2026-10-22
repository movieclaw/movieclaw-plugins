import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from movie_voice import movie_snapshot


class NotificationIdentityTest(unittest.TestCase):
    def test_real_inventory_is_used(self):
        media=movie_snapshot({'media_item_id':2,'kind':'movie','title':'沙丘','year':2021,'file_count':1},2)
        self.assertEqual((media.id,media.title,media.year),(2,'沙丘',2021))

    def test_wrong_missing_or_tv_inventory_is_rejected(self):
        for patch in ({'media_item_id':3},{'kind':'tv'},{'file_count':0},{'title':''}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                movie_snapshot({'media_item_id':2,'kind':'movie','title':'沙丘','file_count':1,**patch},2)


if __name__=='__main__': unittest.main()
