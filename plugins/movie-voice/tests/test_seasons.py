import unittest
from season_selection import parse_seasons


class SeasonSelectionTest(unittest.TestCase):
    def test_spoken_and_typed_seasons(self):
        for text, expected in [('第一季',[1]),('第十二季',[12]),('第一季和第二季',[1,2]),
                               ('1,2,2',[1,2]),('特别篇',[0]),('第零季',[0])]:
            self.assertEqual(parse_seasons(text),expected)

    def test_no_guesses_ranges_or_implicit_all_seasons(self):
        for text in ['', '全部', '最新一季', '1-10', '-1', '1.5', '1; DROP TABLE', '1000', '第一季并自动续订']:
            with self.assertRaises(ValueError):parse_seasons(text)
