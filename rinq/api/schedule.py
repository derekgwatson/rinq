"""Business hours and schedule helpers.

Extracted from routes.py. Handles checking if the business is currently
open and calculating the next opening time for closed-hours messaging.
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def get_next_open_time(schedule: dict, now, tz) -> dict | None:
    """Calculate when the business next opens based on the schedule.

    Checks up to 14 days ahead, accounting for business hours and closures.

    Returns:
        Dict with 'day_label' (e.g. 'tomorrow', 'Monday'),
        'time' (spoken, e.g. '8 30 AY EM'), and 'time_raw' (e.g. '8:30'),
        or None if no opening found.
    """
    business_hours_json = schedule.get('business_hours')
    if not business_hours_json:
        return None

    try:
        business_hours = json.loads(business_hours_json) if isinstance(business_hours_json, str) else business_hours_json
    except (json.JSONDecodeError, TypeError):
        return None

    if not business_hours:
        return None

    holidays = schedule.get('holidays', [])
    day_names = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

    for days_ahead in range(0, 15):
        check_date = now + timedelta(days=days_ahead)
        check_day = day_names[check_date.weekday()]
        check_date_str = check_date.strftime('%Y-%m-%d')
        check_mmdd = check_date.strftime('%m-%d')
        check_dow = check_date.weekday()

        # Check if this day is blocked by a closure/holiday
        is_closed_by_holiday = False
        for holiday in holidays:
            recurrence = holiday.get('recurrence', 'once')
            if recurrence == 'weekly':
                holiday_dow = holiday.get('day_of_week')
                if holiday_dow is not None and int(holiday_dow) == check_dow:
                    if not holiday.get('start_time') or not holiday.get('end_time'):
                        is_closed_by_holiday = True
                        break
            elif holiday.get('is_recurring'):
                if holiday.get('date') == check_mmdd:
                    is_closed_by_holiday = True
                    break
            else:
                if holiday.get('date') == check_date_str:
                    is_closed_by_holiday = True
                    break

        if is_closed_by_holiday:
            continue

        day_hours = business_hours.get(check_day)
        if not day_hours:
            continue

        open_time_str = day_hours.get('open', '').strip()
        if not open_time_str:
            continue

        # If today, only count if the opening time is still in the future
        if days_ahead == 0:
            current_time = now.strftime('%H:%M')
            if open_time_str.zfill(5) <= current_time:
                continue

        # Format the time for speech
        try:
            hour, minute = open_time_str.split(':')
            hour_int = int(hour)
            minute_int = int(minute)
            if hour_int < 12:
                period = 'AY EM'
                display_hour = hour_int if hour_int != 0 else 12
            elif hour_int == 12:
                period = 'PEE EM'
                display_hour = 12
            else:
                period = 'PEE EM'
                display_hour = hour_int - 12
            if minute_int == 0:
                time_spoken = f'{display_hour} {period}'
            else:
                time_spoken = f'{display_hour} {minute_int:02d} {period}'
        except (ValueError, AttributeError):
            time_spoken = open_time_str

        if days_ahead == 0:
            day_label = 'later today'
        elif days_ahead == 1:
            day_label = 'tomorrow'
        else:
            day_label = check_date.strftime('%A')

        return {'day_label': day_label, 'time': time_spoken, 'time_raw': open_time_str,
                'date': check_date.strftime('%Y-%m-%d')}

    return None


def next_open_for_stuart(next_open: dict | None) -> str:
    """When the store next opens, written for Stuart to say - "tomorrow
    (Saturday) at 9am", "Monday 5 January at 9am". Tina's schedule knows the
    holidays and closures; Stuart's own copy of the hours doesn't, so he uses
    this when it's sent (Derek, 2026-10-30). "" when there's nothing to send."""
    if not next_open or not next_open.get('date') or not next_open.get('time_raw'):
        return ""
    try:
        when = datetime.strptime(next_open['date'], '%Y-%m-%d')
        hour, minute = (int(x) for x in next_open['time_raw'].split(':'))
    except (ValueError, TypeError):
        return ""
    time_text = f"{hour % 12 or 12}{f':{minute:02d}' if minute else ''}{'am' if hour < 12 else 'pm'}"
    weekday = when.strftime('%A')
    label = next_open.get('day_label')
    if label in ('later today', 'tomorrow'):
        return f"{label} ({weekday}) at {time_text}"
    return f"{weekday} {when.day} {when.strftime('%B')} at {time_text}"


def check_business_status(schedule: dict) -> dict:
    """Check if we're currently within business hours.

    Returns:
        Dict with:
            - is_open: True if currently open
            - matched_holiday: The matched holiday dict if closed, None otherwise
            - reason: 'open', 'holiday', 'after_hours', or 'day_closed'
            - next_open: Dict with 'day_label' and 'time' if closed
    """
    import pytz

    if not schedule:
        return {'is_open': True, 'matched_holiday': None, 'reason': 'open', 'next_open': None}

    tz_name = schedule.get('timezone', 'Australia/Sydney')
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone('Australia/Sydney')

    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    today_mmdd = now.strftime('%m-%d')
    day_name = now.strftime('%a').lower()
    current_day_of_week = now.weekday()
    current_time = now.strftime('%H:%M')

    holidays = schedule.get('holidays', [])

    def closure_priority(h):
        rec = h.get('recurrence', 'once')
        if rec == 'weekly':
            return 2
        elif h.get('is_recurring'):
            return 1
        return 0

    for holiday in sorted(holidays, key=closure_priority):
        recurrence = holiday.get('recurrence', 'once')

        if recurrence == 'weekly':
            holiday_dow = holiday.get('day_of_week')
            if holiday_dow is not None and int(holiday_dow) == current_day_of_week:
                start_time = holiday.get('start_time')
                end_time = holiday.get('end_time')
                if start_time and end_time:
                    if start_time.zfill(5) <= current_time <= end_time.zfill(5):
                        logger.info(f"Weekly closure match: {holiday.get('name')} ({start_time}-{end_time})")
                        next_open = get_next_open_time(schedule, now, tz)
                        return {'is_open': False, 'matched_holiday': holiday, 'reason': 'holiday', 'next_open': next_open}
                else:
                    logger.info(f"Weekly closure match (all day): {holiday.get('name')}")
                    next_open = get_next_open_time(schedule, now, tz)
                    return {'is_open': False, 'matched_holiday': holiday, 'reason': 'holiday', 'next_open': next_open}

        elif holiday.get('is_recurring'):
            if holiday.get('date') == today_mmdd:
                logger.info(f"Holiday match (recurring): {holiday.get('name')}")
                next_open = get_next_open_time(schedule, now, tz)
                return {'is_open': False, 'matched_holiday': holiday, 'reason': 'holiday', 'next_open': next_open}
        else:
            if holiday.get('date') == today_str:
                logger.info(f"Holiday match (specific): {holiday.get('name')}")
                next_open = get_next_open_time(schedule, now, tz)
                return {'is_open': False, 'matched_holiday': holiday, 'reason': 'holiday', 'next_open': next_open}

    business_hours_json = schedule.get('business_hours')
    if not business_hours_json:
        next_open = get_next_open_time(schedule, now, tz)
        return {'is_open': False, 'matched_holiday': None, 'reason': 'day_closed', 'next_open': next_open}

    try:
        business_hours = json.loads(business_hours_json) if isinstance(business_hours_json, str) else business_hours_json
    except (json.JSONDecodeError, TypeError):
        return {'is_open': False, 'matched_holiday': None, 'reason': 'day_closed', 'next_open': None}

    day_hours = business_hours.get(day_name)
    if not day_hours:
        next_open = get_next_open_time(schedule, now, tz)
        return {'is_open': False, 'matched_holiday': None, 'reason': 'day_closed', 'next_open': next_open}

    open_time = day_hours.get('open', '00:00').zfill(5)
    close_time = day_hours.get('close', '23:59').zfill(5)

    is_open = open_time <= current_time <= close_time
    logger.info(f"Business hours check: {day_name} {current_time}, open={open_time}-{close_time}, is_open={is_open}")

    if is_open:
        return {'is_open': True, 'matched_holiday': None, 'reason': 'open', 'next_open': None}
    else:
        next_open = get_next_open_time(schedule, now, tz)
        return {'is_open': False, 'matched_holiday': None, 'reason': 'after_hours', 'next_open': next_open}
