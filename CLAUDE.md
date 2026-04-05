# Claude Reference Guide for Rinq

## Project Overview

Rinq is a multi-tenant cloud phone system built on Twilio. Extracted from the Watson Blinds bot-team (Tina) and running as a standalone product.

**Repo:** `dezgo/rinq` (Derek's personal GitHub)
**Server:** do-personal (209.38.91.37)
**Domains:** rinq.cc, rinq.appfoundry.cc, tina.watsonblinds.com.au

## Architecture

### Multi-Tenant (always-on, no single-tenant mode)
- Master DB (`data/master.db`) — tenants, users, phone number→tenant mapping
- Per-tenant databases (`data/tenants/{id}/rinq.db`) — phone system data
- Tenant resolution: by domain (login), by phone number (Twilio webhooks)
- Each tenant gets a Twilio subaccount with isolated numbers/billing
- **Tenant isolation is critical** — never use global config for tenant-specific values
- Use `get_twilio_config('twilio_*')` from `tenant.context` for all Twilio config (NOT `config.twilio_*`)
- In-memory caches must be keyed by tenant ID

### Key Directories
```
rinq/
├── api/routes.py          # API endpoints (7000+ lines, needs refactoring)
├── auth/                  # Standalone Google OAuth
├── database/
│   ├── db.py              # Tenant database (phone numbers, call flows, etc.)
│   ├── master.py          # Master database (tenants, users)
│   └── migrations/        # master/ and tenant migrations
├── integrations/
│   ├── base.py            # Abstract interfaces
│   ├── zendesk/           # Native Zendesk (tickets)
│   ├── resend/            # Native Resend (email)
│   └── watson/            # Watson bot-team (Clara, Otto, etc.)
├── services/
│   ├── twilio_service.py  # Twilio API (tenant-aware, per-subaccount clients)
│   ├── transfer_service.py
│   ├── recording_service.py
│   ├── reporting_service.py
│   ├── provisioning.py    # Tenant provisioning (subaccount creation)
│   └── ...
├── tenant/
│   ├── middleware.py       # Request-level tenant resolution
│   └── context.py          # Tenant DB/config access (get_twilio_config, get_db)
├── vendor/                 # Vendored modules for standalone operation
├── web/routes.py           # Web UI routes
└── web/templates/          # Jinja2 templates
```

### Integrations
Pluggable via env vars. Current setup:
- **Tickets:** Native Zendesk (auto-detected from `ZENDESK_*` env vars)
- **Email:** Native Resend (auto-detected from `RESEND_API_KEY`)
- **Customer lookup:** Watson/Clara (via `WATSON_CLARA_URL`)
- **Order lookup:** Watson/Otto (via `WATSON_OTTO_URL`)
- **Staff directory:** Local (staff_extensions table, no external dependency)

### Tenants

| Tenant | Domain | Twilio | Notes |
|--------|--------|--------|-------|
| watson | tina.watsonblinds.com.au | Master account (ACe458...) | Production, 8 phone numbers |
| derek | rinq.cc | Subaccount (AC9a44...) | Personal, 1 phone number |

## Tenant Provisioning

New tenants are fully automated via `provisioning.py` or CLI:
- Creates Twilio subaccount, TwiML App, API key, SIP credential list + domain
- SIP credentials auto-created per user on first visit to My Devices
- CLI: `python -m rinq.cli setup-tenant --id foo --name "Foo" --email admin@foo.com`
- Backfill SIP for existing tenants: `python -m rinq.cli setup-sip --tenant foo`

## Deployment

Push to `main` → GitHub Actions → SSH → `deploy.sh` → pull, pip install, restart gunicorn.

- **Systemd service:** `rinq` (3 workers, unix socket)
- **Nginx:** serves rinq.cc, rinq.appfoundry.cc, tina.watsonblinds.com.au
- **SSL:** Let's Encrypt via Certbot
- **Sudoers:** derek can restart rinq without password
- **Deploy key:** `~/.ssh/rinq_deploy`

## Background Threads

Several functions spawn background threads for Twilio API calls (ringing agents, transfers). These threads have NO Flask request context, so:
- **`config.webhook_base_url`** — must be captured and passed as `base_url` parameter
- **`get_twilio_service().client`** — must call `capture_for_thread()` before spawning
- **`get_db()`** — won't have tenant context, uses cached thread account SID

## Common Gotchas

1. **Never use `config.twilio_*` directly** — use `get_twilio_config()` from `tenant.context`. Global config belongs to the master account and will leak watson's values into other tenants
2. **Tenant context in threads** — always capture base_url and call capture_for_thread() before spawning
3. **PSTN caller ID** — outbound calls to mobiles must use a number owned by the tenant's subaccount
4. **Static audio files** — not in git (gitignored), must be copied to server manually
5. **Recordings directory** — `rinq/data/recordings/`, shared across tenants (SIDs are globally unique)
6. **config.webhook_base_url** — checks tenant record → env var → request host → None
7. **TwilioService is a singleton** — but caches per-account-SID clients for multi-tenant
8. **Service .db properties** — all return get_db() per-call, NOT cached at init (multi-tenant)
9. **Twilio SDK `.list()` pagination** — throws `TwilioException` (base class), NOT `TwilioRestException`. Always catch both
10. **SIP domain names** — globally unique across all Twilio accounts. Use account SID suffix to avoid collisions
11. **SIP registration** — must set `sip_registration=True` when creating domains, otherwise Twilio rejects all REGISTER with 403

## Cron Jobs (derek user on server)

- **Recordings purge:** daily 3am — `POST /api/recordings/purge`
- **Stats aggregation:** every 15min — `POST /api/stats/aggregate`
- **Queue cleanup:** every 5min — `POST /api/queue/cleanup`

## Testing

No automated tests yet (inherited from Tina, tests are in bot-team repo).
Manual test runsheet at `/admin/test-runsheet`.

## Known Issues (Pre-existing)

1. DND + empty queue doesn't fall through to voicemail
2. Internal extension calls show Twilio number instead of caller's name
3. Transfer modal doesn't auto-close when target answers
4. Conference hangup wrongly triggers "not answered" message
5. Stale "on call" status after transfer completes
6. Voicemail offered even when no voicemail destination configured
