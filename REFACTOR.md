# Refactor Plan

Accumulated tech debt from 2026-04-06 testing session. Every fix risked breaking something else due to tight coupling and scattered state. The auto-hangup feature alone caused 3 bugs because it interacted with conference setup, warm transfers, and 3-way calls in ways that weren't obvious.

**Rule: Plan this as a dedicated refactor. Don't mix with feature work.**

## Frontend (phone.html)

1. ~~**Group call state into one object**~~ ✅ Done — `resetCallState()` resets all 19 per-call variables in one shot. Individual vars kept to avoid 200+ renames; the key benefit (no missed resets) is delivered.

2. ~~**Eliminate global flags**~~ ✅ Done — `window._sawMultipleParticipants` now reset inside `resetCallState()`.

3. **Break up phone.html** — transfer logic, call state polling, queue dashboard, contacts all interleaved. Could use `{% include %}` partials. Lower priority now that state management is cleaner.

## Backend (routes.py)

4. ~~**Break up routes.py**~~ ✅ Done — from 7700+ down to 5876 lines. Extracted:
   - `api/twiml.py` — TwiML helpers (audio URL, say/play, closed messages)
   - `api/identity.py` — email/browser/SIP identity conversion
   - `api/schedule.py` — business hours checking, next-open-time
   - `api/call_state.py` — participant resolution, call state polling (270 lines)
   - `api/transfer_routes.py` — all transfer endpoints (~700 lines)
   - `api/recording_routes.py` — all recording endpoints (~410 lines)

5. ~~**Consolidate duplicate call routing paths**~~ ✅ Done — `_build_voicemail_twiml` and `queue_rejected_voicemail` now delegate to `_go_to_voicemail`. `queue_no_answer` inline voicemail replaced with `_build_voicemail_twiml` call. All open-hours voicemail flows through one path. Closed-hours voicemail kept separate (genuinely different prompt logic).

6. ~~**Extract phone number helpers**~~ ✅ Done — `services/phone.py` with `ensure_plus`, `to_e164`, `to_local`, `normalize_au_mobile`, `is_valid_au_mobile`, `format_for_speech`. All inline duplication replaced.

7. ~~**Proper call_state.py module**~~ ✅ Done — `api/call_state.py` with `get_call_state()`, `resolve_participant()`, `build_user_map()`, `get_conference_participants()`. Shared between call state polling and conference participant endpoints.

8. ~~**Call panel name resolution**~~ ✅ Done — `resolve_participant()` extracted as standalone function with explicit parameters (6-strategy resolution chain). No more nested closure scope.

9. ~~**Bare except clauses**~~ ✅ Done — all 16 `except Exception: pass` clauses in routes.py now log at debug/warning level.

10. ~~**`session:` prefix leaking**~~ ✅ Done — added `get_api_caller_email()` for clean email. Transfer endpoints store clean email. Fixed double-prefix bug in callback logging.

---

## ~~Call Participants Table~~ ✅ Done (2026-04-08)

Replaced the reverse-engineering approach with a `call_participants` table as the source of truth. Participants tracked explicitly at every lifecycle event. `call_state.py` dropped from ~390 lines to ~95 (single DB query, zero Twilio API calls per poll).

Also completed in this session:
- **ring_attempts table** — fixes cross-worker ring tracking (gunicorn workers no longer have isolated in-memory state)
- **Every call is a conference** — converted SIP auto-ring queue answer from `<Queue>` to conference. Enables consistent recording, hold, transfer, participant tracking
- **Conference recording** — via TwiML `record="record-from-start"`, captures full call including transfers
- **Local permission service** — team management no longer depends on external Grant API
- **`_handle_participant_left()`** — DRY cleanup across all 5 call-end paths
- **Outbound call improvements** — instant dial (no blocking on customer lookup), proper "Ringing..." → "Connected" status, auto-record waits for answer
- **Old code removed** — `resolve_participant()`, `build_user_map()`, `get_conference_participants()`, `_find_agent_in_conference()`, `_deduplicate_participants()`, `_get_customer_from_call_log()`, `_build_transfer_info()`, legacy fallback path
