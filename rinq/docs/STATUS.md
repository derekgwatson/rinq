# Tina - Status

> **Last updated**: 2026-04-02

## Overview

Tina is the **Watson Twilio PBX** - complete phone system management including numbers, users, recordings, and call routing.

**Core job**: Manage the entire phone system - inbound/outbound routing, SIP devices, call recordings, queues, and voicemail.

**Integration points**:
- **Oscar** -> User provisioning during onboarding
- **Olive** -> User deprovisioning during offboarding
- **Sadie** -> Voicemail tickets to Zendesk
- **Peter** -> Calls Tina's `/api/staff-phones/resolved` for staff phone number sync
- **Pam** -> Calls Tina's `/api/staff-phones/resolved` for live directory display

**Key responsibilities**:
- Phone number management
- SIP domain and credential management
- Call routing (IVR, queues, forwarding)
- Call recording management
- Voicemail with transcription
- User provisioning/deprovisioning

## Current State

### What's Working
- Phone number sync from Twilio
- SIP desk phone provisioning and management
- Mobile SIP support
- Call forwarding configuration
- Queue management with auto-ring
- Hold music configuration
- Queue callback requests (press 2 to get a callback instead of holding)
- Voicemail with transcription to Zendesk
- Call recording (email to Google Group, delete from Twilio)
- Verified caller IDs for external numbers
- Self-service My Devices page
- Call log and reporting
- Customer context pane during calls
- Paste phone numbers into softphone keypad (Ctrl+V)
- DTMF tone sending during active calls (navigate IVR menus)
- `+` dialing via keyboard or long-press `0` on dialpad
- **Token auto-refresh for browser softphone** - Twilio Device token refreshes automatically before 1-hour TTL expires; device auto-reconnects on unregistered event
- Outbound calls to 1300/1800/13xx numbers (requires Twilio High-Risk Special Services geo permission)
- Direct user-to-number assignment with working inbound routing
- Outbound caller ID auto-detected from user's assigned number
- **Extension Directory** - auto-attendant call flow action: callers dial a shared number, enter a 3-digit extension, and are connected to the staff member's devices. Caller ID preserved. Configurable fallback (voicemail, AI receptionist, or queue) for no-answer/invalid extension.
- **PWA (Phone App)** - softphone installable as a standalone web app. Dedicated phone window with compact UI. Incoming calls ring via Twilio Device. Regular website shows queue notifications with "Open Phone App" prompt.
- **System notifications** - browser notifications for incoming calls, visible even when PWA is minimized
- **Contacts tab** in softphone - searchable staff directory with click-to-call. Pulls from Peter (names, positions, sections) and merges with Tina extensions/assignments. Grouped by section. Contact name shown on dialpad when dialing from contacts. No-phone contacts greyed out. Filter pills (All/Online/Favourites). Favourites stored in localStorage with star toggle, shown as a group at top of contacts list.
- **History tab** in softphone - shows the agent's recent call history from `call_log`. Filter pills (All/Inbound/Outbound/Missed). Grouped by date (Today/Yesterday/date). Shows customer name or formatted phone number, call direction icon (colour-coded: green inbound, blue outbound, red missed), call type, queue name, time, and duration. Clicking a history card populates the dialpad for quick redial. Handles internal/extension calls gracefully. API: `GET /api/my-call-history?limit=N`.
- **Online presence** - heartbeat-based presence tracking. Softphone sends heartbeat every 30s; staff shown as online if heartbeat within 60s. Status dots on contact avatars: green (online), red (DND), grey (offline). `GET /api/presence` returns status map. Online filter excludes DND users.
- **Do Not Disturb (DND)** - staff can toggle DND from dashboard or softphone. Direct extension calls hear "[Name] is currently unavailable" then go to fallback. DND enforced centrally in `get_user_ring_settings()` — returns `ring_browser=False, ring_sip=False` when DND is on, so all ringing paths (queue, direct, extension) respect it automatically. Browser softphone also rejects incoming calls when DND is on. API: `POST/GET /api/dnd`.
- **Direct extension dialing** - softphone detects 4-digit extension numbers and routes internally (bypasses PSTN/IVR). Uses `client:identity` as callerId so recipient sees caller's name. Checks DND. Logs as `direction: 'internal'`.
- **Hold with music** - hold plays music to the caller for all call types. Queue calls use the existing conference. Non-queue calls (outbound, direct inbound) use a lazy conference: the first hold press creates a conference on-the-fly. Handles both outbound (agent=parent) and direct inbound (agent=child) call architectures.
- **Active Calls tab** in softphone - real-time view of who's on a call. Auto-refreshes every 5s. Shows agent name, caller, queue, duration. Dismiss button for stale entries. Auto-cleans stale ringing (>2min) and answered calls older than 2 hours. Browser sends `POST /api/voice/call-ended` from `endCall()` to reliably mark calls as completed (Twilio status callbacks don't always fire for conference-based calls). Resolves SIP identities to staff names.
- **Queue alert mute** - bell toggle next to DND silences queue beep sounds and notifications. Persists in localStorage. Visual queue banner still shows.
- **Dialpad helpers** - Paste, backspace, and Clear buttons between the number display and keypad. Paste reads from clipboard and strips non-digits.
- **Transfer: Blind, Warm, and 3-Way** for ALL call types. Non-queue calls (direct inbound, outbound) are escalated to a conference on-the-fly when warm/3-way is initiated. Transfer targets show all active staff from Peter with extension numbers and busy indicators.
  - **Blind**: immediate redirect, agent disconnects
  - **Warm**: customer on hold, agent consults target privately, then completes or cancels
  - **3-Way**: customer stays on line, target joins, all 3 talk. Agent drops when ready, call continues between other parties
- **Incoming call suppression** - silently rejects incoming calls when agent is already on a call or has DND enabled, so the call routes to the next available person
- **Queue alert suppression** - `playQueueAlert()` checks `currentCall` and `isDndEnabled` centrally, so beeps never play while on a call or in DND mode
- **Polycom VVX 300 setup guide** - step-by-step web page at `/my-devices/vvx300-setup` with user's SIP credentials pre-filled. Reference doc at `tina/docs/polycom-vvx-setup.md`
- **Phone page cache busting** - `/phone` route returns `Cache-Control: no-cache` headers so browsers always load latest JS
- **Staff sync from Peter** - visiting `/admin/staff` auto-creates extensions for all active Peter staff. Migrates Peter's extension numbers and mobile forwarding. API: `POST /api/staff/sync`.
- **Resolved staff phone numbers** - New endpoint `GET /api/staff-phones/resolved` returns each staff member's resolved external phone number (extension directory number) and extension. This is the source of truth for staff phone numbers; called by Peter (for sync) and Pam (for live directory display).
- **Extension info on softphone** - shows user's extension number and dial-in number on the phone pad

### Phone Number Routing

Phone numbers support two mutually exclusive routing modes:

1. **Call Flow** - For shared/team numbers. Routes via IVR, queues, schedules, extension directory, etc. Section dropdown available for caller ID matching.
2. **User Assignment** - For direct lines. Rings the assigned user's browser softphone and SIP devices. Outbound caller ID is automatically set from the assignment (no section needed).

### Extension Directory

A call flow `open_action` of `extension_directory` creates an auto-attendant:
1. Caller dials the shared number, hears a prompt (custom audio or TTS)
2. Caller enters a 4-digit extension via DTMF (can type immediately without waiting)
3. Extension looked up in `staff_extensions` table; if DND enabled, goes straight to fallback with unavailable message
4. Otherwise user's devices dialed (browser, SIP, mobile forwarding)
5. Caller ID of the original caller is preserved (not the switch number)
6. One retry on invalid extension, then configurable fallback

**Config columns on `call_flows`:** `extension_prompt_audio_id`, `extension_no_answer_action`

**Webhook endpoints:** `/api/voice/extension-dial`, `/api/voice/extension-no-answer`

### Phone App (PWA)

The softphone is installable as a Progressive Web App. Works on desktop (Chrome/Edge) and mobile.

- **Manifest:** `static/manifest.json`, scope `/phone`
- **Service worker:** `static/sw.js` (minimal, pass-through)
- **Standalone detection:** CSS `@media (display-mode: standalone)` hides nav bar, install prompts, help section
- **Compact UI:** smaller header, tighter dialpad, no footer in standalone mode
- **System notifications:** browser notifications for incoming calls (visible when PWA is minimized)
- **Install prompt:** shown on all website pages (dismissable, re-shows after 24h) and prominently on phone page

**Call receiving behaviour:**
- **PWA open** → calls ring (Twilio Device registered)
- **Website, on phone tab** → calls ring (phone page device, fallback)
- **Otherwise** → calls missed (queue notifications only via polling)

**Website phone page behaviour:**
- If PWA installed: shows "use the Tina Phone app from your taskbar" message (no button)
- If PWA not installed: shows "Install App" button with install prompt
- Warns users that Chrome's "Open in app" address bar button will close the tab

The admin UI (`/admin/phone-numbers`) presents these as a clear either/or choice when configuring an unconfigured number.

**Outbound caller ID priority:**
1. User's manually chosen default (My Devices page)
2. Directly assigned number (from `phone_assignments` table)
3. Section-based matching (user's Peter section matched to a phone number's section)
4. System default

### Known Issues / Limitations
- **SIP registration delays** - 403 errors during credential list propagation
- **External forwarding (`forward_to` on phone numbers) is broken** - Calls to forwarded numbers fail. Needs investigation.

### Data Quality Notes
- Phone numbers synced from Twilio
- SIP credentials use memorable passwords (purple-tiger-sunset-42)
- Call recordings stored temporarily, emailed, then deleted
- Phone assignments (store numbers, queue lines) are operational/queue routing, not personal DIDs. If personal DIDs are ever needed, a `did_number` field should be added to `staff_extensions` table.

## Architecture

### Key Tables

| Table | Purpose |
|-------|---------|
| `phone_numbers` | Managed phone numbers from Twilio |
| `phone_assignments` | Direct user-to-number assignments (receive + make calls) |
| `call_flows` | IVR routing definitions (greeting, schedule, queue) |
| `users` | User/extension mappings |
| `sip_devices` | SIP device credentials and config |
| `queues` | Call queue definitions |
| `queue_members` | Agent membership in queues |
| `recordings` | Call recording metadata |
| `staff_extensions` | Staff extension numbers, DND, forwarding, PAM visibility, heartbeat |
| `call_log` | Comprehensive call history (conference_name, child_call_sid for hold, transfer_* fields for non-queue transfers) |
| `verified_caller_ids` | External numbers for caller ID |

### Main Services

**TwilioService** (`services/twilio_service.py`)
- Phone number management
- SIP credential management
- Call routing configuration

**RecordingService** (`services/recording_service.py`)
- Recording webhook handling
- Email to Google Group
- Recording cleanup

**CallerEnrichmentService** (`services/caller_enrichment.py`)
- Enriches incoming call data with caller context

**TransferService** (`services/transfer_service.py`)
- Blind, warm, and 3-way transfers for all call types
- Conference escalation for non-queue calls
- Transfer state tracked in `queued_calls` (queue) or `call_log` (non-queue)

**DriveService** (`services/drive_service.py`)
- Google Drive integration for call recordings/files

**GmailService** (`services/gmail_service.py`)
- Email integration for recording delivery

**ReportingService** (`services/reporting_service.py`)
- Call reporting and analytics

**TtsService** (`services/tts_service.py`)
- Text-to-speech for IVR prompts and greetings

### Call Flow
1. Inbound call hits Twilio
2. Tina webhook determines routing (IVR, queue, direct)
3. Queue calls ring agents (auto-ring or manual)
4. Voicemail escape option for callers in queue
5. Recordings emailed to group, deleted from Twilio

### Recording Configuration
- Recordings emailed to: call-recordings@watsonblinds.com.au
- Default recording enabled for new users

## Next Steps

### High Priority
- [ ] **Investigate broken external forwarding** - `forward_to` on phone numbers doesn't work (call fails with short tones). Needs debugging of TwiML generation and Twilio configuration.
- [ ] **Warm-transfer reunite path is broken** - When a warm-transfer consult ends, the customer should either continue with agent 2 (on "complete transfer") or return to agent 1 (on "cancel transfer"). Neither works — customer gets dropped both ways. Two confirmed occurrences 2026-04-15: Suzette → Stephanie (wrong ext, consult ended, customer dropped) and Suzette → Cid Cortes (correct ext, 83s consult, customer dropped after consult ended). Investigate `transfer_service.py` and `transfer_routes.py` completion/cancel flows.

### Medium Priority
- [ ] **SIP caller ID display name** - When ringing SIP desk phones, pass the customer's resolved name as the SIP INVITE From display-name so desk phones show "Greta Balmaceda" instead of "+61404181129". Affects Binu (deskphone user) — she currently can't see who's calling. Implementation: update `_ring_targets_into_conference` to include display name in `from_` when target is `<Sip>`, using the customer name already available in `queued_calls`/`call_log`/Clara lookup. Verify Twilio's accepted format before shipping. Doesn't help softphone or mobile-forwarded agents (display names stripped by carriers).
- [ ] **Custom audio for queue voicemail escape** - Currently uses robotic TTS voices. Add ability to use custom audio files for: (1) the prompt telling callers they can press 1 to leave voicemail, (2) the voicemail greeting after they press 1. Implementation: add `voicemail_escape_audio_id` and `voicemail_greeting_audio_id` columns to queues table (both nullable FK to audio_messages), update queue edit form with audio dropdowns, update TwiML generation to use custom audio when configured.
- [ ] Add call analytics dashboard
- [ ] **Clean up legacy `can_make`/`can_receive` columns** - These are still in the `phone_assignments` table but `can_make` was never used and checkboxes have been removed from the UI. Both are now always set to `True`. Could simplify the schema by removing them, but low priority since they don't cause harm.

### Low Priority / Future
- [x] ~~**Contacts: Favourites**~~ - Done. Star toggle on contacts, stored in localStorage. Favourites filter pill and pinned section at top.
- [ ] **Contacts: Recents** - Show recent calls as quick-dial entries.
- [ ] **Contacts: Standalone page** - Full-page `/contacts` directory with richer detail (department, photo, all assigned numbers). Click opens softphone with number pre-filled.
- [ ] **Contacts: External contacts** - Extend address book to include external contacts (customers, suppliers) in addition to internal staff.
- [ ] Add click-to-call from CRM
- [ ] Add call sentiment analysis

## Recent Sessions

### 2026-04-02
- **Added staff phone resolution endpoint** - New `GET /api/staff-phones/resolved` endpoint that returns each staff member's resolved external phone number (extension directory number) and extension. This is now the source of truth for staff phone numbers, called by Peter (for sync) and Pam (for live directory display). Phone assignments represent operational/queue routing, not personal DIDs.

### 2026-03-30
- **Fix transfer to extensions for queue/warm calls** - `blind_transfer` and `warm_transfer_start` were treating 4-digit extensions as phone numbers (+6816). Now use `_is_extension()` and `_build_extension_dial_twiml` consistently (was only in `blind_transfer_direct`).
- **Fix active calls not clearing** - conference-based queue calls don't reliably trigger Twilio status callbacks, so `call_log.ended_at` was never set. `endCall()` now sends `POST /api/voice/call-ended` as the primary signal. Stale cleanup tightened from "before today" to "> 2 hours". Transfer modal polls active calls every 5s while open.
- **Centralize DND enforcement** - moved DND check into `get_user_ring_settings()` in db.py. Returns `ring_browser=False, ring_sip=False` when DND is on. Removed scattered DND checks. Browser softphone also rejects calls during DND.
- **Queue alert beeps suppressed during calls/DND** - single guard in `playQueueAlert()` checks `currentCall` and `isDndEnabled`.
- **Polycom VVX 300 setup** - new doc `tina/docs/polycom-vvx-setup.md` and web page renamed to `/my-devices/vvx300-setup` (was generic `desk-phone-setup`). Added missing fields to match doc.
- **Phone page cache busting** - `Cache-Control: no-cache` headers prevent browsers caching stale JS.
- **Dialpad clears on hangup** - `endCall()` resets `phoneNumber` and `contactName`.

### 2026-03-27
- **Unified transfer system** - Blind, warm, and 3-way transfers now work for ALL call types (queue, direct inbound, outbound). Non-queue calls escalated to conference on-the-fly. Transfer targets pulled from Peter (all active staff) with extension numbers and busy indicators.
  - Migration 052: transfer tracking columns on `call_log`
  - Key architectural insight: for direct inbound calls, agent is the child call — must redirect agent before customer to avoid killing the Dial bridge
  - Transfer key = customer call SID (matches call_log), agent SID passed separately for redirects
- **Active Calls tab** - real-time view of who's on a call with auto-refresh, dismiss, stale cleanup
- **Queue alert mute toggle** - bell icon next to DND
- **Dialpad paste/clear buttons** - between number display and keypad
- **Incoming call suppression** when already on a call
- **Hold fix for direct inbound** - handles parent/child architecture correctly
- **dial-status callbacks on all Dial elements** - prevents stale call log entries
- **Many transfer bug fixes** - modal closing on disconnect, cancel not killing agent, missing From number, error visibility, 3-way cleanup

### 2026-03-19
- **Online presence tracking** - heartbeat-based system for showing who's online in the softphone contacts
  - Migration 047: `last_heartbeat` column on `staff_extensions`
  - `POST /api/heartbeat` - softphone sends every 30s
  - `GET /api/presence` - returns online/DND status for all staff (online = heartbeat within 60s)
  - Status dots on contact avatars: green (online), red (DND), grey (offline)
  - Presence polled every 15s, auto-updates visible contacts
- **Direct extension-to-extension calling** - softphone detects 4-digit numbers and routes internally
  - Bypasses PSTN/IVR: looks up extension, checks DND, builds dial targets, dials directly
  - Uses `client:identity` as callerId so recipient sees caller's name (not a phone number)
  - Internal caller name resolution: `resolveInternalCaller()` converts `client:` identity back to staff name
  - Contacts pre-loaded on init for caller name resolution
  - Logged as `direction: 'internal'` in call_log
- **Contacts tab enhancements**
  - Filter pills: All / Online / Favourites
  - Favourites: star button on each contact, stored in localStorage, shown as pinned section at top
  - Online filter excludes DND users
  - Consolidated filtering via `applyFilterAndRender()`
  - Contextual empty states per filter
  - Clarified click affordance: card title says "Select on dialpad", buttons say "Dial" not "Call"

### 2026-03-17 (session 2)
- **Do Not Disturb** - toggle from dashboard (banner with icons) and softphone (toggle switch in info bar)
  - `POST/GET /api/dnd` endpoints with session auth
  - Direct extension calls: caller hears "[Name] is currently unavailable", then fallback (voicemail/AI/queue)
  - Queue calls: DND users silently skipped from ring list
  - `_build_dial_targets` checks DND before adding any targets (browser, SIP, mobile)
  - Migration 044: `dnd_enabled` column on `staff_extensions`
- **Softphone contacts improvements**
  - Phone fallback chain: Tina assignment > mobile > fixed line > extension
  - Contact name shown on dialpad when dialing from contacts (clears on manual typing)
  - No-phone contacts greyed out with "No phone" label, not clickable
  - Left-aligned contacts list (was inheriting center from phone panel)
  - Sorted by name within each section
  - `event.stopPropagation()` on icon buttons so clicking extension vs phone works correctly
- **Extension info on softphone** - info bar shows extension number and dial-in number
- **Hold for all call types** (later revised in session 3 — see below)
  - Outbound calls initially used pre-conferencing for hold (broke ringback tone)
  - Migration 045: `conference_name` column on `call_log`
- **Staff sync from Peter** - auto-runs on `/admin/staff` page load
  - Creates extensions for all active Peter staff who don't have one
  - Uses Peter's extension number when available (falls back to auto-assign)
  - Sets mobile forwarding (mode: always) from Peter's `phone_mobile`
  - Only updates records created by `system:sync`, never user-configured ones
  - API: `POST /api/staff/sync` for automation
- **PAM integration** - extension directory number shown for staff with Tina extension but no fixed line
  - Tina's directory-overrides endpoint now returns `all_extensions` (not just active)
- **Bug fixes**
  - Fixed zero button dialing on hover (missing `zeroPressed` guard)
  - Fixed DND API using `session:email` prefix instead of bare email
  - Fixed HTML entity rendering in DND banner (Jinja auto-escape)
  - Cleaned up "voicemail is not configured" message to caller-friendly "Please try again later"
- **Peter**: removed extension-must-match-fixed-line validation (extensions now managed by Tina)

### 2026-03-18 (session 4)
- **Token auto-refresh** - Added `tokenWillExpire` event handler on Twilio Device that fetches fresh token before 1-hour TTL expires. Added `unregistered` handler for auto-reconnect. Fixes call button becoming unresponsive when token expires.
- **Better error on unresponsive call button** - `makeCall()` JS function now shows visible error and attempts device reinitialization when device is null, instead of silently returning.
- **Server-side hangup fallback** - New `/api/voice/hangup` POST endpoint (login_required) terminates call via Twilio REST API. `hangupCall()` JS function sends this as background request alongside SDK disconnect, ensuring both call legs terminate even if browser-side WebRTC disconnect doesn't propagate to Twilio server.
  - Fixes "screen shows disconnected but customer still on the line" issue

### 2026-03-18 (session 3)
- **Fix outbound ringback tone** - pre-conferencing all outbound calls (from session 2) killed the ringback tone. Staff heard silence/hold music instead of ringing when calling out. Reverted to simple `<Dial><Number>`.
- **Lazy conference for hold** - hold still works via on-demand conference creation:
  1. Agent presses hold → find child call via Twilio API → redirect child to hold music
  2. `<Dial>` bridge breaks → `dial-status` detects pending conference → puts agent in conference
  3. Unhold → redirect child call into the conference → reconnected
  4. Subsequent hold/unhold uses conference participant API
  - Migration 046: `child_call_sid` column on `call_log`
  - Hold API: `_hold_outbound_call()` handles lazy setup; existing conference path unchanged for queue calls

### 2026-03-17 (session 1)
- **Contacts tab in softphone** - searchable staff address book integrated into the phone panel
  - New `/api/contacts` endpoint merges Peter staff directory with Tina extensions/assignments
  - Tab switcher (Dialpad / Contacts) in phone panel
  - Contacts grouped by section, showing name, position, extension
  - Search filters by name, section, position, email
  - Click-to-call: clicking a contact fills the dialpad with their number/extension
  - Direct call and extension buttons on each contact card
  - Locked to dialpad tab during active calls (for DTMF)
  - Compact layout support for PWA mode
- **Smart staff activation** on /admin/staff
  - Usage signals shown: call count (with last call date), queue memberships, phone assignments, SIP credentials
  - Auto-activation: staff with call history, queue membership, or phone assignment auto-activated on page load
  - Manual overrides locked (`is_active_locked`): Activate/Deactivate buttons always lock, preventing auto-activation from overriding
  - Unlock button to hand control back to auto-activation
  - Inline extension number editing for admins
  - Migration 043: `is_active_locked` column on `staff_extensions`
- **PAM directory overrides** - PAM calls Tina's `/api/pam/directory-overrides` to replace dead old VoIP numbers with extension directory number for Tina-active staff. Uses `staff_extensions.is_active` (admin-controlled flag, not SIP credentials).

### 2026-03-16
- **Queue Callback Feature** - callers waiting in queue can press 2 to request a callback
  - Caller hangs up but doesn't lose their place; agent sees pending callbacks in dashboard
  - Per-queue toggle: `offer_callback` with configurable `callback_threshold` (seconds before offering)
  - Unified queue escape handler: press 1 for voicemail, press 2 for callback
  - API endpoints: GET `/api/queue/callbacks`, POST `.../claim`, `.../complete`, `.../fail`
  - Phone dashboard shows callback requests with Call Back / Done / Failed actions
  - Admin UI: offer callback checkbox with threshold setting on queue edit form
- **Extension Directory** - new call flow action for auto-attendant with 3-digit extensions
  - Migration 040: `extension_prompt_audio_id`, `extension_no_answer_action` on `call_flows`
  - Webhooks: `/api/voice/extension-dial`, `/api/voice/extension-no-answer`
  - Admin UI: open action dropdown with extension directory option and settings
  - Caller ID preserved from original caller, not the switch number
  - Retry on invalid extension, configurable fallback (voicemail/AI/queue)
- **PWA Phone App** - softphone as installable standalone web app
  - `static/manifest.json` (scope: `/phone`), `static/sw.js`, icons
  - Compact layout: no nav bar, smaller header/dialpad/padding, no help section
  - Customer context pane hidden by default in PWA, shown during active calls
  - Twilio Device registered in PWA and on website `/phone` page only
  - System notifications for incoming calls (visible when PWA is minimized)
  - Install prompt on website pages; phone page shows context-aware message (install vs open)
  - Warns about Chrome "Open in app" button closing the browser tab
  - Non-phone URLs redirect to `/phone` in standalone mode
- **Softphone UI improvements**
  - Fixed DTMF: dialpad/keyboard now sends tones during active calls via `sendDigits()`
  - Added `#` button to dialpad (was missing, backspace was in its place)
  - Added `+` dialing: keyboard `+` key and long-press `0` on dialpad
  - Compact dialpad for both website and PWA (smaller buttons, tighter spacing)
  - Removed "Browser Softphone" heading
- Fixed outbound calls to 1300/1800/13xx numbers: added E.164 formatting rules and enabled Twilio High-Risk Special Services geo permission
- Fixed URL encoding of phone numbers with `+` in extension directory webhook URLs
- Hid "When Closed" section in call flow admin when schedule is 24/7
- Added `open_action` dropdown to call flow admin (was hardcoded to queue)

### 2026-02-09
- Phone assignment improvements: direct outbound caller ID assignment, auto-set section from Peter, simplified user assignment UI, wired phone_assignments into incoming call routing

### 2026-01-22
- Created this STATUS.md document

### Recent commits (Dec 2025 - Jan 2026)
- Fix SIP devices not ringing after schema simplification
- Refactor admin page to tiles + fix staff name formatting
- Add shared CSS context processor
- Simplify device management and consolidate caller ID
- Add mobile SIP support and fix recording staff visibility
- Add customer context pane to phone UI
- Fix mobile devices not ringing for inbound queue calls
- Fix voicemail recordings appearing in recordings tab
- Add sync from Twilio for verified caller IDs
- Add voicemail transcription with Zendesk ticket updates
- Add UX improvements to templates
- Add verified caller IDs for external numbers
- Add self-service My Devices page
- Add queue auto-ring feature
- Add SIP desk phone routing for inbound calls
- Add call_log table for tracking ALL calls
- Add comprehensive call reporting

---

*This is a living document. Update it when making significant changes to Tina.*
