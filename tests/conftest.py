"""Test harness for Rinq ("Tina").

Every test gets its own tenant (own master-DB row, own tenant database built
by the real migrations) and a phone number that resolves to it, exactly as a
Twilio webhook would. Nothing here talks to Twilio, Rosie or the internet:
the outside world is replaced by the stubs in `outside`, which record what
Tina tried to do so a test can assert on it.

Run from the repo root:  python -m pytest
"""

import itertools
import json
import os
import sys
import tempfile
from datetime import datetime as _real_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Environment BEFORE rinq is imported - config reads it at import time.
# (Each test then gets its own data folder - see the `app` fixture.)
os.environ['RINQ_DATA_DIR'] = tempfile.mkdtemp(prefix='rinq-tests-')
os.environ['FLASK_SECRET_KEY'] = 'test-secret-key'
os.environ['RINQ_TWILIO_SIGNATURE_MODE'] = 'off'
os.environ['RINQ_INTEGRATIONS'] = 'none'
os.environ['RINQ_AUTH'] = 'standalone'
os.environ['RINQ_WEBHOOK_URL'] = 'https://tina.test'
os.environ['WATSON_ROSIE_URL'] = 'https://rosie.test'
os.environ.pop('AI_RECEPTIONIST_URL', None)
os.environ.pop('ROSIE_WEBHOOK_URL', None)

from rinq.app import app as _app  # noqa: E402
from rinq.database.master import get_master_db  # noqa: E402
from rinq.tenant.context import get_tenant_db  # noqa: E402

ROSIE_ANSWER_URL = 'https://rosie.test/api/voice/answer'
OPEN_ALL_WEEK = json.dumps({d: {'open': '00:00', 'close': '23:59'}
                            for d in ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')})
# A store's real hours (Shoalhaven's in Tina): weekdays 9-4:30
STORE_HOURS = {d: {'open': '09:00', 'close': '16:30'} for d in ('mon', 'tue', 'wed', 'thu', 'fri')}
# A Wednesday, mid-morning in Sydney
FROZEN_NOW = _real_datetime(2026, 10, 28, 10, 30)

_tenant_ids = itertools.count(1)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The real app, pointed at an empty data folder for this test only."""
    from rinq.config import config
    import rinq.database.master as master
    import rinq.tenant.context as context
    monkeypatch.setattr(config, 'master_db_path', str(tmp_path / 'master.db'))
    monkeypatch.setattr(config, 'tenants_dir', str(tmp_path / 'tenants'))
    monkeypatch.setattr(master, '_master_db', None)
    monkeypatch.setattr(context, '_tenant_dbs', {})
    _app.config['TESTING'] = True
    return _app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Opening hours are decided by the clock - pinned to Wed 10:30 Sydney.

    A test can move it: `frozen_clock.set(datetime(2026, 10, 28, 16, 45))`
    (naive = Sydney local time)."""
    import rinq.api.schedule as schedule
    clock = SimpleNamespace(now=FROZEN_NOW)
    clock.set = lambda when: setattr(clock, 'now', when)

    class FrozenDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(clock.now) if tz is not None else clock.now

    monkeypatch.setattr(schedule, 'datetime', FrozenDatetime)
    return clock


@pytest.fixture(autouse=True)
def outside(monkeypatch):
    """Stand-ins for Twilio, Rosie, recordings and caller lookup.

    `outside.busy` - emails Twilio would report as on a call right now.
    `outside.rosie_up` - whether Rosie answers its health check.
    `outside.calls_created` - every Twilio calls.create() Tina made.
    `outside.busy_lookup_fails` - make the "who's on a call" check blow up.
    """
    import rinq.api.routes as routes
    import rinq.api.ai_test_routes as ai_test_routes

    state = SimpleNamespace(busy=set(), rosie_up=True, calls_created=[],
                            busy_lookup_fails=False)

    def create(**kwargs):
        state.calls_created.append(kwargs)
        return SimpleNamespace(sid=f"CAleg{len(state.calls_created)}")

    fake_client = SimpleNamespace(
        calls=lambda *a, **k: SimpleNamespace(update=lambda **kw: None, fetch=lambda: None),
    )
    fake_client.calls.create = create
    fake_twilio = SimpleNamespace(client=fake_client, capture_for_thread=lambda: None,
                                  _format_phone_number=lambda n: n)
    monkeypatch.setattr(routes, 'get_twilio_service', lambda: fake_twilio)
    monkeypatch.setattr(ai_test_routes, 'rosie_is_up', lambda: state.rosie_up)

    def active_calls():
        if state.busy_lookup_fails:
            raise RuntimeError("Twilio unreachable")
        return [{'agent_email': e, 'call_sid': f'CAbusy{i}'} for i, e in enumerate(state.busy)]

    monkeypatch.setattr(routes, '_get_active_calls_from_twilio', active_calls)
    monkeypatch.setattr(routes, '_get_active_call_sids', lambda: set())
    # Background Twilio work (ringing agents) runs inline, against the fake
    monkeypatch.setattr(routes, 'start_twilio_thread', lambda fn, *a, **k: fn())

    from rinq.services import caller_enrichment
    monkeypatch.setattr(caller_enrichment, 'get_enrichment_service',
                        lambda: SimpleNamespace(enrich_caller=lambda number: {}))
    from rinq.services import recording_service as rec
    # Recording starts fine unless a test says otherwise (outside.recordable)
    state.recordable = True
    state.recordings_started = []

    def start_recording(sid):
        state.recordings_started.append(sid)
        return ({'success': True, 'recording_sid': f'RE{sid}'} if state.recordable
                else {'success': False, 'error': 'Requested resource is not eligible for recording'})
    monkeypatch.setattr(rec.recording_service, 'start_recording', start_recording)
    return state


class Store:
    """One store's phone setup, built through Tina's own database calls."""

    def __init__(self, db, number):
        self.db = db
        self.number = number

    def with_staff(self, *emails, active=True):
        for i, email in enumerate(emails):
            self.db.add_queue_member(self.queue_id, email, i, 'test')
        if not active:
            with self.db._get_conn() as conn:
                conn.execute("UPDATE queue_members SET is_active = 0 WHERE queue_id = ?",
                             (self.queue_id,))
                conn.commit()
        return self


@pytest.fixture
def make_store(app):
    """make_store(no_answer='ai_receptionist', open=True) -> Store.

    A tenant with one store number, an all-week (or never) schedule, a queue
    and a call flow - the same shape as Watson's store call flows. One store
    per test.
    """
    contexts = []

    def build(no_answer='voicemail', open=True, name='Shoalhaven', tenant_id='watson',
              hours=None, closed_action='voicemail', tenant_default_caller_id=True):
        # 'watson' by default: Stuart (WATSON_ROSIE_URL) is only ever handed
        # the watson tenant's callers. `hours` = a business_hours dict (e.g.
        # STORE_HOURS); default open all week, or never with open=False.
        # tenant_default_caller_id=False leaves the tenant with no default.
        number = f"+6124418{next(_tenant_ids):04d}"
        master = get_master_db()
        master.create_tenant(tenant_id, f"Test {tenant_id}",
                             twilio_account_sid=f"AC{tenant_id}",
                             twilio_default_caller_id=number if tenant_default_caller_id else None)
        master.register_phone_number(number, tenant_id)

        ctx = app.test_request_context()
        ctx.push()
        contexts.append(ctx)
        from flask import g
        g.tenant = master.get_tenant(tenant_id)
        db = get_tenant_db()

        business_hours = json.dumps(hours) if hours is not None else (OPEN_ALL_WEEK if open else '{}')
        schedule_id = db.create_schedule(
            {'name': f'{name} hours', 'business_hours': business_hours}, 'test')
        queue_id = db.create_queue({'name': name}, 'test')
        vm_id = db.create_voicemail_destination(
            {'name': f'{name} voicemail', 'email': 'store@example.com'}, 'test')
        flow_id = db.create_call_flow({'name': name, 'schedule_id': schedule_id,
                                       'open_action': 'queue', 'open_queue_id': queue_id,
                                       'closed_action': closed_action,
                                       'voicemail_destination_id': vm_id}, 'test')
        db.update_call_flow(flow_id, {'open_no_answer_action': no_answer}, 'test')
        sid = f"PN{tenant_id}"
        db.upsert_phone_number({'sid': sid, 'phone_number': number, 'friendly_name': name,
                                'forward_to': None, 'is_active': 1, 'synced_at': None})
        db.set_phone_number_call_flow(sid, flow_id, 'test')

        store = Store(db, number)
        store.queue_id, store.flow_id = queue_id, flow_id
        store.schedule_id, store.vm_id, store.phone_sid = schedule_id, vm_id, sid
        return store

    yield build
    for ctx in reversed(contexts):
        ctx.pop()


def ring(client, store, caller='+61400111222', sid='CAcustomer1'):
    """A customer rings the store - Twilio's inbound webhook."""
    r = client.post('/api/voice/incoming',
                    data={'To': store.number, 'From': caller, 'CallSid': sid,
                          'AccountSid': 'ACtest'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)
