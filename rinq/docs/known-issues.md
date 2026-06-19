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

## 7. Warm transfer to scheduling recipient drops mid-consult
**Reproduce:** Agent warm-transfers a call to a scheduling-team member (no queue — dialed as an individual, e.g. Stephanie, Zeel). The recipient's consult leg ends mid-consult; server cancels the transfer and rejoins the customer to the originating agent. Originating agent experiences it as a failed transfer.
**Verified case:** 2026-06-05 ~10:35am AEST — Hazel → Stephanie (ext 1104), consult ran 62s then Stephanie's leg ended. Customer (Kaye Wilson) was rejoined to Hazel, not dropped — the rejoin safety net worked.
**Cause:** Not yet confirmed. The recipient leg ended cleanly: carrier `status=completed`, only a bare `sdk-disconnect` (no warning/reconnecting/error despite handlers being wired at `phone.html:_wireCallDiagnostics`), and no server-side hangup by the recipient. Evidence leans *against* media throttling. Recipient's tab was backgrounded (`visibility:hidden`); the open question is whether Chrome froze the tab (which kills WebRTC without firing SDK warnings) vs. a clean disconnect from another path. Voice Insights Advanced is off on the account (no media telemetry, not retroactive).
**Instrumentation (commit dce6105):** Page-lifecycle events (`page-freeze`/`page-resume`/`visibility`/`pagehide`) now logged into the per-call diagnostic buffer. Next recurrence: a `page-freeze` immediately before `sdk-disconnect` confirms backgrounded-tab death (→ push recipients to the PWA); no freeze → hunt what disconnected the leg.
**Mitigation:** Scheduling recipients advised to use the PWA (separate window, not throttled). Unproven until tracing catches a freeze.

**Update 2026-06-17 — tab-freeze ruled out for this case; scope quantified; PWA experiment running:**
- Recurrence: Hazel → Rhae (ext 6883), 3 consecutive warm transfers, consults ran 56s/86s/25s then the recipient leg dropped each time. Rejoin worked, customer not lost. Hazel has a long history of *completing* warm transfers — not training.
- Recipient diagnostics on all 3: `visibility:"visible"`, `online:true`, bare `sdk-disconnect`, and crucially **no `page-freeze`** in the buffer (the dce6105 instrumentation fired nothing). → backgrounded-tab-freeze is RULED OUT for a foregrounded tab; the WebRTC media leg dropped while visible. Points at recipient network/connection.
- **Scope: NOT systemic — concentrated in ~4 (likely remote) scheduling recipients.** Per-recipient mid-consult drop rate, last 6 wks: Rhae 28% (102 received), Zeel 25% (75), Cid 21% (74), Stephanie 23% (42) vs controls Philip 8% (24), Brittany 12% (24). Baseline ~8-12% is mostly legitimate consult-then-cancel; the 4 sit 2-3× above it. Steady for ~2 months — under-reported because the rejoin keeps the customer, so agents silently retry/call direct.
- **Experiment in progress:** Rhae moved browser→PWA on 2026-06-17. **Re-check her drop rate ~2026-06-24.** If it falls to ~10% baseline → roll PWA to Zeel/Cid/Stephanie, no code change. If it stays ~25% on PWA → it's their underlying network (IT/connectivity), still not a Rinq code fix. Only if both fail is consult-leg hardening justified: ring the recipient's full device set (browser + SIP + mobile `forward_to`) like blind transfer's `_build_extension_dial_twiml` already does — warm currently rings ONLY `client:` — and/or auto re-ring a mid-consult drop instead of cancelling.

**Update 2026-06-19 — recurrence on Rhae; PWA NOT confirmed measurable; instrumentation added:**
- Recurrence (reported by Shristi): Shristi → Rhae (6883), customer +61424826161, answered 11:24:35 AEST. Consult ran 61s, Rhae's leg `sdk-disconnect` (path:incoming), then Shristi cancelled — she experienced it as "Hand off says *Completing…* but won't complete" and told the customer scheduling would call back. Two more Shristi/Hazel→Rhae drops within the same 5 min (142s, 86s consults). All three: `visibility:"visible"`, `online:true`, **no `page-freeze`** — same foregrounded network-side signature as 2026-06-17.
- **Can we confirm Rhae was on the PWA? NO.** The `/api/voice/call-diagnostic` payload captured `ua`/`visibility`/`online`/`connection` but **not display-mode** — and a PWA sends the *identical* UA to a browser tab. So the experiment's core variable was never recorded; "Rhae is on the PWA" was an assumption, not a measurement.
- **Fix shipped this session:** client now sends `display_mode: "standalone"|"browser"` (via `matchMedia('(display-mode: standalone)')` + iOS `navigator.standalone`) in the diagnostic payload. Going forward, every drop is attributable to PWA-vs-tab. Grep: `Call diagnostic .* "display_mode": "standalone"`.
- **Provisional readout (small n, and unconfirmed she's actually on PWA):** warm transfers to Rhae since Jun 17 = 6 drops / 10 received (~60%) vs 25/88 (~28%) before. PWA is **not** reducing her drops — consistent with underlying-network rather than tab/PWA. Two next steps: (1) verify with Rhae she's running the *installed* PWA (logs will now show it); (2) if `display_mode:standalone` drops stay high, it's her connection → either IT/network remediation or the gated consult-leg hardening (ring full device set incl. mobile `forward_to`). Re-check still ~2026-06-24 but now with a real PWA-vs-tab signal.

**Re-check query** (per-recipient drop rate; against `data/tenants/watson/rinq.db`):
```sql
WITH starts AS (SELECT substr(details, instr(details,'for ')+4, 34) AS call_sid, target AS ext
  FROM activity_log WHERE action='call_transfer_warm_start' AND performed_at > DATE('now','-42 days')),
fails AS (SELECT target AS call_sid FROM activity_log
  WHERE action='call_transfer_failed' AND details LIKE '%: completed' AND performed_at > DATE('now','-42 days'))
SELECT s.ext, COUNT(*) received, SUM(s.call_sid IN (SELECT call_sid FROM fails)) drops
FROM starts s WHERE s.ext GLOB '[0-9][0-9][0-9][0-9]' GROUP BY s.ext HAVING received>=10 ORDER BY received DESC;
```
