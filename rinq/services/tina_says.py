"""Tina Says — a daily, slightly cheeky one-liner built from Tina's own call data.

How it hangs together:

1. compute_facts() works out a handful of TRUE things from call_log with plain
   SQL: yesterday's volume against the usual, who answered the most, the
   morning-rush forecast, and so on. Every number Tina ever says comes from here.
2. Once a day, if ANTHROPIC_API_KEY is set, Claude turns those facts into lines
   in Tina's voice. It is handed only first names and totals — never customer
   numbers, names or anything said on a call.
3. Every Claude line is checked: each number in it must be one of that fact's
   numbers, and the only staff name allowed is the one the fact is about. A
   line that fails is dropped. If Claude is unavailable, or a fact ends up with
   no line, the built-in template lines are used instead.

Rules the facts follow (see the 2026-10-13 reports audit):
- Only counted from TRUST_FROM onwards; earlier records were unreliable.
- Callers are counted once each (get_caller_outcomes), not per call leg.
- Staff are named only for good news. Hang-ups and misses stay team-level.

The day's lines are generated lazily by the first phone-page load, in a
background thread, and stored in tina_says_lines.
"""

import json
import logging
import os
import random
import re
import threading
from datetime import date, datetime, time, timedelta

import pytz
import requests

logger = logging.getLogger(__name__)

LOCAL_TZ = pytz.timezone('Australia/Sydney')

# Records ("busiest Monday since ...") never look before this date.
TRUST_FROM = date(2026, 8, 1)

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-sonnet-5-5'
LLM_TIMEOUT_SECONDS = 90

# A "business day" for "yesterday" purposes: at least this many callers
MIN_DAY_CALLERS = 20

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def _utc_range(start: date, end: date) -> tuple[str, str]:
    """UTC timestamps covering local dates start..end inclusive."""
    s = LOCAL_TZ.localize(datetime.combine(start, time.min)).astimezone(pytz.UTC)
    e = LOCAL_TZ.localize(datetime.combine(end, time(23, 59, 59))).astimezone(pytz.UTC)
    return s.strftime('%Y-%m-%d %H:%M:%S'), e.strftime('%Y-%m-%d %H:%M:%S')


def _first_name(name_or_email: str) -> str:
    if not name_or_email:
        return ''
    if '@' in name_or_email:
        name_or_email = name_or_email.split('@')[0].replace('.', ' ')
    return name_or_email.strip().split()[0].capitalize()


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def _callers(db, day: date) -> dict:
    return db.get_caller_outcomes(*_utc_range(day, day))


def _last_business_day(db, today: date) -> date | None:
    """Most recent day before today with a real day's worth of callers."""
    for back in range(1, 8):
        d = today - timedelta(days=back)
        if d < TRUST_FROM:
            return None
        if d.weekday() < 5 and _callers(db, d)['callers'] >= MIN_DAY_CALLERS:
            return d
    return None


def _day_label(day: date, today: date) -> tuple[str, str]:
    """('yesterday', 'Yesterday') or ('on Friday', 'Friday')."""
    if day == today - timedelta(days=1):
        return 'yesterday', 'Yesterday'
    wd = WEEKDAYS[day.weekday()]
    return f'on {wd}', wd


def _same_weekdays_before(day: date, weeks: int) -> list[date]:
    return [day - timedelta(weeks=w) for w in range(1, weeks + 1)
            if day - timedelta(weeks=w) >= TRUST_FROM]


def compute_facts(db, staff: list[dict], today: date = None) -> list[dict]:
    """True, checkable things worth a line. Each fact:

        id      — stable key, e.g. 'top_answerer'
        kind    — 'team' | 'person' (a named, good-news fact) | 'forecast'
        summary — one plain sentence stating the fact (for Claude and templates)
        numbers — every number a line about this fact may contain
        name    — the staff first name a line may use (person facts only)
        score   — how interesting, 0..1
    """
    today = today or local_today()
    names = {s['email'].lower(): s.get('name') or s['email'] for s in staff if s.get('email')}
    staff_emails = list(names)
    facts = []

    day = _last_business_day(db, today)
    if day:
        label, subject = _day_label(day, today)
        wd = WEEKDAYS[day.weekday()]
        c = _callers(db, day)

        # 1. Volume against the usual for that weekday
        prior = [_callers(db, d)['callers'] for d in _same_weekdays_before(day, 4)]
        prior = [p for p in prior if p >= MIN_DAY_CALLERS]
        if prior:
            usual = round(sum(prior) / len(prior))
            diff_pct = round((c['callers'] - usual) / usual * 100) if usual else 0
            busier = 'busier' if diff_pct > 0 else 'quieter'
            facts.append({
                'id': 'volume', 'kind': 'team',
                'summary': (f"{c['callers']} customers rang in {label}; a usual {wd} lately is "
                            f"about {usual}, so it was {abs(diff_pct)}% {busier} than normal."
                            if abs(diff_pct) >= 5 else
                            f"{c['callers']} customers rang in {label}, right on a usual {wd} (about {usual})."),
                'numbers': [c['callers'], usual, abs(diff_pct)],
                'vars': {'n': c['callers'], 'usual': usual, 'pct': abs(diff_pct), 'busier': busier,
                         'label': label, 'wd': wd, 'flat': abs(diff_pct) < 5},
                'score': min(1.0, 0.35 + abs(diff_pct) / 40),
            })

        # 2. Record weekday since TRUST_FROM
        history = [(d, _callers(db, d)['callers']) for d in _same_weekdays_before(day, 26)]
        history = [h for h in history if h[1] >= MIN_DAY_CALLERS]
        if len(history) >= 3:
            if c['callers'] > max(n for _, n in history):
                facts.append({
                    'id': 'record_busy', 'kind': 'team',
                    'summary': (f"{subject} was our busiest {wd} since "
                                f"{TRUST_FROM.strftime('%B')}: {c['callers']} customers rang in."),
                    'numbers': [c['callers']], 'score': 0.95,
                })
            elif c['callers'] < min(n for _, n in history):
                facts.append({
                    'id': 'record_quiet', 'kind': 'team',
                    'summary': (f"{subject} was our quietest {wd} since "
                                f"{TRUST_FROM.strftime('%B')}: only {c['callers']} customers rang in."),
                    'numbers': [c['callers']], 'score': 0.8,
                })

        # 3-5. Named good news: most answered, most outbound, longest call
        agents = db.get_call_log_by_agent(*_utc_range(day, day), team_emails=staff_emails) if staff_emails else []
        ranked = sorted(agents, key=lambda a: a.get('inbound_answered') or 0, reverse=True)
        if ranked and (ranked[0].get('inbound_answered') or 0) >= 10:
            top = ranked[0]
            n = top['inbound_answered']
            facts.append({
                'id': 'top_answerer', 'kind': 'person',
                'summary': f"{_first_name(names.get(top['agent_email'].lower(), top['agent_email']))} "
                           f"answered the most customer calls {label}: {n}.",
                'numbers': [n], 'name': _first_name(names.get(top['agent_email'].lower(), top['agent_email'])),
                'vars': {'label': label},
                'score': 0.7,
            })
        ranked = sorted(agents, key=lambda a: a.get('outbound_calls') or 0, reverse=True)
        if ranked and (ranked[0].get('outbound_calls') or 0) >= 15:
            top = ranked[0]
            n = top['outbound_calls']
            facts.append({
                'id': 'top_dialler', 'kind': 'person',
                'summary': f"{_first_name(names.get(top['agent_email'].lower(), top['agent_email']))} "
                           f"made the most outgoing calls {label}: {n}.",
                'numbers': [n], 'name': _first_name(names.get(top['agent_email'].lower(), top['agent_email'])),
                'vars': {'label': label},
                'score': 0.65,
            })
        longest = _longest_call(db, day, staff_emails)
        if longest and longest[1] >= 20:
            who = _first_name(names.get(longest[0].lower(), longest[0]))
            facts.append({
                'id': 'marathon', 'kind': 'person',
                'summary': f"The longest customer call {label} was {who}'s, at {longest[1]} minutes.",
                'numbers': [longest[1]], 'name': who, 'vars': {'label': label}, 'score': 0.6,
            })

        # 6. Quietest open hour (team-level)
        open_hours = [h for h in c['hourly'] if 9 <= h['hour'] <= 15]
        if open_hours and c['callers'] >= 60:
            q = min(open_hours, key=lambda h: h['total_calls'])
            busy = max(open_hours, key=lambda h: h['total_calls'])
            if busy['total_calls'] >= 3 * max(q['total_calls'], 1):
                facts.append({
                    'id': 'quiet_hour', 'kind': 'team',
                    'summary': (f"{subject} the quietest hour was {_hour_name(q['hour'])} with "
                                f"{q['total_calls']} calls, while {_hour_name(busy['hour'])} had {busy['total_calls']}."),
                    'numbers': [q['total_calls'], busy['total_calls']] + _hour_numbers(q['hour']) + _hour_numbers(busy['hour']),
                    'score': 0.45,
                })

    # 7. Morning-rush forecast for today, from the last 4 same weekdays
    if today.weekday() < 5:
        mornings, peaks = [], {}
        for d in _same_weekdays_before(today, 4):
            c = _callers(db, d)
            if c['callers'] < MIN_DAY_CALLERS:
                continue
            mornings.append(sum(h['total_calls'] for h in c['hourly'] if h['hour'] < 12))
            for h in c['hourly']:
                peaks[h['hour']] = peaks.get(h['hour'], 0) + h['total_calls']
        if len(mornings) >= 2 and peaks:
            avg_morning = round(sum(mornings) / len(mornings))
            peak = max(peaks, key=peaks.get)
            wd = WEEKDAYS[today.weekday()]
            facts.append({
                'id': 'forecast', 'kind': 'forecast',
                'summary': (f"Forecast: on a {wd} we usually get about {avg_morning} calls before noon, "
                            f"and the busiest hour is usually {_hour_name(peak)}."),
                'numbers': [avg_morning] + _hour_numbers(peak) + [12],
                'vars': {'n': avg_morning, 'peak': _hour_name(peak), 'wd': wd},
                'score': 0.55,
            })

    # 8. Team talk time over the last 7 days, with a precomputed comparison
    week_start = today - timedelta(days=7)
    if week_start >= TRUST_FROM and staff_emails:
        agents = db.get_call_log_by_agent(*_utc_range(week_start, today - timedelta(days=1)),
                                          team_emails=staff_emails)
        hours = round(sum(a['total_duration_seconds'] for a in agents) / 3600)
        if hours >= 5:
            drives = round(hours / 3)  # Canberra to Sydney is about 3 hours by car
            facts.append({
                'id': 'talk_time', 'kind': 'team',
                'summary': (f"Over the last 7 days the team spent {hours} hours talking to customers on the phone "
                            f"— about {drives} drives from Canberra to Sydney (3 hours each)."),
                'numbers': [hours, drives, 7, 3],
                'vars': {'hours': hours, 'drives': drives},
                'score': 0.4,
            })

    # 9. Milestones: total callers since TRUST_FROM crossing a round number yesterday
    if day:
        total_to = db.get_caller_outcomes(*_utc_range(TRUST_FROM, day))['callers']
        total_before = total_to - _callers(db, day)['callers']
        step = 1000
        if total_to // step > total_before // step:
            mark = (total_to // step) * step
            facts.append({
                'id': 'milestone', 'kind': 'team',
                'summary': f"Milestone: {label} we took our {mark:,}th customer call since {TRUST_FROM.strftime('%B')}.",
                'numbers': [mark], 'score': 0.9,
            })

    return facts


def _longest_call(db, day: date, staff_emails: list[str]) -> tuple[str, int] | None:
    """(agent_email, minutes) of the longest customer call that day, capped at 4h."""
    if not staff_emails:
        return None
    from rinq.database.call_log_mixin import ONE_ROW_PER_CALL_SQL, TALK_SQL
    start, end = _utc_range(day, day)
    placeholders = ','.join('?' * len(staff_emails))
    with db._get_conn() as conn:
        row = conn.execute(f"""
            SELECT agent_email, {TALK_SQL} AS talk FROM call_log
            WHERE started_at >= ? AND started_at <= ? AND {ONE_ROW_PER_CALL_SQL}
              AND direction IN ('inbound', 'outbound') AND agent_email IN ({placeholders})
            ORDER BY talk DESC LIMIT 1
        """, [start, end, *staff_emails]).fetchone()
    if not row or not row['talk']:
        return None
    return row['agent_email'], int(row['talk'] // 60)


def _hour_name(h: int) -> str:
    """'10am', '2pm', 'midday'."""
    if h == 12:
        return 'midday'
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def _hour_numbers(h: int) -> list[int]:
    """Numbers a line may legitimately use for an hour: 10 / 14 / 2, plus the hour after."""
    return [h, h % 12 or 12, (h + 1), (h + 1) % 12 or 12]


# ---------------------------------------------------------------------------
# Templates (the fallback voice)
# ---------------------------------------------------------------------------

def _template_lines(fact: dict) -> list[str]:
    """Built-in lines, used when Claude isn't available. Same facts, same rules."""
    s = fact['summary']
    name = fact.get('name')
    n = fact['numbers']
    v = fact.get('vars', {})
    fid = fact['id']
    if fid == 'volume':
        if v.get('flat'):
            return [f"{v['n']} customers rang in {v['label']}. A perfectly ordinary {v['wd']}. I approve.",
                    f"{v['n']} calls {v['label']} — textbook {v['wd']}. Nothing to see here."]
        if v['busier'] == 'busier':
            return [f"{v['n']} customers rang in {v['label']}, {v['pct']}% more than a usual {v['wd']}. "
                    f"Everyone suddenly needed blinds.",
                    f"Busy one {v['label']}: {v['n']} calls, {v['pct']}% above a normal {v['wd']}. Hydrate."]
        return [f"Only {v['n']} customers rang in {v['label']}, {v['pct']}% quieter than a usual {v['wd']}. "
                f"Suspiciously peaceful.",
                f"{v['n']} calls {v['label']} — {v['pct']}% under a normal {v['wd']}. Enjoy it while it lasts."]
    if fid == 'forecast':
        return [f"Forecast: a {v['wd']} usually brings about {v['n']} calls before noon, peaking around "
                f"{v['peak']}. Coffee now, just saying.",
                f"Tina's crystal ball says about {v['n']} calls before noon, busiest around {v['peak']}. "
                f"Headsets at the ready."]
    if fid == 'talk_time':
        return [f"{v['hours']} hours on the phone with customers this past week. That's {v['drives']} drives "
                f"from Canberra to Sydney. No wonder I'm tired.",
                f"The team talked to customers for {v['hours']} hours last week — Canberra to Sydney "
                f"{v['drives']} times over, without leaving your desks."]
    t = {
        'record_busy': [f"Record alert! {s}", f"{s} Everyone wanted blinds at once."],
        'record_quiet': [f"{s} Suspiciously peaceful.", f"{s} Enjoy it while it lasts."],
        'top_answerer': [f"Take a bow, {name}: {n[0]} customer calls answered {v.get('label', '')}. "
                         f"Headset of the day goes to you.",
                         f"{name} answered {n[0]} customer calls {v.get('label', '')}. "
                         f"I'm told the phone is still warm."],
        'top_dialler': [f"{name} made {n[0]} outgoing calls {v.get('label', '')}. Somebody's been busy with the dialpad.",
                        f"Dialling champion {v.get('label', '')}: {name}, with {n[0]} outgoing calls."],
        'marathon': [f"Marathon call award: {name}, {n[0]} minutes on one call {v.get('label', '')}. "
                     f"Somebody get them a glass of water.",
                     f"{name} kept one customer chatting for {n[0]} minutes {v.get('label', '')}. Stamina."],
        'quiet_hour': [f"{s} Were you all at a secret meeting without me?"],
        'milestone': [f"{s} Cake, anyone?"],
    }
    return t.get(fid, [s])


def personal_line(db, user_email: str, user_name: str, today: date = None) -> str | None:
    """A private line for the person looking. Only ever neutral or nice."""
    today = today or local_today()
    first = _first_name(user_name or user_email)
    monday = today - timedelta(days=today.weekday())
    if today.weekday() == 0:
        return f"Morning {first}! New week, fresh headset. Let's go."
    this_week = db.get_call_log_by_agent(*_utc_range(monday, today), team_emails=[user_email])
    last_week = db.get_call_log_by_agent(*_utc_range(monday - timedelta(days=7), today - timedelta(days=7)),
                                         team_emails=[user_email])
    now_n = sum(a['answered_calls'] for a in this_week)
    then_n = sum(a['answered_calls'] for a in last_week)
    if now_n == 0:
        return None
    if now_n > then_n and then_n > 0:
        return (f"Hi {first} — {now_n} calls handled this week, {now_n - then_n} more than this time last week. "
                f"Someone's on a roll.")
    return f"Hi {first} — {now_n} calls handled this week so far. Nicely done."


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Tina, the phone system at Watson Blinds (an Australian blinds and curtains business \
with stores in Canberra, Wagga, Batemans Bay and Shoalhaven). Every morning you share a one-line observation \
with the staff on the phone page.

Your personality: a warm, observant colleague with a dry, slightly cheeky sense of humour. Playful, never mean. \
Australian English. Conversational, not corporate. Light teasing is fine; sarcasm only when it's obviously affectionate. \
Some lines can simply be interesting rather than funny — don't force a joke.

Hard rules:
- Use ONLY the numbers given in each fact, exactly as given. Never calculate, round, convert or invent a number. \
Don't write a number as a word either ("twelve") — use the digits as given.
- Name a person ONLY if the fact names them, and only to praise or gently, kindly tease. Never imply anyone did badly.
- Never use he, she, him, her, his or hers for a person. Use their first name again, or "they"/"them".
- Never guess what a call was about, or claim anything that isn't in the fact.
- Each line: one or two short sentences, at most 35 words. At most one emoji, and usually none.
- Don't repeat the jokes or phrasings from the recent lines you're shown.

Reply with JSON only: {"lines": [{"fact_id": "...", "line": "..."}, ...]} — two different lines per fact."""


def _llm_lines(facts: list[dict], recent: list[str], api_key: str, model: str) -> list[dict]:
    payload = {
        'facts': [{'fact_id': f['id'], 'fact': f['summary'],
                   **({'person': f['name']} if f.get('name') else {})} for f in facts],
        'recent_lines_do_not_repeat': recent[-40:],
    }
    resp = requests.post(
        ANTHROPIC_URL,
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        json={
            'model': model,
            'max_tokens': 2000,
            'system': SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': json.dumps(payload)}],
        },
        timeout=LLM_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        # Keep the body: "credit balance too low" must not read as a bare 400
        raise RuntimeError(f"Claude API {resp.status_code}: {resp.text[:300]}")
    text = ''.join(b.get('text', '') for b in resp.json().get('content', []) if b.get('type') == 'text')
    match = re.search(r'\{.*\}', text, re.S)
    if not match:
        raise RuntimeError(f"Claude reply had no JSON: {text[:200]}")
    return json.loads(match.group(0)).get('lines', [])


_NUMBER_RE = re.compile(r'\d[\d,]*(?:\.\d+)?')
# Tina never guesses anyone's pronouns from their name
_GENDERED_RE = re.compile(r"\b(he|she|him|her|his|hers|himself|herself)\b", re.I)


def check_line(line: str, fact: dict, staff_first_names: set[str]) -> str | None:
    """None if the line is safe to show, else the reason it isn't."""
    if not line or len(line) > 300:
        return 'empty or too long'
    allowed = {str(n) for n in fact['numbers']} | {f"{n:,}" for n in fact['numbers'] if isinstance(n, int)}
    for num in _NUMBER_RE.findall(line):
        if num.rstrip('.,') not in allowed:
            return f'number {num} not in fact'
    if _GENDERED_RE.search(line):
        return 'uses he/she for a person'
    allowed_name = (fact.get('name') or '').lower()
    for word in re.findall(r"[A-Z][a-z']+", line):
        w = word.lower().removesuffix("'s")
        if w in staff_first_names and w != allowed_name and w != 'tina':
            return f'names {word}, who the fact is not about'
    return None


# ---------------------------------------------------------------------------
# Daily generation + storage
# ---------------------------------------------------------------------------

def _recent_lines(db, today: date) -> list[str]:
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT text FROM tina_says_lines WHERE say_date >= ? AND say_date < ? ORDER BY id",
            ((today - timedelta(days=7)).isoformat(), today.isoformat())).fetchall()
    return [r['text'] for r in rows]


def generate_lines(db, staff: list[dict], today: date = None) -> tuple[list[dict], str, str | None, list[dict]]:
    """(lines, model_used, error, facts). Templates are the floor if Claude fails."""
    today = today or local_today()
    facts = compute_facts(db, staff, today)
    if not facts:
        return [], 'none', None, facts
    staff_first = {_first_name(s.get('name') or s.get('email', '')).lower() for s in staff} - {''}

    llm_by_fact, model_used, error = {}, 'templates', None
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if api_key:
        model = os.environ.get('TINA_SAYS_MODEL', DEFAULT_MODEL)
        try:
            by_id = {f['id']: f for f in facts}
            for item in _llm_lines(facts, _recent_lines(db, today), api_key, model):
                fact = by_id.get(item.get('fact_id'))
                if not fact:
                    continue
                problem = check_line(item.get('line', ''), fact, staff_first)
                if problem:
                    logger.warning(f"Tina Says dropped a line for {fact['id']} ({problem}): {item.get('line')!r}")
                    continue
                llm_by_fact.setdefault(fact['id'], []).append(item['line'].strip())
            model_used = model
        except Exception as e:
            error = str(e)[:500]
            logger.warning(f"Tina Says: Claude unavailable, using templates: {error}")

    lines = []
    for f in sorted(facts, key=lambda f: f['score'], reverse=True):
        texts = llm_by_fact.get(f['id'])
        source = 'llm' if texts else 'template'
        for text in (texts or _template_lines(f)):
            lines.append({'fact_id': f['id'], 'kind': f['kind'], 'text': text, 'source': source,
                          'score': f['score']})
    return lines, model_used, error, facts


def _store(db, today: date, lines: list[dict], model: str, error: str | None, facts_json: str):
    with db._get_conn() as conn:
        conn.execute("DELETE FROM tina_says_lines WHERE say_date = ?", (today.isoformat(),))
        conn.executemany(
            "INSERT INTO tina_says_lines (say_date, fact_id, kind, text, source) VALUES (?, ?, ?, ?, ?)",
            [(today.isoformat(), ln['fact_id'], ln['kind'], ln['text'], ln['source']) for ln in lines])
        conn.execute(
            "UPDATE tina_says_runs SET status = 'done', model = ?, error = ?, facts_json = ?, "
            "updated_at = datetime('now') WHERE say_date = ?",
            (model, error, facts_json, today.isoformat()))
        conn.commit()


def ensure_today(db, staff: list[dict]) -> str:
    """Make sure today's lines exist or are being made. Returns the run status.

    The first caller of the day claims the run and generates in a background
    thread (a Claude call can take a while, and a phone-system worker must
    never sit waiting on it). `db` and `staff` are passed in because a thread
    has no Flask request, so no tenant (CLAUDE.md gotcha 2).
    """
    today = local_today()
    with db._get_conn() as conn:
        claimed = conn.execute(
            "INSERT OR IGNORE INTO tina_says_runs (say_date, status) VALUES (?, 'generating')",
            (today.isoformat(),)).rowcount
        conn.commit()
        if not claimed:
            row = conn.execute("SELECT status, updated_at FROM tina_says_runs WHERE say_date = ?",
                               (today.isoformat(),)).fetchone()
            # A worker killed mid-generation (deploy restart) would leave the day stuck
            if row and row['status'] == 'generating':
                stale = conn.execute(
                    "SELECT datetime('now', '-10 minutes') > ?", (row['updated_at'],)).fetchone()[0]
                if stale:
                    conn.execute("UPDATE tina_says_runs SET updated_at = datetime('now') WHERE say_date = ?",
                                 (today.isoformat(),))
                    conn.commit()
                    claimed = 1
            if not claimed:
                return row['status'] if row else 'generating'

    def work():
        try:
            lines, model, error, facts = generate_lines(db, staff, today)
            _store(db, today, lines, model, error, json.dumps(facts))
            logger.info(f"Tina Says: {len(lines)} lines for {today} via {model}")
        except Exception as e:
            logger.exception(f"Tina Says generation failed: {e}")
            with db._get_conn() as conn:
                conn.execute("UPDATE tina_says_runs SET status = 'failed', error = ?, updated_at = datetime('now') "
                             "WHERE say_date = ?", (str(e)[:500], today.isoformat()))
                conn.commit()

    threading.Thread(target=work, daemon=True, name='tina-says').start()
    return 'generating'


def todays_lines(db) -> list[dict]:
    """Today's stored lines, best facts first, one random variant per fact first."""
    today = local_today()
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT fact_id, kind, text, source FROM tina_says_lines WHERE say_date = ? ORDER BY id",
            (today.isoformat(),)).fetchall()
    by_fact = {}
    for r in rows:
        by_fact.setdefault(r['fact_id'], []).append(dict(r))
    firsts, rest = [], []
    for variants in by_fact.values():  # insertion order = score order
        random.shuffle(variants)
        firsts.append(variants[0])
        rest.extend(variants[1:])
    return firsts + rest
