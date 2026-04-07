# Rinq Known Issues

Issues found during testing, noted for follow-up. Not blocking production.

## 1. "On a call" presence lingers after blind transfer
**Reproduce:** Agent 1 blind transfers to Agent 2. Check transfer targets on Agent 2's side — Agent 1 still shows "on a call" for ~5-10 seconds.
**Cause:** Twilio active calls API has a 5-second cache. Agent 1's call takes a moment to fully terminate on Twilio's side.
**Impact:** Cosmetic. Clears itself after cache refresh.

## 2. Internal extension caller ID shows Twilio number
**Reproduce:** Agent 1 dials Agent 2's extension from the browser. Agent 2 sees "6663" (last digits of Twilio number) instead of Agent 1's name.
**Cause:** REST API `calls.create()` requires a real phone number as `from_`. Can't pass a `client:` identity. The browser's `resolveInternalCaller` tries to match the number to a contact but Twilio system numbers aren't in the contacts list.
**Impact:** Cosmetic. Agent 2 doesn't know who's calling internally.

## 3. Hold after blind transfer may fail
**Reproduce:** Queue call → Agent 1 answers → Agent 1 blind transfers to Agent 2 → Agent 2 tries Hold → "Conference not found or not active"
**Cause:** The transfer conference may not be properly tracked. The conference name is stored but the Twilio conference might have ended during the handover, or the participant SIDs don't match.
**Impact:** Hold doesn't work on the receiving end of a blind transfer. Resume to get the customer back isn't possible.

## 4. Recordings restart on each transfer
**Reproduce:** Start recording on a call, then blind transfer. New recording starts on the receiving agent's side. Original recording is a separate file.
**Cause:** Each transfer ends the old call and creates a new one. Recording is per-call, not per-conference.
**Fix:** Record the conference instead of individual calls. Separate improvement.

## 5. Conference participant panel missing for non-queue calls
**Reproduce:** Make an outbound call or receive a direct inbound call. The "In this call" participant panel doesn't appear (but works for queue calls).
**Cause:** `my-call-state` polling only searches `queued_calls` for conference info. Conference-first calls store conference names in `call_log` which isn't checked by the polling endpoint.
**Fix:** Update `my-call-state` to also search `call_log` for conference info.

## 6. Queue answer race condition — agent hears brief hold music
**Reproduce:** Answer a queue call from the browser softphone. Agent may hear 1-2 seconds of hold music before the caller connects.
**Cause:** The caller redirect from queue to conference is async. Agent can join the conference before the redirect completes, sitting alone briefly.
**Mitigation:** Added "Connecting." Say message to delay agent join (2026-04-07). Full fix requires restructuring to wait for redirect confirmation.
**Impact:** Minor UX annoyance. Call connects after a brief delay.
