"""Opening hours and closures - when a store counts as closed, and what a
closed-hours caller hears (rinq/api/schedule.py, _handle_closed_call).

Shoalhaven's real hours (weekdays 9-4:30) are used, and the frozen clock is
moved to either side of them. Naive times are Sydney local.
"""

import re
from datetime import datetime

import pytest

from conftest import ROSIE_ANSWER_URL, STORE_HOURS, ring

PHIL = 'phil@watsonblinds.com.au'


def _is_open(twiml):
    return '<Enqueue' in twiml


@pytest.mark.parametrize('when, open_', [
    (datetime(2026, 10, 28, 8, 59), False),   # Wed, a minute before opening
    (datetime(2026, 10, 28, 9, 0), True),     # Wed, opening
    (datetime(2026, 10, 28, 16, 30), True),   # Wed, the closing minute still counts
    (datetime(2026, 10, 28, 16, 31), False),  # Wed, after closing
    (datetime(2026, 10, 31, 10, 30), False),  # Saturday
    (datetime(2026, 11, 1, 10, 30), False),   # Sunday
])
def test_store_hours(client, make_store, frozen_clock, when, open_):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    frozen_clock.set(when)
    assert _is_open(ring(client, store)) is open_


def test_after_hours_caller_gets_voicemail(client, make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert 'closed' in twiml.lower()
    assert '<Record' in twiml


def test_after_hours_never_reaches_stuart_even_on_a_stuart_store(client, make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist').with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_after_hours_to_stuart_skips_the_leave_a_message_recording(client, make_store, frozen_clock):
    """Derek 2026-10-28: after-hours callers at a Stuart store go to Stuart.
    He says the store's closed himself - the closed recording asks for a
    message, so it must not play first - and Tina tells him why (reason=closed)."""
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    audio_id = store.db.create_audio_file({'name': 'Closed, leave a message', 'file_type': 'closed',
                                           'file_url': '/audio/closed_leave_msg.mp3'}, 'test')
    store.db.update_call_flow(store.flow_id, {'closed_audio_id': audio_id}, 'test')
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL in twiml
    assert 'reason=closed' in twiml
    assert '<Record' not in twiml
    assert 'closed_leave_msg' not in twiml


def test_after_hours_stuart_says_the_store_welcome_himself(client, make_store, frozen_clock):
    """Derek 2026-10-29: Tina's recorded welcome then Stuart's voice is
    jarring - when the call goes straight to him, he says the welcome."""
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    welcome_id = store.db.create_audio_file({
        'name': 'Welcome Shoalhaven', 'file_type': 'greeting',
        'file_url': '/audio/welcome_shoal.mp3',
        'tts_text': 'Welcome to Watson Blinds and Awnings Shoalhaven.'}, 'test')
    store.db.update_call_flow(store.flow_id, {'greeting_audio_id': welcome_id}, 'test')
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL in twiml
    assert 'welcome_shoal' not in twiml
    assert 'welcome=Welcome+to+Watson+Blinds+and+Awnings+Shoalhaven' in twiml


def test_a_recorded_welcome_with_no_words_still_plays_before_stuart(client, make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    welcome_id = store.db.create_audio_file({
        'name': 'Welcome (uploaded)', 'file_type': 'greeting',
        'file_url': '/audio/welcome_upload.mp3'}, 'test')
    store.db.update_call_flow(store.flow_id, {'greeting_audio_id': welcome_id}, 'test')
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert 'welcome_upload' in twiml
    assert 'welcome=' not in twiml


def test_closed_action_message_plays_and_hangs_up(client, make_store):
    store = make_store(open=False, closed_action='message').with_staff(PHIL)
    twiml = ring(client, store)
    assert '<Hangup/>' in twiml
    assert '<Record' not in twiml


def test_closed_action_forward_dials_the_forward_number(client, make_store):
    store = make_store(open=False, closed_action='forward').with_staff(PHIL)
    store.db.update_call_flow(store.flow_id, {'closed_forward_number': '+61400999888'}, 'test')
    twiml = ring(client, store)
    assert '<Number>+61400999888</Number>' in twiml
    assert '<Record' not in twiml


def test_public_holiday_closes_an_open_day(client, make_store):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Show day', '2026-10-28', False, 'test')
    twiml = ring(client, store)
    assert not _is_open(twiml)
    assert '<Record' in twiml


def test_recurring_holiday_matches_every_year(client, make_store):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Every 28 Oct', '10-28', True, 'test')
    assert not _is_open(ring(client, store))


def test_holiday_on_another_day_changes_nothing(client, make_store):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Christmas Day', '2026-12-25', False, 'test')
    assert _is_open(ring(client, store))


@pytest.mark.parametrize('when, closed', [
    (datetime(2026, 10, 28, 12, 30), True),   # inside the weekly lunch closure
    (datetime(2026, 10, 28, 14, 0), False),   # after it
])
def test_weekly_closure_window(client, make_store, frozen_clock, when, closed):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Wednesday lunch', None, False, 'test',
                                  recurrence='weekly', day_of_week=2,
                                  start_time='12:00', end_time='13:00')
    frozen_clock.set(when)
    assert (not _is_open(ring(client, store))) is closed


@pytest.mark.xfail(strict=True, reason=(
    "BUG: real incoming calls (voice_incoming) use an older closed-hours branch that only "
    "reads the call flow's closed_action - a closure's own action/forward_to and the "
    "schedule's default_closure_* are ignored. Only the admin test-menu line "
    "(_handle_incoming_call_internal -> _handle_closed_call) honours them. No live closure "
    "depends on it today (all three that set an action say 'voicemail', same as the flow)."))
def test_holiday_action_overrides_the_call_flow(client, make_store):
    """A closure set to 'message' hangs up even though the flow says voicemail."""
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Stocktake', '2026-10-28', False, 'test',
                                  action='message')
    twiml = ring(client, store)
    assert '<Hangup/>' in twiml
    assert '<Record' not in twiml


@pytest.mark.xfail(strict=True, reason=(
    "BUG: same as above - voice_incoming ignores a closure's own forward_to, so a "
    "closure set to forward still plays the call flow's closed handling."))
def test_holiday_forward_overrides_the_call_flow(client, make_store):
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    store.db.add_schedule_holiday(store.schedule_id, 'Stocktake', '2026-10-28', False, 'test',
                                  action='forward', forward_to='+61400777666')
    assert '+61400777666' in ring(client, store)


def test_holiday_audio_is_played(client, make_store):
    """Live closures (Canberra Saturdays) rely on their own message playing."""
    store = make_store(hours=STORE_HOURS).with_staff(PHIL)
    audio_id = store.db.create_audio_file({'name': 'Closed Saturday', 'file_type': 'closed',
                                           'file_url': '/audio/closed_sat.mp3'}, 'test')
    store.db.add_schedule_holiday(store.schedule_id, 'Stocktake', '2026-10-28', False, 'test',
                                  audio_id=audio_id)
    twiml = ring(client, store)
    assert 'closed_sat.mp3' in twiml
    assert '<Record' in twiml


def test_stuart_is_told_who_from_the_team_rang_the_caller(client, make_store, frozen_clock):
    """1 in 8 callers open with "I had a missed call from you" (call scan,
    2026-10-29) - Stuart should know it was Phil, and when."""
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    store.db.upsert_user({'sid': 'USphil', 'username': 'phil', 'friendly_name': 'Phil Smith',
                          'staff_email': PHIL, 'is_active': 1, 'synced_at': '2026-10-28'})
    store.db.log_call({'call_sid': 'CAout', 'direction': 'outbound', 'from_number': store.number,
                       'to_number': '+61400111222', 'agent_email': PHIL, 'status': 'completed',
                       'started_at': _utc_minutes_ago(5)})
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    twiml = ring(client, store)
    assert 'called_by_name=Phil' in twiml
    assert 'called_by_email=phil%40watsonblinds.com.au' in twiml
    # "today at 3:15pm" - the date wording runs on the real clock
    assert re.search(r'called_by_when=(today|yesterday)\+at\+\d', twiml)


def _utc_minutes_ago(n):
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).strftime('%Y-%m-%d %H:%M:%S')


def test_nobody_rang_means_nothing_extra_for_stuart(client, make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    assert 'called_by' not in ring(client, store)
