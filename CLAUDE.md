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
- **Email:** Mabel bot (auto-detected from `WATSON_MABEL_URL`) — sends via Watson's Google Workspace SMTP. Falls back to Resend if `RESEND_API_KEY` set. Override with `RINQ_EMAIL_PROVIDER=mabel|resend`
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
13. **SIP URIs in webhook fields** — when a SIP device places an outbound call, Twilio sends the dialed string wrapped in a SIP URI in `To` (e.g. `sip:*99@watsonblinds.sip.twilio.com:5060`), not the bare digits. Same shape appears in `From` for incoming SIP calls and in `From`/`To` parameters from the `<Sip>` noun. Twilio also appends `;transport=UDP` and similar params. Any code matching against `To`/`From` must normalise: strip the `sip:` prefix, then everything from `@` and `;` onward. Otherwise feature codes (`*99`), 4-digit extension routing, and identity lookups silently miss when called from desk phones
14. **Tenant resolution for SIP** — SIP calls have SIP URIs in From/To, not phone numbers. Middleware resolves tenant from the SIP domain name via `twilio_sip_domain` in the tenant record
15. **Twilio has no SIP registration API** — there is no REST API or webhook to check if a SIP device is currently registered. SIP presence is tracked by stamping `staff_extensions.sip_registered_at` when we ring or see a call from a SIP device. Users with activity in the last 24h show as "desk phone" in contacts/transfer targets
16. **LocalStaffDirectory email field** — returns `email` key, not `google_primary_email`/`work_email` (Peter format). Code consuming staff directory results must check all three
17. **Unix socket auth bypass** — requests hitting gunicorn directly (no `X-Forwarded-For` header) skip API key auth. All cron jobs should use `--unix-socket` instead of API keys

## Cron Jobs (derek user on server)

Cron jobs hit the gunicorn unix socket directly (no API key needed):

- **Recordings purge:** daily 3am — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/recordings/purge`
- **Stats aggregation:** every 15min — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/stats/aggregate`
- **Queue cleanup:** every 5min — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/queue/cleanup`
- **Address book sync:** daily 2am — `curl -s -X POST --unix-socket /var/www/rinq/rinq.sock http://localhost/api/address-book/sync` (iterates all tenants; Watson syncs from Peter, others no-op)

18. **Every call is a conference** — all call types (outbound, inbound, queue answer, extension, SIP auto-ring) use Twilio conferences. No `<Queue>` noun or direct `<Dial><Number>` bridges. This enables consistent recording, participant tracking, hold/transfer for all calls
19. **call_participants table** — source of truth for who is in each call. Updated at every lifecycle event (join, leave, transfer). `call_state.py` reads from this table instead of making Twilio API calls. The `conference_join` endpoint is the catch-all for participant tracking
20. **ring_attempts table** — tracks outbound ring calls across gunicorn workers (replaces in-memory dicts that broke across processes). Cleaned up by the 5-minute queue cleanup cron
21. **Don't force-end calls via REST API** — `calls.update(status='completed')` triggers after-dial TwiML processing which can cause unexpected callbacks (e.g. blind transfer rejection flow calling agent back). Let calls end naturally via conference end or browser disconnect
22. **Twilio SDK `call._from` not `call.from_`** — SDK 9.10.4 uses `_from` (leading underscore) for the from field. Use `getattr(call, '_from', None)`
23. **Conference recording via TwiML** — use `record="record-from-start"` on the Conference noun, NOT the REST API. The SDK's `conference.recordings` has no `create()` method
24. **Permissions model** — three levels: admin > manager > user. Stored in `tenant_users.role` (master DB). Domain-level Google OAuth controls login access. The `reports_to` field in `staff_extensions` determines visibility (recordings, reports). Admins see everything. Role management is at `/admin/users`. Decorators: `login_required`, `manager_required`, `admin_required`. Admins pass all three; managers pass `login_required` and `manager_required` only
25. **Queue pause** — queues have `paused_from` / `paused_until` (UTC ISO, nullable). `_is_queue_paused()` in `api/routes.py` checks the window at both inbound routing decision points; returns True for indefinite pauses (`paused_until = NULL`). Managed via `/queues` (managers) or the Queues card under `/admin` (admins). Setting a pause never touches DND
26. **queue_managers table** — separate from `queue_members`. Links managers to queues they can pause without being a call-answering member. Admins assign managers per queue in the Queues admin page. The `/queues` manager page shows only queues where `queue_managers.user_email` matches the current user (admins see all)
27. **`show_in_pam` is legacy / Peter-only** — the `staff_extensions.show_in_pam` column controls visibility in Peter's PAM directory (`/staff-phones` API), NOT Rinq's `/api/contacts` list. The home-page Settings card no longer surfaces it. Don't bind UI to it; use `hide_mobile` for Rinq directory behaviour
28. **`/api/contacts` merges two sources** — staff source (Peter `/api/staff` if reachable, else local `staff_extensions`) PLUS the `address_book` table populated by the daily Peter sync. Staff entries are deduped against address book by `email`; staff take precedence and are enriched with section/position/display_mobile from the address book copy when missing. `address_book.email` was added in migration 069 specifically to enable this dedup
29. **Conference-based talk time attribution** — `call_log` has one primary entry per call (keyed on customer SID for inbound, agent SID for outbound). Agent SIDs never have their own call_log entries. `_credit_conference_participants()` in `api/routes.py` is called at call end to walk `call_participants` and give every agent their actual talk time: primary agent's `talk_seconds` is corrected to their actual conference time; additional agents (warm/blind transfer recipients, etc.) get new `call_type='transfer'` entries linked via `parent_call_sid`. This runs regardless of how agents joined the conference
30. **`currentQueueCallerSid` vs `currentOutboundCallSid` in phone.html** — `currentQueueCallerSid` takes priority over `currentOutboundCallSid` in `endCall()`. The call state poll must NOT set `currentQueueCallerSid` from `state.customer_call_sid` when `currentOutboundCallSid` is already set, or `voice/call-ended` fires with the customer's SID instead of the agent's SID, leaving `talk_seconds` NULL on the agent's call_log entry
31. **`call_participants` is purged after 24h** — the 5-minute queue cleanup cron calls `cleanup_old_participants(hours=24)`. Historical participant data is gone; warm transfer backfills must be reconstructed from `call_log.transfer_consult_call_sid`, `transferred_at`, `answered_at`, `ended_at`, and `staff_extensions.extension`
32. **`endCall()` during warm-transfer consult kills the customer** — `endCall()` in `phone.html` posts to `/api/voice/call-ended` with `currentQueueCallerSid` (the customer's SID). If it fires while `isConsulting=true` (e.g. the agent's Twilio Device leg disconnects mid-consult, firing `currentCall.on('disconnect')`), the server ends the customer's call even though the customer is still held in the main conference. Guard at the top of `endCall()` clears `currentQueueCallerSid`/`currentTransferKey`/`isConsulting`/`consultCallSid` when `isConsulting` is true, so the signal falls back to the agent's own browser leg SID. The server's consult-status callback still handles unhold/rejoin
33. **Feature codes** — `*99` is the only one today. Toggles `staff_extensions.default_caller_id` ↔ `alternate_caller_id` and plays a friendly-name announcement. Dispatched in `voice_outbound` *after* SIP-URI normalisation (gotcha 13) and *before* the 4-digit extension and PSTN routing branches. Add new feature codes to that same dispatch ladder. Per-user — each user picks their two slots in `/my-devices`. Browser softphone exposes the same swap as a button via `/my-desk-phone/swap-caller-id` (JSON)
34. **Browser/SIP ring legs never carry the customer's CallSid** — queue auto-ring and extension rings are REST-created calls (`client.calls.create(to='client:...'|'sip:...')`), so the `CallSid` the browser SDK (or the agent-answer webhook's `CallSid` param) sees is the **agent's ring leg**, not the customer's call. The customer's SID travels only in webhook query strings (`?customer_call_sid=...`) — Twilio's servers see it, the browser never does. The only customer identity the browser gets is `From` (set to the customer's number when ringing; subject to gotcha 13's SIP-URI wrapping on desk phones). Never compare a ring leg's CallSid against a queued caller's SID — it can't match; identify the caller by `From` digits (`_ctxDigits` in phone.html) or ask the server (`/api/my-call-state`, `/api/voice/incoming-caller`)

35. **Cron endpoints have NO tenant context** — `resolve_tenant()` resolves from the session, or from phone numbers on `/api/voice/*` and `/api/sip/*`. A cron POSTing to the unix socket has neither, so `g.tenant` is `None` and the first `get_db()` raises `RuntimeError("No tenant in request context")`. Cron redirects output to `/dev/null`, so the job dies silently — this killed the recordings purge outright and stopped stats aggregation on 2026-04-01 without anyone noticing for four months. Any endpoint a cron calls must loop over `iter_tenant_contexts()` (`tenant/context.py`) when there is no session, and log per-tenant failures. Applies to `/api/recordings/purge`, `/api/stats/aggregate`, `/api/queue/cleanup` and `/api/address-book/sync` — all fixed 2026-07-26. `/api/voicemail/cleanup` has the same shape and is not currently wired to a cron; fix it the same way if it ever is
36. **`request.get_json()` 415s on a bodyless cron POST** — the cron `curl`s send no body and no `Content-Type`, and a bare `request.get_json()` (even with `or {}`) raises 415 *before* the handler body runs. This is the second, independent reason the purge/stats/cleanup crons silently did nothing — fixing the tenant context alone was not enough. Any endpoint with an optional JSON body must use `request.get_json(silent=True) or {}`. The same broken `get_json() or {}` pattern is still present at ~15 other call sites that are only ever called from the browser (which sends the header) — fix them if a cron or external caller is ever pointed at them
37. **Local recordings are a cache, not storage** — `data/recordings/` holds MP3s that `get_recording_audio` re-fetches from Google Drive and re-caches on a miss. Purging a file is only safe when the row has a `drive_file_id`; without one the local file is the only playable copy (the Google Groups archive is cold and returns 404 for playback). Also note almost nothing is ever played (182 of 20,913 on watson), so any staleness rule keyed on `last_accessed_at` must fall back to `created_at` or it evicts same-day calls

## Testing

No automated tests yet (inherited from Tina, tests are in bot-team repo).
Manual test runsheet at `/admin/test-runsheet`.

## Known Issues

1. `phone.html` and `routes.py` are too large with deeply coupled logic — see refactor notes in memory
