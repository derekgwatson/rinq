"""Personal voicemail pages (services/personal_voicemail.py).

- My Numbers "My voicemail" card: the line's OWNER only - greeting,
  email copies, messages. Every owner route re-checks ownership.
- Admin -> Personal voicemail: set lines up. Shows no messages, and any
  change emails the owner.
- /personal-greeting/...: the greeting file for Twilio's <Play>.

Registered via register(web_bp).
"""

import logging
from datetime import datetime, timezone

import pytz
from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from rinq.database.db import get_db
from rinq.services import personal_voicemail as pv
from rinq.services.auth import admin_required, get_current_user, login_required
from rinq.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

_TZ = pytz.timezone('Australia/Sydney')
MAX_GREETING_BYTES = 5 * 1024 * 1024


def _tenant_id():
    return (get_current_tenant() or {}).get('id')


def _when(iso):
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(_TZ).strftime('%a %-d %b, %-I:%M%p').replace('AM', 'am').replace('PM', 'pm')
    except Exception:
        return iso or ''


def my_voicemail_context(db, email):
    """What the My Numbers card needs, or None if this person has no line."""
    line = pv.line_for_owner(db, email)
    if not line:
        return None
    msgs = pv.messages(db, line['id'])
    for m in msgs:
        m['who'] = pv.caller_label(m.get('from_number'), m.get('caller_name'))
        m['when'] = _when(m.get('created_at'))
    return {'line': line, 'messages': msgs,
            'greeting_url': pv.greeting_url(_tenant_id(), line)}


def _sniff(audio: bytes):
    """'wav' / 'mp3' from the file's own first bytes, or None."""
    if audio[:4] == b'RIFF' and audio[8:12] == b'WAVE':
        return 'wav'
    if audio[:3] == b'ID3' or (len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0):
        return 'mp3'
    return None


def register(bp):

    # ------------------------------------------------------------ owner

    def _my_line():
        user = get_current_user()
        line = pv.line_for_owner(get_db(), user.email) if user else None
        if not line:
            abort(404)
        return user, line

    @bp.route('/my-voicemail/greeting', methods=['POST'])
    @login_required
    def my_voicemail_greeting():
        user, line = _my_line()
        f = request.files.get('greeting')
        audio = f.read(MAX_GREETING_BYTES + 1) if f else b''
        if not audio:
            return jsonify(success=False, error="no recording received"), 400
        if len(audio) > MAX_GREETING_BYTES:
            return jsonify(success=False, error="that file is over 5 MB - keep the greeting short"), 400
        ext = _sniff(audio)
        if not ext:
            return jsonify(success=False, error="it must be a WAV or MP3 file"), 400
        db = get_db()
        pv.save_greeting(db, _tenant_id(), line, audio, ext, f"session:{user.email}")
        return jsonify(success=True,
                       greeting_url=pv.greeting_url(_tenant_id(), pv.get_line(db, line['id'])))

    @bp.route('/my-voicemail/greeting/clear', methods=['POST'])
    @login_required
    def my_voicemail_greeting_clear():
        user, line = _my_line()
        pv.clear_greeting(get_db(), _tenant_id(), line, f"session:{user.email}")
        return jsonify(success=True)

    @bp.route('/my-voicemail/email-copies', methods=['POST'])
    @login_required
    def my_voicemail_email_copies():
        user, line = _my_line()
        on = 1 if request.form.get('on') == '1' else 0
        pv.update_line(get_db(), line['id'], f"session:{user.email}", email_copies=on)
        return jsonify(success=True, email_copies=on)

    @bp.route('/my-voicemail/message/<int:message_id>/audio')
    @login_required
    def my_voicemail_audio(message_id):
        user = get_current_user()
        msg = pv.message_for_owner(get_db(), message_id, user.email)
        path = pv.message_audio_path(_tenant_id(), msg)
        if not path:
            abort(404)
        resp = send_file(path, mimetype='audio/mpeg', conditional=True)
        resp.headers['Cache-Control'] = 'private, no-store'
        return resp

    @bp.route('/my-voicemail/message/<int:message_id>/delete', methods=['POST'])
    @login_required
    def my_voicemail_delete(message_id):
        user = get_current_user()
        db = get_db()
        msg = pv.message_for_owner(db, message_id, user.email)
        if not msg:
            abort(404)
        pv.delete_message(db, _tenant_id(), msg)
        return jsonify(success=True)

    # ------------------------------------------------------------ Twilio

    @bp.route('/personal-greeting/<tenant_id>/<token>.<ext>')
    def personal_greeting(tenant_id, token, ext):
        path = pv.greeting_file(tenant_id, token, ext)
        if not path:
            abort(404)
        return send_file(path, mimetype=pv.GREETING_TYPES[ext], conditional=True)

    # ------------------------------------------------------------ admin

    def _staff_and_numbers(db, lines, keep_number=None):
        staff = [s for s in db.get_all_staff_extensions() if s.get('email')]
        taken = {l['phone_number'] for l in lines} - {keep_number}
        numbers = [n for n in db.get_phone_numbers() if n['phone_number'] not in taken]
        return staff, numbers

    @bp.route('/admin/personal-voicemail')
    @admin_required
    def admin_personal_voicemail():
        db = get_db()
        lines = pv.list_lines(db)
        staff, numbers = _staff_and_numbers(db, lines)
        names = {s['email'].lower(): s.get('name_audio_text') for s in staff}
        return render_template('admin_personal_voicemail.html', lines=lines, staff=staff,
                               all_numbers=db.get_phone_numbers(), free_numbers=numbers,
                               names=names, current_user=get_current_user())

    def _form_line():
        number = request.form.get('phone_number', '').strip()
        owner = request.form.get('owner_email', '').strip().lower()
        if not number or not owner or '@' not in owner:
            return None, None, "Pick a number and a person"
        if not get_db().get_phone_number_by_number(number):
            return None, None, "That number isn't one of ours"
        return number, owner, None

    @bp.route('/admin/personal-voicemail/create', methods=['POST'])
    @admin_required
    def admin_personal_voicemail_create():
        user = get_current_user()
        db = get_db()
        number, owner, err = _form_line()
        if err:
            flash(err, 'error')
        elif pv.line_for_number(db, number):
            flash("That number is already a personal voicemail line", 'error')
        elif pv.line_for_owner(db, owner):
            flash(f"{owner} already has a personal voicemail line", 'error')
        else:
            pv.create_line(db, number, owner, f"session:{user.email}")
            db.log_activity(action="personal_voicemail_line_created", target=number,
                            details=f"Owner: {owner}", performed_by=f"session:{user.email}")
            pv.notify_owner_of_change(
                [owner], f"A personal voicemail line was set up for you on {number}. "
                         f"Your greeting and messages are on Tina's My Numbers page.", user.email)
            flash(f"Personal voicemail line set up for {owner}", 'success')
        return redirect(url_for('web.admin_personal_voicemail'))

    @bp.route('/admin/personal-voicemail/<int:line_id>/update', methods=['POST'])
    @admin_required
    def admin_personal_voicemail_update(line_id):
        user = get_current_user()
        db = get_db()
        line = pv.get_line(db, line_id) or abort(404)
        number, owner, err = _form_line()
        if err:
            flash(err, 'error')
            return redirect(url_for('web.admin_personal_voicemail'))
        other = pv.line_for_number(db, number)
        if other and other['id'] != line_id:
            flash("That number is already a personal voicemail line", 'error')
            return redirect(url_for('web.admin_personal_voicemail'))
        other = pv.line_for_owner(db, owner)
        if other and other['id'] != line_id:
            flash(f"{owner} already has a personal voicemail line", 'error')
            return redirect(url_for('web.admin_personal_voicemail'))
        changes = []
        if number != line['phone_number']:
            changes.append(f"Number: {line['phone_number']} -> {number}")
        if owner != line['owner_email'].lower():
            changes.append(f"Owner (who gets the messages): {line['owner_email']} -> {owner}")
        if changes:
            pv.update_line(db, line_id, f"session:{user.email}", phone_number=number, owner_email=owner)
            db.log_activity(action="personal_voicemail_line_changed", target=number,
                            details='; '.join(changes), performed_by=f"session:{user.email}")
            pv.notify_owner_of_change([line['owner_email'], owner], '\n'.join(changes), user.email)
            flash("Saved - the owner has been emailed about the change", 'success')
        return redirect(url_for('web.admin_personal_voicemail'))

    @bp.route('/admin/personal-voicemail/<int:line_id>/delete', methods=['POST'])
    @admin_required
    def admin_personal_voicemail_delete(line_id):
        user = get_current_user()
        db = get_db()
        line = pv.get_line(db, line_id) or abort(404)
        if request.form.get('confirm', '').strip() != line['phone_number']:
            flash("Type the line's number to confirm - removing it deletes every message on it", 'error')
            return redirect(url_for('web.admin_personal_voicemail'))
        pv.delete_line(db, _tenant_id(), line_id)
        db.log_activity(action="personal_voicemail_line_removed", target=line['phone_number'],
                        details=f"Owner: {line['owner_email']}", performed_by=f"session:{user.email}")
        pv.notify_owner_of_change(
            [line['owner_email']],
            f"Your personal voicemail line ({line['phone_number']}) was removed, with its messages.",
            user.email)
        flash("Line removed - the owner has been emailed", 'success')
        return redirect(url_for('web.admin_personal_voicemail'))
