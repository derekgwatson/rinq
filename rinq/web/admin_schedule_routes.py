"""Schedule, holiday, and template admin routes.

Extracted from web/routes.py. Registered via register(web_bp).
"""

import json
import logging
from flask import request, redirect, url_for, flash, jsonify, render_template
from rinq.services.auth import admin_required, get_current_user
from rinq.database.db import get_db
from rinq.config import config
from rinq.web.util import flash_error

logger = logging.getLogger(__name__)

def _audit_tag(user):
    return f"session:{user.email}"

def register(bp):
    """Register schedule/holiday/template admin routes on the given blueprint."""

    @bp.route('/admin/schedule/create', methods=['POST'])
    @admin_required
    def create_schedule():
        """Create a new schedule with business hours."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        timezone = request.form.get('timezone', 'Australia/Sydney').strip()
    
        # Parse business hours from form
        business_hours = {}
        for day in ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']:
            open_time = request.form.get(f'{day}_open', '').strip()
            close_time = request.form.get(f'{day}_close', '').strip()
            if open_time and close_time:
                # Normalize to HH:MM (zero-pad hours for consistent string comparison)
                open_time = open_time.zfill(5)  # "9:00" -> "09:00"
                close_time = close_time.zfill(5)
                business_hours[day] = {'open': open_time, 'close': close_time}
    
        # Parse closure defaults
        default_closure_action = request.form.get('default_closure_action', '').strip() or None
        default_closure_audio_id = request.form.get('default_closure_audio_id', '').strip()
        default_closure_audio_id = int(default_closure_audio_id) if default_closure_audio_id else None
        default_closure_forward_to = request.form.get('default_closure_forward_to', '').strip() or None
    
        if not name:
            flash("Schedule name is required", "error")
            return schedule_redirect()
    
        user = get_current_user()
        db = get_db()
    
        schedule_id = None
        try:
            schedule_id = db.create_schedule(
                data={
                    'name': name,
                    'description': description or None,
                    'timezone': timezone,
                    'business_hours': json.dumps(business_hours) if business_hours else None,
                    'default_closure_action': default_closure_action,
                    'default_closure_audio_id': default_closure_audio_id,
                    'default_closure_forward_to': default_closure_forward_to,
                },
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="create_schedule",
                target=name,
                details=f"Created schedule ID {schedule_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Created schedule '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to create schedule: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/update', methods=['POST'])
    @admin_required
    def update_schedule(schedule_id):
        """Update a schedule (name, description, timezone, business hours)."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        timezone = request.form.get('timezone', 'Australia/Sydney').strip()
    
        # Parse business hours from form
        business_hours = {}
        for day in ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']:
            open_time = request.form.get(f'{day}_open', '').strip()
            close_time = request.form.get(f'{day}_close', '').strip()
            if open_time and close_time:
                # Normalize to HH:MM (zero-pad hours for consistent string comparison)
                open_time = open_time.zfill(5)  # "9:00" -> "09:00"
                close_time = close_time.zfill(5)
                business_hours[day] = {'open': open_time, 'close': close_time}
    
        if not name:
            flash("Schedule name is required", "error")
            return redirect(url_for('web.admin'))
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.update_schedule(
                schedule_id=schedule_id,
                data={
                    'name': name,
                    'description': description or None,
                    'timezone': timezone,
                    'business_hours': json.dumps(business_hours) if business_hours else None,
                },
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_schedule",
                target=name,
                details=f"Updated schedule ID {schedule_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated schedule '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to update schedule: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/closure-defaults', methods=['POST'])
    @admin_required
    def update_closure_defaults(schedule_id):
        """Update closure defaults for a schedule."""
        default_closure_action = request.form.get('default_closure_action', '').strip() or None
        default_closure_audio_id = request.form.get('default_closure_audio_id', '').strip()
        default_closure_audio_id = int(default_closure_audio_id) if default_closure_audio_id else None
        default_closure_forward_to = request.form.get('default_closure_forward_to', '').strip() or None
    
        user = get_current_user()
        db = get_db()
    
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            flash("Schedule not found", "error")
            return schedule_redirect()
    
        try:
            db.update_schedule(
                schedule_id=schedule_id,
                data={
                    # Preserve existing values for fields we're not changing
                    'name': schedule['name'],
                    'description': schedule['description'],
                    'timezone': schedule['timezone'],
                    'business_hours': schedule['business_hours'],
                    # Update closure defaults
                    'default_closure_action': default_closure_action,
                    'default_closure_audio_id': default_closure_audio_id,
                    'default_closure_forward_to': default_closure_forward_to,
                },
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_closure_defaults",
                target=schedule['name'],
                details=f"Updated closure defaults: action={default_closure_action or 'none'}",
                performed_by=_audit_tag(user)
            )
            flash("Updated closure defaults", "success")
        except Exception as e:
            flash_error(f"Failed to update closure defaults: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/clone', methods=['POST'])
    @admin_required
    def clone_schedule(schedule_id):
        """Clone a schedule with its business hours and holidays."""
        new_name = request.form.get('name', '').strip()
    
        if not new_name:
            flash("New schedule name is required", "error")
            return schedule_redirect(schedule_id)
    
        user = get_current_user()
        db = get_db()
    
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            flash("Schedule not found", "error")
            return schedule_redirect()
    
        new_id = None
        try:
            new_id = db.clone_schedule(
                schedule_id=schedule_id,
                new_name=new_name,
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="clone_schedule",
                target=new_name,
                details=f"Cloned schedule '{schedule['name']}' (ID {schedule_id}) to new schedule ID {new_id}",
                performed_by=_audit_tag(user)
            )
            holiday_count = len(schedule.get('holidays', []))
            flash(f"Created '{new_name}' from '{schedule['name']}' with {holiday_count} holiday{'s' if holiday_count != 1 else ''}", "success")
        except Exception as e:
            flash_error(f"Failed to clone schedule: {e}", e)
    
        # Redirect to the NEW cloned schedule
        return schedule_redirect(new_id if new_id else schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/delete', methods=['POST'])
    @admin_required
    def delete_schedule(schedule_id):
        """Delete a schedule (only if not used by any call flows)."""
        user = get_current_user()
        db = get_db()
    
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            flash("Schedule not found", "error")
            return schedule_redirect()
    
        try:
            db.delete_schedule(schedule_id)
            db.log_activity(
                action="delete_schedule",
                target=schedule['name'],
                details=f"Deleted schedule ID {schedule_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Deleted schedule '{schedule['name']}'", "success")
        except ValueError as e:
            # Schedule is in use
            flash(str(e), "error")
            return schedule_redirect(schedule_id)
        except Exception as e:
            flash_error(f"Failed to delete schedule: {e}", e)
            return schedule_redirect(schedule_id)
    
        return schedule_redirect()
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/holiday/add', methods=['POST'])
    @admin_required
    def add_holiday(schedule_id):
        """Add a closure to a schedule (one-off or weekly recurring)."""
        name = request.form.get('name', '').strip()
        recurrence = request.form.get('recurrence', 'once')
        date = request.form.get('date', '').strip()
        day_of_week = request.form.get('day_of_week', '').strip()
        start_time = request.form.get('start_time', '').strip()
        end_time = request.form.get('end_time', '').strip()
        audio_id = request.form.get('audio_id')
        action = request.form.get('action', '').strip()
        forward_to = request.form.get('forward_to', '').strip()
    
        if not name:
            flash("Closure name is required", "error")
            return schedule_redirect(schedule_id)
    
        # Validate based on recurrence type
        if recurrence == 'once':
            if not date:
                flash("Date is required for one-off closures", "error")
                return schedule_redirect(schedule_id)
            if len(date) != 10:
                flash("Invalid date format. Use YYYY-MM-DD.", "error")
                return schedule_redirect(schedule_id)
            day_of_week_int = None
        else:  # weekly
            if not day_of_week:
                flash("Day of week is required for weekly closures", "error")
                return schedule_redirect(schedule_id)
            day_of_week_int = int(day_of_week)
            date = None  # Weekly doesn't use date
    
        user = get_current_user()
        db = get_db()
    
        try:
            holiday_id = db.add_schedule_holiday(
                schedule_id=schedule_id,
                name=name,
                date=date,
                is_recurring=False,
                created_by=_audit_tag(user),
                audio_id=int(audio_id) if audio_id else None,
                recurrence=recurrence,
                day_of_week=day_of_week_int,
                start_time=start_time.zfill(5) if start_time else None,
                end_time=end_time.zfill(5) if end_time else None,
                action=action or None,
                forward_to=forward_to or None
            )
            if recurrence == 'weekly':
                days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                detail = f"Weekly on {days[day_of_week_int]}"
                if start_time and end_time:
                    detail += f" {start_time}-{end_time}"
            else:
                detail = f"date={date}"
    
            db.log_activity(
                action="add_closure",
                target=name,
                details=f"Added to schedule {schedule_id} ({detail})",
                performed_by=_audit_tag(user)
            )
            flash(f"Added closure '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to add closure: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/holiday/<int:holiday_id>/edit', methods=['POST'])
    @admin_required
    def update_holiday(schedule_id, holiday_id):
        """Update a closure."""
        name = request.form.get('name', '').strip()
        recurrence = request.form.get('recurrence', 'once')
        date = request.form.get('date', '').strip()
        day_of_week = request.form.get('day_of_week', '').strip()
        start_time = request.form.get('start_time', '').strip()
        end_time = request.form.get('end_time', '').strip()
        audio_id = request.form.get('audio_id')
        action = request.form.get('action', '').strip()
        forward_to = request.form.get('forward_to', '').strip()
    
        if not name:
            flash("Closure name is required", "error")
            return schedule_redirect(schedule_id)
    
        # Validate based on recurrence type
        if recurrence == 'once':
            if not date:
                flash("Date is required for one-off closures", "error")
                return schedule_redirect(schedule_id)
            day_of_week_int = None
        else:  # weekly
            if not day_of_week:
                flash("Day of week is required for weekly closures", "error")
                return schedule_redirect(schedule_id)
            day_of_week_int = int(day_of_week)
            date = None
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.update_schedule_holiday(
                holiday_id=holiday_id,
                data={
                    'name': name,
                    'date': date,
                    'audio_id': int(audio_id) if audio_id else None,
                    'recurrence': recurrence,
                    'day_of_week': day_of_week_int,
                    'start_time': start_time or None,
                    'end_time': end_time or None,
                    'action': action or None,
                    'forward_to': forward_to or None,
                },
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_closure",
                target=name,
                details=f"Updated closure ID {holiday_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated closure '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to update closure: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    @bp.route('/admin/schedule/<int:schedule_id>/holiday/<int:holiday_id>/delete', methods=['POST'])
    @admin_required
    def remove_holiday(schedule_id, holiday_id):
        """Remove a holiday from a schedule."""
        user = get_current_user()
        db = get_db()
    
        try:
            db.remove_schedule_holiday(holiday_id)
            db.log_activity(
                action="remove_holiday",
                target=str(holiday_id),
                details="Removed holiday",
                performed_by=_audit_tag(user)
            )
            flash("Holiday removed", "success")
        except Exception as e:
            flash_error(f"Failed to remove holiday: {e}", e)
    
        return schedule_redirect(schedule_id)
    
    
    # =============================================================================
    # Holiday Template Management
    # =============================================================================
    
    # Helper for template/schedule redirects - keeps specific item expanded
    def template_redirect(template_id=None):
        """Generate redirect URL for template operations."""
        if template_id:
            return redirect(url_for('web.admin_templates') + f'#template_{template_id}')
        return redirect(url_for('web.admin_templates'))
    
    
    def schedule_redirect(schedule_id=None):
        """Generate redirect URL for schedule operations."""
        if schedule_id:
            return redirect(url_for('web.admin_schedules') + f'#schedule_{schedule_id}')
        return redirect(url_for('web.admin_schedules'))
    
    
    @bp.route('/admin/template/create', methods=['POST'])
    @admin_required
    def create_template():
        """Create a new holiday template."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        source_url = request.form.get('source_url', '').strip()
        data_as_at = request.form.get('data_as_at', '').strip()
    
        if not name:
            flash("Template name is required", "error")
            return template_redirect()
    
        user = get_current_user()
        db = get_db()
    
        template_id = None
        try:
            template_id = db.create_holiday_template(
                name=name,
                description=description or None,
                source_url=source_url or None,
                data_as_at=data_as_at or None,
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="create_template",
                target=name,
                details=f"Created holiday template ID {template_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Created holiday template '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to create template: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/update', methods=['POST'])
    @admin_required
    def update_template(template_id):
        """Update a holiday template."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        source_url = request.form.get('source_url', '').strip()
        data_as_at = request.form.get('data_as_at', '').strip()
    
        if not name:
            flash("Template name is required", "error")
            return template_redirect(template_id)
    
        user = get_current_user()
        db = get_db()
    
        template = db.get_holiday_template(template_id)
        if not template:
            flash("Template not found", "error")
            return template_redirect()
    
        try:
            db.update_holiday_template(
                template_id=template_id,
                name=name,
                description=description or None,
                source_url=source_url or None,
                data_as_at=data_as_at or None,
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_template",
                target=name,
                details=f"Updated holiday template ID {template_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated template '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to update template: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/clone', methods=['POST'])
    @admin_required
    def clone_template(template_id):
        """Clone a holiday template with all its items."""
        new_name = request.form.get('name', '').strip()
    
        if not new_name:
            flash("New template name is required", "error")
            return template_redirect(template_id)
    
        user = get_current_user()
        db = get_db()
    
        template = db.get_holiday_template(template_id)
        if not template:
            flash("Template not found", "error")
            return template_redirect()
    
        new_id = None
        try:
            new_id = db.clone_holiday_template(
                template_id=template_id,
                new_name=new_name,
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="clone_template",
                target=new_name,
                details=f"Cloned template '{template['name']}' (ID {template_id}) to new template ID {new_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Created '{new_name}' from '{template['name']}' with {len(template.get('items', []))} holidays", "success")
        except Exception as e:
            flash_error(f"Failed to clone template: {e}", e)
    
        # Redirect to the NEW cloned template
        return template_redirect(new_id if new_id else template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/delete', methods=['POST'])
    @admin_required
    def delete_template(template_id):
        """Delete a holiday template."""
        user = get_current_user()
        db = get_db()
    
        template = db.get_holiday_template(template_id)
        if not template:
            flash("Template not found", "error")
            return template_redirect()
    
        try:
            db.delete_holiday_template(template_id)
            db.log_activity(
                action="delete_template",
                target=template['name'],
                details=f"Deleted holiday template ID {template_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Deleted template '{template['name']}'", "success")
        except Exception as e:
            flash_error(f"Failed to delete template: {e}", e)
    
        # Template deleted, so redirect to section (not specific template)
        return template_redirect()
    
    
    @bp.route('/admin/template/<int:template_id>/item/add', methods=['POST'])
    @admin_required
    def add_template_item(template_id):
        """Add a holiday item to a template."""
        name = request.form.get('name', '').strip()
        date = request.form.get('date', '').strip()
    
        if not name or not date:
            flash("Holiday name and date are required", "error")
            return template_redirect(template_id)
    
        # Validate date format (YYYY-MM-DD)
        if len(date) != 10:
            flash("Invalid date format. Use YYYY-MM-DD.", "error")
            return template_redirect(template_id)
    
        user = get_current_user()
        db = get_db()
    
        try:
            item_id = db.add_template_item(
                template_id=template_id,
                name=name,
                date=date,
                is_recurring=False,  # Always use absolute dates now
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="add_template_item",
                target=name,
                details=f"Added to template {template_id} (date={date})",
                performed_by=_audit_tag(user)
            )
            flash(f"Added '{name}' to template", "success")
        except Exception as e:
            flash_error(f"Failed to add item: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/item/<int:item_id>/delete', methods=['POST'])
    @admin_required
    def remove_template_item(template_id, item_id):
        """Remove a holiday item from a template."""
        user = get_current_user()
        db = get_db()
    
        try:
            db.remove_template_item(item_id)
            db.log_activity(
                action="remove_template_item",
                target=str(item_id),
                details="Removed template item",
                performed_by=_audit_tag(user)
            )
            flash("Holiday removed from template", "success")
        except Exception as e:
            flash_error(f"Failed to remove item: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/item/<int:item_id>/edit', methods=['POST'])
    @admin_required
    def update_template_item(template_id, item_id):
        """Update a holiday item in a template."""
        name = request.form.get('name', '').strip()
        date = request.form.get('date', '').strip()
    
        if not name or not date:
            flash("Name and date are required", "error")
            return template_redirect(template_id)
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.update_template_item(item_id, name=name, date=date, updated_by=_audit_tag(user))
            db.log_activity(
                action="update_template_item",
                target=name,
                details=f"Updated to date={date}",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated '{name}'", "success")
        except Exception as e:
            flash_error(f"Failed to update item: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/link-schedule', methods=['POST'])
    @admin_required
    def link_schedule_to_template(template_id):
        """Link a schedule to a template."""
        schedule_id = request.form.get('schedule_id')
        if not schedule_id:
            flash("Schedule is required", "error")
            return template_redirect(template_id)
    
        user = get_current_user()
        db = get_db()
    
        try:
            schedule_id = int(schedule_id)
            created = db.link_template_to_schedule(
                template_id=template_id,
                schedule_id=schedule_id,
                created_by=_audit_tag(user)
            )
            if created:
                db.log_activity(
                    action="link_template_schedule",
                    target=str(template_id),
                    details=f"Linked template {template_id} to schedule {schedule_id}",
                    performed_by=_audit_tag(user)
                )
                flash("Schedule linked to template", "success")
            else:
                flash("Schedule already linked", "info")
        except Exception as e:
            flash_error(f"Failed to link schedule: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/unlink-schedule/<int:schedule_id>', methods=['POST'])
    @admin_required
    def unlink_schedule_from_template(template_id, schedule_id):
        """Remove a schedule link from a template."""
        user = get_current_user()
        db = get_db()
    
        try:
            removed = db.unlink_template_from_schedule(template_id, schedule_id)
            if removed:
                db.log_activity(
                    action="unlink_template_schedule",
                    target=str(template_id),
                    details=f"Unlinked template {template_id} from schedule {schedule_id}",
                    performed_by=_audit_tag(user)
                )
                flash("Schedule unlinked from template", "success")
            else:
                flash("Schedule was not linked", "info")
        except Exception as e:
            flash_error(f"Failed to unlink schedule: {e}", e)
    
        return template_redirect(template_id)
    
    
    @bp.route('/admin/template/<int:template_id>/sync-preview')
    @admin_required
    def template_sync_preview(template_id):
        """Preview what would happen when syncing a template to schedules."""
        db = get_db()
        preview = db.get_template_sync_preview(template_id)
    
        if 'error' in preview:
            flash(preview['error'], "error")
            return template_redirect(template_id)
    
        return render_template('sync_preview.html',
                               preview=preview,
                               current_user=get_current_user())
    
    
    @bp.route('/admin/template/<int:template_id>/apply', methods=['POST'])
    @admin_required
    def apply_template(template_id):
        """Apply a template to selected schedules."""
        schedule_ids = request.form.getlist('schedule_ids')
        if not schedule_ids:
            flash("No schedules selected", "error")
            return redirect(url_for('web.template_sync_preview', template_id=template_id))
    
        schedule_ids = [int(sid) for sid in schedule_ids]
    
        user = get_current_user()
        db = get_db()
    
        try:
            result = db.apply_template_to_schedules(
                template_id=template_id,
                schedule_ids=schedule_ids,
                created_by=_audit_tag(user)
            )
    
            if 'error' in result:
                flash(result['error'], "error")
            else:
                added_count = len(result.get('added', []))
                skipped_count = len(result.get('skipped', []))
                db.log_activity(
                    action="apply_template",
                    target=str(template_id),
                    details=f"Applied to schedules: {added_count} added, {skipped_count} skipped",
                    performed_by=_audit_tag(user)
                )
                flash(f"Applied template: {added_count} holidays added, {skipped_count} already existed", "success")
        except Exception as e:
            flash_error(f"Failed to apply template: {e}", e)
    
        return template_redirect(template_id)
    
    
