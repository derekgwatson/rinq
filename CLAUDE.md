# Claude Reference Guide for Rinq

You are working on a modern SaaS-style web application.

Design standards:
- Clean, minimal, professional UI
- Generous whitespace
- Clear visual hierarchy
- Consistent spacing scale (8px system)
- Subtle shadows, soft borders, restrained colors
- Avoid clutter and unnecessary elements

Frontend expectations:
- Production-ready code only
- Prefer reusable components
- Mobile-first responsive design
- Clean layout structure (no messy nesting)
- Use consistent spacing and typography

UX expectations:
- Clear primary CTA on every page
- Reduce cognitive load
- Obvious navigation and flow
- Good empty states and loading states

Copywriting style:
- Direct, simple, benefits-first
- No hype, no fluff
- Short sentences
- Clear value

When improving UI:
- Do not just tweak — restructure if needed
- Prioritise clarity over cleverness
- Make it feel like a polished SaaS product

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
2. **Tenant context in threads** — always capture `db = get_db()`, `sip_domain = _get_sip_domain()`, and `base_url` BEFORE spawning. Call `capture_for_thread()` on TwilioService too. `flask.g` does not exist in background threads — any function that touches `get_db()`, `get_current_tenant()`, or `_get_sip_domain()` will silently fail or raise RuntimeError
3. **PSTN caller ID** — outbound calls to mobiles must use a number owned by the tenant's subaccount
4. **Static audio files** — not in git (gitignored), must be copied to server manually
5. **Recordings directory** — `rinq/data/recordings/`, shared across tenants (SIDs are globally unique)
6. **config.webhook_base_url** — checks tenant record → env var → request host → None
7. **TwilioService is a singleton** — but caches per-account-SID clients for multi-tenant
8. **Service .db properties** — all return get_db() per-call, NOT cached at init (multi-tenant)
9. **Twilio SDK `.list()` pagination** — throws `TwilioException` (base class), NOT `TwilioRestException`. Always use `twilio_list()` from `twilio_service.py`, never call `.list()` directly
10. **SIP domain names** — globally unique across all Twilio accounts. Use account SID suffix to avoid collisions
11. **SIP registration** — must set `sip_registration=True` when creating domains, otherwise Twilio rejects all REGISTER with 403
12. **SIP domain voice URL** — must point to `/api/voice/outbound` (handles both browser and SIP device calls). NOT `/api/sip/incoming` (doesn't exist)
13. **SIP URI parameters** — Twilio appends `;transport=UDP` to SIP URIs. Always strip parameters after `@` before matching (e.g. `split(';')[0]`)
14. **Tenant resolution for SIP** — SIP calls have SIP URIs in From/To, not phone numbers. Middleware resolves tenant from the SIP domain name via `twilio_sip_domain` in the tenant record
15. **Twilio has no SIP registration API** — there is no REST API or webhook to check if a SIP device is currently registered. SIP presence is tracked by stamping `staff_extensions.sip_registered_at` when we ring or see a call from a SIP device. Users with activity in the last 24h show as "desk phone" in contacts/transfer targets
16. **LocalStaffDirectory email field** — returns `email` key, not `google_primary_email`/`work_email` (Peter format). Code consuming staff directory results must check all three
17. **Unix socket auth bypass** — requests hitting gunicorn directly (no `X-Forwarded-For` header) skip API key auth. All cron jobs should use `--unix-socket` instead of API keys

## Cron Jobs (derek user on server)

Cron jobs hit the gunicorn unix socket directly (no API key needed):

- **Recordings purge:** daily 3am — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/recordings/purge`
- **Stats aggregation:** every 15min — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/stats/aggregate`
- **Queue cleanup:** every 5min — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/queue/cleanup`

## Testing

No automated tests yet (inherited from Tina, tests are in bot-team repo).
Manual test runsheet at `/admin/test-runsheet`.

18. **Every call is a conference** — all call types (outbound, inbound, queue answer, extension, SIP auto-ring) use Twilio conferences. No `<Queue>` noun or direct `<Dial><Number>` bridges. This enables consistent recording, participant tracking, hold/transfer for all calls
19. **call_participants table** — source of truth for who is in each call. Updated at every lifecycle event (join, leave, transfer). `call_state.py` reads from this table instead of making Twilio API calls. The `conference_join` endpoint is the catch-all for participant tracking
20. **ring_attempts table** — tracks outbound ring calls across gunicorn workers (replaces in-memory dicts that broke across processes). Cleaned up by the 5-minute queue cleanup cron
21. **Don't force-end calls via REST API** — `calls.update(status='completed')` triggers after-dial TwiML processing which can cause unexpected callbacks (e.g. blind transfer rejection flow calling agent back). Let calls end naturally via conference end or browser disconnect
22. **Twilio SDK `call._from` not `call.from_`** — SDK 9.10.4 uses `_from` (leading underscore) for the from field. Use `getattr(call, '_from', None)`
23. **Conference recording via TwiML** — use `record="record-from-start"` on the Conference noun, NOT the REST API. The SDK's `conference.recordings` has no `create()` method
24. **Local permission service** — roles stored in tenant DB `permissions` table via `LocalPermissionService`. No dependency on external Grant API

## Known Issues

1. `phone.html` and `routes.py` are too large with deeply coupled logic — see refactor notes in memory
