"""The weekly Tina-vs-Twilio check email (rinq/services/cross_check.py).

The 9 Nov 2026 email flagged 25 "missing" calls that were all one outage
(8:33-9:21am Mon 2 Nov), but it printed them in UTC ("2026-11-01 22:19" -
apparently the day before the week it checked), in SID order (hiding the
cluster), and counted four hand-made test rows as calls Twilio "lost".
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytz

from rinq.services import cross_check

WEEK = (date(2026, 11, 2), date(2026, 11, 8))


def _sid(n):
    return 'CA' + f'{n:032x}'


def _twilio_call(sid, to, start_utc, frm='+61400000355'):
    return SimpleNamespace(sid=sid, to=to, _from=frm, direction='inbound', status='busy',
                           start_time=pytz.UTC.localize(start_utc))


def _log_inbound(db, sid, started_utc):
    with db._get_conn() as conn:
        conn.execute("INSERT INTO call_log (call_sid, direction, from_number, to_number, status, started_at)"
                     " VALUES (?, 'inbound', '+61400000001', '+61244180000', 'answered', ?)",
                     (sid, started_utc))


def _run(store, monkeypatch, calls):
    import rinq.services.twilio_service as ts
    monkeypatch.setattr(ts, 'twilio_list', lambda *a, **k: calls)
    return cross_check.run(store.db, SimpleNamespace(calls=None), *WEEK)


def _incoming(result):
    return next(c for c in result['checks'] if c['name'] == 'Incoming calls')


def test_missing_calls_show_sydney_time_in_time_order(make_store, monkeypatch):
    store = make_store()
    # Twilio order (and SID order) deliberately NOT time order
    calls = [_twilio_call(_sid(1), store.number, datetime(2026, 11, 1, 22, 19, 13)),
             _twilio_call(_sid(2), store.number, datetime(2026, 11, 1, 21, 41, 44))]
    check = _incoming(_run(store, monkeypatch, calls))
    assert check['examples'] == [
        f"missing: {_sid(2)} at Mon 2 Nov 8:41am from …355",
        f"missing: {_sid(1)} at Mon 2 Nov 9:19am from …355",
    ]


def test_test_rows_in_call_log_are_not_counted_as_extra(make_store, monkeypatch):
    store = make_store()
    _log_inbound(store.db, _sid(3), '2026-11-02 01:00:00')
    for probe in ('CAclaudeprobe', 'CAclaudeprobe-b1', 'CAcustomer1'):
        _log_inbound(store.db, probe, '2026-11-02 01:00:00')
    calls = [_twilio_call(_sid(3), store.number, datetime(2026, 11, 2, 1, 0, 0))]
    check = _incoming(_run(store, monkeypatch, calls))
    assert check['ok'] is True
    assert check['examples'] == []
    assert "Tina recorded 1." in check['summary']


def test_a_real_extra_call_is_still_reported_in_sydney_time(make_store, monkeypatch):
    store = make_store()
    _log_inbound(store.db, _sid(4), '2026-11-04 22:07:54')
    for n in (5, 6):
        _log_inbound(store.db, _sid(n), '2026-11-03 02:00:00')
    check = _incoming(_run(store, monkeypatch, []))
    assert check['ok'] is False
    assert check['examples'][0] == f"extra: {_sid(5)} at Tue 3 Nov 1:00pm (answered)"
    assert check['examples'][-1] == f"extra: {_sid(4)} at Thu 5 Nov 9:07am (answered)"


def test_local_time_format():
    assert cross_check._local('2026-11-02 00:05:00') == 'Mon 2 Nov 11:05am'
    assert cross_check._local('2026-11-02 01:30:00') == 'Mon 2 Nov 12:30pm'
    assert cross_check._local('2026-11-01 13:00:00') == 'Mon 2 Nov 12:00am'
    assert cross_check._local('not a date') == 'not a date'
