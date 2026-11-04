# Session Log — 2026-11-04 (voicemail box "Assign to")

## Overview

Jacinta asked why voicemails #726716 / #726717 went to Zelda's Projects Team. The phone system put them
there, not Zelda: +61261926884 is Brittany Moore's own direct line, and its voicemail box (#11 "Project
Team") files to Projects. Brittany is Showroom. Derek: calls ring Britt (already true) and voicemails go
to Britt too.

## What was found

- +61261926884 has no call flow. Brittany is its only `phone_assignments` receiver, so inbound calls ring
  her directly (144 of 239 answered since 1 Aug). It is also her `default_caller_id` (~1,400 outbound calls
  since Apr), so customers ring it back.
- Its voicemail box #11 "Project Team" (Zendesk-era group id → Zelda Projects Team) was created 2026-05-01,
  seconds before the number was pointed at it. Nothing else uses box 11. About 63 voicemail tickets since May.
- ⚠️ Email routing on a voicemail box is **not implemented**: `routes.py` logs `voicemail_routing_skipped`
  and the voicemail goes nowhere, but the admin UI offers the option. Nothing used it today.
- Zelda's routing rules do not run on an API ticket that pins a group, so they can't re-route Rinq voicemails.
- Counting `call_log.status = 'completed'` for inbound calls is wrong. Answered inbound calls are `answered`.

## Shipped

- **PR #92** (merged 03:09 UTC): optional **Assign to** staff picker on a ticket voicemail box (Admin →
  Voicemail destinations). Migration 085 adds `voicemail_destinations.ticket_assignee_email`. Passed as
  `assignee_email` to Zelda's `POST /api/tickets`; Zendesk/Watson providers accept it and ignore it.
- Tests: 4 new in `tests/test_voicemail_filing.py`. Full suite 94 passed. The routing tests were watched failing
  without the `routes.py` change.
- At session end the deploy was still holding for live calls to finish.

## Still TODO

- After the deploy lands: edit box 11 → name "Brittany Moore", group Showroom (Zelda 18), assign
  brittany.moore@watsonblinds.com.au. Check the next voicemail on that number lands correctly, then tell Jacinta.
- Decide on email routing for voicemail boxes: remove the option or build it.
