"""仅解析用户明确指定的季号，不猜季数、不默认全选或追更。"""
import re


def parse_seasons(value):
    digits = dict(zip('零一二三四五六七八九两', [0,1,2,3,4,5,6,7,8,9,2]))
    def number(match):
        word = match[0]
        if '十' in word:
            left, right = word.split('十')
            return str((digits[left] if left else 1)*10 + (digits[right] if right else 0))
        return str(digits[word])
    value = re.sub(r'[零一二三四五六七八九两]?十[一二三四五六七八九]?|[零一二三四五六七八九两]', number, value.strip())
    value = value.replace('特别篇','0')
    if not re.fullmatch(r'\s*第?\s*\d{1,3}\s*季?(?:\s*[,，、和及]\s*第?\s*\d{1,3}\s*季?)*\s*', value):
        raise ValueError('请输入明确的季号，例如“第1季”或“1,2”；特别篇填0。不支持猜测或默认全部季。')
    seasons = sorted(set(int(n) for n in re.findall(r'\d+', value)))
    if len(seasons)>20 or any(n>999 for n in seasons):
        raise ValueError('一次最多选择20季。')
    return seasons


def season_label(seasons):
    return '、'.join('特别篇' if n==0 else f'第{n}季' for n in seasons)
