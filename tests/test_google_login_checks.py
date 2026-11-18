"""Google login: who gets in, and who is auto-added to a tenant.

- An unverified Google email must never log in: users are matched by email,
  so an unverified address could step into an existing staff member's account.
- Auto-provisioning (no explicit membership, email domain in the tenant's
  allowed_domains) also requires Google's `hd` claim to be one of those
  domains — i.e. the account is managed by the company's Workspace, not a
  personal Google account someone made with a work address.
- Explicitly-added users (e.g. a personal Gmail admin) are unaffected.
"""
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def login(app, monkeypatch):
    import rinq.auth.oauth as oauth
    from rinq.config import config
    from rinq.database.master import get_master_db
    master = get_master_db()
    with sqlite3.connect(config.master_db_path) as c:
        c.execute("INSERT INTO tenants (id, name, domain, allowed_domains, is_active) "
                  "VALUES ('acme', 'Acme', 'tina.test', 'acme.test,acme-drapes.test', 1)")
        c.execute("INSERT INTO tenants (id, name, domain, is_active) "
                  "VALUES ('solo', 'Solo', 'solo.test', 1)")
    owner = master.get_or_create_user(email='owner@gmail.com')
    master.add_user_to_tenant('solo', owner['id'], role='admin')

    claims = {}
    flow = SimpleNamespace(code_verifier=None, fetch_token=lambda **kw: None,
                           credentials=SimpleNamespace(id_token='tok'))
    monkeypatch.setattr(oauth, '_get_flow', lambda state=None: flow)
    monkeypatch.setattr(oauth.id_token, 'verify_oauth2_token', lambda *a, **k: dict(claims))
    client = app.test_client()

    def do(**c):
        claims.clear()
        claims.update({'sub': '123', 'name': 'X', **c})
        with client.session_transaction() as s:
            s['oauth_state'] = 'st'
        client.get('/auth/callback?state=st&code=c')
        with client.session_transaction() as s:
            out = dict(s)
        client.get('/logout')
        return out

    def memberships(email):
        u = master.get_user_by_email(email)
        return [t['id'] for t in master.get_user_tenants(u['id'])] if u else []

    do.memberships = memberships
    return do


def test_workspace_account_is_auto_provisioned(login):
    s = login(email='new@acme.test', email_verified=True, hd='acme.test')
    assert s.get('tenant_id') == 'acme'
    assert login.memberships('new@acme.test') == ['acme']


def test_hd_may_be_any_of_the_tenants_domains(login):
    # One Workspace, several domains: hd can name the primary one.
    s = login(email='new@acme-drapes.test', email_verified=True, hd='acme.test')
    assert s.get('tenant_id') == 'acme'


@pytest.mark.parametrize('hd', [None, 'other.test'])
def test_non_workspace_account_with_a_work_address_is_not_provisioned(login, hd):
    claims = {'email': 'sneaky@acme.test', 'email_verified': True}
    if hd:
        claims['hd'] = hd
    s = login(**claims)
    assert 'user_id' not in s
    assert login.memberships('sneaky@acme.test') == []


@pytest.mark.parametrize('verified', [False, None, 'false'])
def test_unverified_email_never_logs_in(login, verified):
    claims = {'email': 'owner@gmail.com'}
    if verified is not None:
        claims['email_verified'] = verified
    s = login(**claims)
    assert 'user_id' not in s


def test_explicit_gmail_member_still_logs_in(login):
    s = login(email='owner@gmail.com', email_verified=True)
    assert s.get('tenant_id') == 'solo'
