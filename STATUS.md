# Rinq — Status

## Current State: Production (Watson), Beta (Multi-tenant)

Watson Blinds is running in production through Rinq as of 2026-04-03. Multi-tenant features (subaccounts, onboarding) are functional but being refined.

## What Works

- **Inbound calls** — routing, queues, extension directory, schedules, holidays
- **Softphone** — browser-based calling via Twilio Client
- **Call recording** — local storage + Google Drive upload
- **Voicemail** — with Zendesk ticket creation (native API)
- **Email** — via Mabel (bot-team) using Watson's Google Workspace SMTP; Resend fallback
- **Caller enrichment** — customer/order lookup from Watson CRM (Clara/Otto)
- **Transfers** — blind, warm, 3-way
- **Reports** — call stats, leaderboard
- **Multi-tenant** — domain-based login, per-tenant databases, Twilio subaccounts
- **Auto-deploy** — push to main triggers GitHub Actions deploy
- **Onboarding** — address setup, phone number search/purchase

## What Needs Work

### High Priority (before onboarding more tenants)
- [ ] Refactor `api/routes.py` — 7000+ lines, break into modules
- [ ] Don't offer voicemail when no destination configured
- [ ] Per-tenant Twilio config — move remaining creds from .env to tenant record
- [ ] Onboarding wizard — guided setup flow for new tenants
- [ ] Billing — Stripe subscriptions, usage tracking

### Medium Priority
- [ ] Native staff directory — replace Peter dependency for Watson
- [ ] DND + empty queue should fall through to voicemail
- [ ] Internal caller ID — show name for extension-to-extension calls
- [ ] Transfer modal auto-close on answer
- [ ] Conference hangup vs not-answered detection
- [ ] Stale "on call" status after transfers
- [ ] Per-tenant recording storage (separate directories)
- [ ] Tenant admin UI — manage branding, domains, integrations

### Future
- [ ] Watson Twilio migration to subaccount
- [ ] Self-service tenant signup
- [ ] Twilio subaccount usage billing/markup
- [ ] Email routing per tenant (Cloudflare Email Routing API)
- [ ] Native CRM integration (replace Clara dependency)
- [ ] AI receptionist integration
- [ ] SIP trunk support

## Timeline

| Date | Milestone |
|------|-----------|
| 2026-04-03 | Extracted from bot-team, Watson migrated, multi-tenant working |
| 2026-04-03 | Derek personal tenant with Twilio subaccount |
| 2026-04-07 | Watson staff return to office (Rinq must be stable) |
