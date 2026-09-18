"""Transcribe voicemails whose transcription never arrived, and post it to the ticket.

Written for the 2026-09-10 → 09-18 outage: the OpenAI account ran out of
credit, so ``WhisperService.transcribe`` returned None for every voicemail and
the ticket was created with "(Transcription pending or unavailable)". The
audio survives — nothing deletes a voicemail from Twilio until a transcription
lands (see ``transcription_handler``) — so the backlog is recoverable.

Safe to re-run: a recording that already holds a transcription is skipped, so
a partial run resumes where it stopped. It never deletes anything from Twilio;
recovering the text is a separate question from reclaiming the storage.

Usage (on the server, from /var/www/rinq):
    venv/bin/python rinq/scripts/backfill_voicemail_transcriptions.py --since 2026-09-10
    venv/bin/python rinq/scripts/backfill_voicemail_transcriptions.py --since 2026-09-10 --commit
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests
from flask import g

# Importing the app wires up the integrations singletons (ticket service etc).
from rinq.app import app
from rinq.integrations import get_ticket_service
from rinq.integrations.openai.whisper import get_whisper_service
from rinq.tenant.context import get_tenant_db, get_twilio_config, iter_tenant_contexts

# Staff see this on a ticket that has been quiet for days, so it has to say why
# it just moved. The live path's wording is in `transcription_handler`.
COMMENT_HEADER = (
    "📝 **Voicemail Transcription** (added late — transcription was unavailable "
    "when this voicemail arrived):"
)


def _download(recording_url):
    auth = (get_twilio_config('twilio_account_sid'), get_twilio_config('twilio_auth_token'))
    response = requests.get(f"{recording_url}.mp3", auth=auth, timeout=60)
    response.raise_for_status()
    return response.content


def backfill(since, commit, limit, pause):
    whisper = get_whisper_service()
    if not whisper.is_configured:
        print("OPENAI_API_KEY is not set — nothing to do.")
        return 1

    tickets = get_ticket_service() if commit else None
    if commit and not tickets:
        print("No ticket service configured — refusing to run with --commit.")
        return 1

    totals = {'found': 0, 'transcribed': 0, 'posted': 0, 'no_speech': 0, 'failed': 0}
    aborted = None

    for tenant in iter_tenant_contexts():
        if aborted:
            break
        db = get_tenant_db()
        rows = db.get_untranscribed_voicemails(since)
        if not rows:
            continue
        if limit:
            rows = rows[:limit]
        totals['found'] += len(rows)
        print(f"\n=== {tenant['id']}: {len(rows)} voicemail(s) to transcribe ===")

        for row in rows:
            sid = row['recording_sid']
            label = f"{row['created_at'][:16]} from {row['from_number']} (ticket #{row['ticket_id']})"
            try:
                audio = _download(row['recording_url'])
                result = whisper.transcribe(audio, filename=f"voicemail_{sid}.mp3")
            except Exception as e:
                totals['failed'] += 1
                print(f"  FAILED  {label}: {e}")
                continue

            if not result.ok:
                # Leave the row untouched either way, so a later run can retry.
                # A fault is ours to fix; "no speech" is just a silent message.
                bucket = 'failed' if result.fault else 'no_speech'
                totals[bucket] += 1
                print(f"  {'FAILED ' if result.fault else 'NO TEXT'} {label}: {result.error}")
                if result.fault:
                    # A service-wide fault (no credit, bad key) will hit every
                    # remaining row too — stop rather than burn through 250 of
                    # them printing the same line. Already-posted rows stand;
                    # re-running picks up from here.
                    aborted = result.error
                    break
                continue

            text = result.text
            totals['transcribed'] += 1
            if not commit:
                print(f"  WOULD POST {label}: {text[:90]}{'…' if len(text) > 90 else ''}")
                continue

            db.update_recording_transcription(sid, text)
            if tickets.add_comment(str(row['ticket_id']), f"{COMMENT_HEADER}\n\n{text}", public=False):
                totals['posted'] += 1
                db.log_activity(
                    action="transcription_backfilled",
                    target=str(row['ticket_id']),
                    details=f"Recording {sid}",
                    performed_by="tina",
                )
                print(f"  POSTED  {label}")
            else:
                # The text is saved either way; only the ticket note is missing.
                totals['failed'] += 1
                print(f"  SAVED, TICKET POST FAILED  {label}")
            time.sleep(pause)

    print(
        f"\n{'Applied' if commit else 'Dry run'}: found {totals['found']}, "
        f"transcribed {totals['transcribed']}, posted {totals['posted']}, "
        f"no speech {totals['no_speech']}, failed {totals['failed']}"
    )
    if aborted:
        print(f"STOPPED EARLY — {aborted}. Fix that, then re-run: the rest are untouched.")
        return 1
    if not commit:
        print("Nothing was written. Re-run with --commit to apply.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True, help='ISO date, e.g. 2026-09-10')
    parser.add_argument('--commit', action='store_true',
                        help='Write transcriptions and post ticket notes (default: dry run)')
    parser.add_argument('--limit', type=int, default=0, help='Stop after N per tenant')
    parser.add_argument('--pause', type=float, default=0.5,
                        help='Seconds between writes, to stay under rate limits')
    args = parser.parse_args()

    with app.app_context():
        g.tenant = None
        return backfill(args.since, args.commit, args.limit, args.pause)


if __name__ == '__main__':
    sys.exit(main())
