"""Minimal 5-field cron (minute hour day-of-month month day-of-week) for sync scheduling.

Supported syntax per field: ``*``, ``*/n``, ``a``, ``a-b``, ``a-b/n``, comma lists.
Day-of-week accepts 0-6 (Sunday=0) and 7 as Sunday. Ranges iterate minute-by-minute,
which is cheap for the once-a-second scheduling checks we do.
"""
import time
from datetime import datetime, timedelta

RANGES=((0,59),(0,23),(1,31),(1,12),(0,7))


def _field(text,low,high):
    values=set()
    for part in text.split(','):
        part=part.strip()
        if not part:raise ValueError('cron 字段存在空项')
        if '/' in part:
            base,step=part.split('/',1)
            try:step=int(step)
            except ValueError:raise ValueError('cron 步长必须为数字') from None
            if step<1:raise ValueError('cron 步长必须大于 0')
        else:base,step=part,1
        if base=='*':start,end=low,high
        elif '-' in base:
            a,b=base.split('-',1)
            try:start,end=int(a),int(b)
            except ValueError:raise ValueError('cron 范围必须为数字') from None
            if start>end:raise ValueError('cron 范围起点大于终点')
        else:
            try:start=end=int(base)
            except ValueError:raise ValueError('cron 值必须为数字') from None
        if start<low or end>high:raise ValueError(f'cron 值超出范围（{low}-{high}）')
        values.update(range(start,end+1,step))
    if high==7:  # day-of-week: 7 is an alias of Sunday (0)
        if 7 in values:values.discard(7);values.add(0)
    return values


class Cron:
    def __init__(self,expression):
        fields=expression.split()
        if len(fields)!=5:raise ValueError('cron 表达式必须为 5 个字段（分 时 日 月 周）')
        self.sets=[_field(text,*span) for text,span in zip(fields,RANGES)]
        self.expression=expression

    def matches(self,when):
        return (when.minute in self.sets[0] and when.hour in self.sets[1]
                and when.day in self.sets[2] and when.month in self.sets[3]
                and (when.weekday()+1)%7 in self.sets[4])

    def next_after(self,timestamp):
        """First fire time strictly after ``timestamp`` (epoch seconds, local time)."""
        moment=datetime.fromtimestamp(timestamp).replace(second=0,microsecond=0)+timedelta(minutes=1)
        # Search up to four years ahead; any sane schedule fires far earlier.
        for _ in range(4*366*24*60):
            if self.matches(moment):return moment.timestamp()
            moment+=timedelta(minutes=1)
        raise ValueError('cron 表达式在四年内没有触发时间')

    def next_runs(self,timestamp,count=5):
        runs=[];cursor=timestamp
        for _ in range(count):
            cursor=self.next_after(cursor)
            runs.append(cursor)
        return runs


def next_run_after(expression,timestamp):
    return Cron(expression).next_after(timestamp)
