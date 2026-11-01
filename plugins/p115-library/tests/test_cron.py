"""cronexpr parsing and scheduling tests."""
import pytest
from datetime import datetime
from cronexpr import Cron,next_run_after

def ts(y=2026,m=10,d=13,h=23,mi=40,s=0):
    return datetime(y,m,d,h,mi,s).timestamp()

def test_parse_every_20_minutes_daytime():
    cron=Cron('*/20 8-23 * * *')
    runs=[datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M') for t in cron.next_runs(ts(mi=20),3)]
    assert runs==['2026-10-13 23:40','2026-10-14 08:00','2026-10-14 08:20']

def test_fixed_time_and_weekday():
    cron=Cron('30 6 * * 1')  # Monday 06:30
    first=datetime.fromtimestamp(cron.next_after(ts(d=13)))  # 2026-10-13 is Tuesday
    assert first.weekday()==0 and (first.hour,first.minute)==(6,30)

def test_lists_and_steps():
    cron=Cron('0,30 0-23/6 1,15 * *')
    runs=cron.next_runs(ts(d=1,h=0,mi=0),3)
    assert [datetime.fromtimestamp(t).strftime('%d %H:%M') for t in runs]==['01 00:30','01 06:00','01 06:30']

def test_sunday_alias():
    assert Cron('0 0 * * 7').sets[4]=={0}
    assert Cron('0 0 * * 0').sets[4]=={0}

def test_invalid_expressions():
    for expr in ('* * * *','a b c d e','*/0 * * * *','70 * * * *','5-1 * * * *','* * * * * *'):
        with pytest.raises(ValueError):Cron(expr)

def test_next_run_after_helper():
    assert next_run_after('* * * * *',ts())==ts(mi=41)
