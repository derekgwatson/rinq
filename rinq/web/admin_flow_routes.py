"""Call flow admin routes — CRUD for call flows, voicemail destinations, phone sections.

Extracted from web/routes.py. Registered via register(web_bp).
"""

import logging
from flask import request, redirect, url_for, flash, jsonify, render_template
from rinq.services.auth import admin_required, get_current_user
from rinq.database.db import get_db
from rinq.config import config

logger = logging.getLogger(__name__)

def _audit_tag(user):
    return f"session:{user.email}"

def register(bp):
    """Register call flow admin routes on the given blueprint."""

    @bp.route('/admin/call-flow/create', methods=['POST'])
    @admin_required
    def create_call_flow():
        """Create a new call flow."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        open_queue_id = request.form.get('open_queue_id')
        schedule_id = request.form.get('schedule_id')
    
        if not name:
            flash("Call flow name is required", "error")
            return redirect(url_for('web.admin'))
    
        user = get_current_user()
        db = get_db()
    
        greeting_audio_id = request.form.get('greeting_audio_id')
        closed_audio_id = request.form.get('closed_audio_id')
        closed_action = request.form.get('closed_action', 'ai_receptionist')
        open_no_answer_action = request.form.get('open_no_answer_action', 'ai_receptionist')
        voicemail_destination_id = request.form.get('voicemail_destination_id', '').strip()
        closed_forward_number = request.form.get('closed_forward_number', '').strip()
        open_forward_number = request.form.get('open_forward_number', '').strip()
        closed_message_parts = request.form.get('closed_message_parts', '').strip() or None
    
        open_action = request.form.get('open_action', 'queue')
        extension_prompt_audio_id = request.form.get('extension_prompt_audio_id')
        extension_invalid_audio_id = request.form.get('extension_invalid_audio_id')
    
        try:
            flow_id = db.create_call_flow(
                data={
                    'name': name,
                    'description': description or None,
                    'greeting_audio_id': int(greeting_audio_id) if greeting_audio_id else None,
                    'open_action': open_action,
                    'open_queue_id': int(open_queue_id) if open_queue_id else None,
                    'open_forward_number': open_forward_number or None,
                    'open_no_answer_action': open_no_answer_action,
                    'schedule_id': int(schedule_id) if schedule_id else None,
                    'closed_action': closed_action,
                    'closed_audio_id': int(closed_audio_id) if closed_audio_id else None,
                    'closed_message_parts': closed_message_parts,
                    'voicemail_destination_id': int(voicemail_destination_id) if voicemail_destination_id else None,
                    'closed_forward_number': closed_forward_number or None,
                    'extension_prompt_audio_id': int(extension_prompt_audio_id) if extension_prompt_audio_id else None,
                    'extension_invalid_audio_id': int(extension_invalid_audio_id) if extension_invalid_audio_id else None,
                },
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="create_call_flow",
                target=name,
                details=f"Created call flow ID {flow_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Created call flow '{name}'", "success")
        except Exception as e:
            flash(f"Failed to create call flow: {e}", "error")
    
        return redirect(url_for('web.admin'))
    
    
    @bp.route('/admin/phone/<sid>/call-flow', methods=['POST'])
    @admin_required
    def assign_call_flow(sid):
        """Assign a call flow to a phone number."""
        call_flow_id = request.form.get('call_flow_id')
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.set_phone_number_call_flow(
                phone_sid=sid,
                call_flow_id=int(call_flow_id) if call_flow_id else None,
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="assign_call_flow",
                target=sid,
                details=f"Assigned call flow {call_flow_id}",
                performed_by=_audit_tag(user)
            )
            flash("Call flow assigned", "success")
        except Exception as e:
            flash(f"Failed to assign call flow: {e}", "error")
    
        return redirect(url_for('web.admin_phone_numbers'))
    
    
    @bp.route('/admin/call-flow/<int:flow_id>/update', methods=['POST'])
    @admin_required
    def update_call_flow(flow_id):
        """Update an existing call flow."""
        user = get_current_user()
        db = get_db()
    
        name = request.form.get('name', '').strip()
        if not name:
            flash("Call flow name is required", "error")
            return redirect(url_for('web.admin_call_flows'))
    
        description = request.form.get('description', '').strip()
        greeting_audio_id = request.form.get('greeting_audio_id')
        schedule_id = request.form.get('schedule_id')
        open_action = request.form.get('open_action', 'queue')
        open_queue_id = request.form.get('open_queue_id')
        closed_action = request.form.get('closed_action', 'ai_receptionist')
        open_no_answer_action = request.form.get('open_no_answer_action', 'ai_receptionist')
        closed_audio_id = request.form.get('closed_audio_id')
        closed_forward_number = request.form.get('closed_forward_number', '').strip()
        open_forward_number = request.form.get('open_forward_number', '').strip()
        voicemail_destination_id = request.form.get('voicemail_destination_id', '').strip()
        no_answer_audio_id = request.form.get('no_answer_audio_id')
        extension_prompt_audio_id = request.form.get('extension_prompt_audio_id')
        extension_invalid_audio_id = request.form.get('extension_invalid_audio_id')
        closed_message_parts = request.form.get('closed_message_parts', '').strip() or None
    
        try:
            db.update_call_flow(
                flow_id=flow_id,
                data={
                    'name': name,
                    'description': description or None,
                    'greeting_audio_id': int(greeting_audio_id) if greeting_audio_id else None,
                    'schedule_id': int(schedule_id) if schedule_id else None,
                    'open_action': open_action,
                    'open_queue_id': int(open_queue_id) if open_queue_id else None,
                    'open_forward_number': open_forward_number or None,
                    'open_no_answer_action': open_no_answer_action,
                    'no_answer_audio_id': int(no_answer_audio_id) if no_answer_audio_id else None,
                    'closed_action': closed_action,
                    'closed_audio_id': int(closed_audio_id) if closed_audio_id else None,
                    'closed_message_parts': closed_message_parts,
                    'closed_forward_number': closed_forward_number or None,
                    'voicemail_destination_id': int(voicemail_destination_id) if voicemail_destination_id else None,
                    'extension_prompt_audio_id': int(extension_prompt_audio_id) if extension_prompt_audio_id else None,
                    'extension_invalid_audio_id': int(extension_invalid_audio_id) if extension_invalid_audio_id else None,
                },
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_call_flow",
                target=name,
                details=f"Updated call flow ID {flow_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Call flow '{name}' updated", "success")
        except Exception as e:
            flash(f"Failed to update call flow: {e}", "error")
    
        return redirect(url_for('web.admin_call_flows'))
    
    
    @bp.route('/admin/call-flow/<int:flow_id>/clone', methods=['POST'])
    @admin_required
    def clone_call_flow(flow_id):
        """Clone a call flow."""
        user = get_current_user()
        db = get_db()
    
        source = db.get_call_flow(flow_id)
        if not source:
            flash("Call flow not found", "error")
            return redirect(url_for('web.admin_call_flows'))
    
        try:
            clone_data = {
                'name': f"{source['name']} (Copy)",
                'description': source.get('description'),
                'greeting_audio_id': source.get('greeting_audio_id'),
                'schedule_id': source.get('schedule_id'),
                'open_action': source.get('open_action', 'queue'),
                'open_queue_id': source.get('open_queue_id'),
                'open_forward_number': source.get('open_forward_number'),
                'open_audio_id': source.get('open_audio_id'),
                'open_no_answer_action': source.get('open_no_answer_action', 'ai_receptionist'),
                'closed_action': source.get('closed_action', 'message'),
                'closed_audio_id': source.get('closed_audio_id'),
                'closed_forward_number': source.get('closed_forward_number'),
                'voicemail_email': source.get('voicemail_email'),
                'voicemail_destination_id': source.get('voicemail_destination_id'),
                'extension_prompt_audio_id': source.get('extension_prompt_audio_id'),
                'extension_no_answer_action': source.get('extension_no_answer_action', 'voicemail'),
            }
            new_id = db.create_call_flow(data=clone_data, created_by=_audit_tag(user))
            db.log_activity(
                action="clone_call_flow",
                target=clone_data['name'],
                details=f"Cloned from '{source['name']}' (ID {flow_id}) to new ID {new_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Cloned call flow '{source['name']}' as '{clone_data['name']}'", "success")
        except Exception as e:
            flash(f"Failed to clone call flow: {e}", "error")
    
        return redirect(url_for('web.admin_call_flows'))
    
    
    @bp.route('/admin/call-flow/<int:flow_id>/delete', methods=['POST'])
    @admin_required
    def delete_call_flow(flow_id):
        """Delete a call flow."""
        user = get_current_user()
        db = get_db()
    
        # Get the flow name for logging
        flow = db.get_call_flow(flow_id)
        if not flow:
            flash("Call flow not found", "error")
            return redirect(url_for('web.admin_call_flows'))
    
        try:
            deleted = db.delete_call_flow(flow_id)
            if deleted:
                db.log_activity(
                    action="delete_call_flow",
                    target=flow['name'],
                    details=f"Deleted call flow ID {flow_id}",
                    performed_by=_audit_tag(user)
                )
                flash(f"Call flow '{flow['name']}' deleted", "success")
            else:
                flash(f"Cannot delete '{flow['name']}' - it is still assigned to phone numbers", "error")
        except Exception as e:
            flash(f"Failed to delete call flow: {e}", "error")
    
        return redirect(url_for('web.admin_call_flows'))
    
    
    # =============================================================================
    # Voicemail Destinations Management
    # =============================================================================

    @bp.route('/admin/voicemail-destinations')
    @admin_required
    def admin_voicemail_destinations():
        """Voicemail destinations management page."""
        from rinq.integrations import get_ticket_service
        user = get_current_user()
        db = get_db()

        voicemail_destinations = db.get_voicemail_destinations()

        ticket_service = get_ticket_service()
        zendesk_groups = []
        if ticket_service:
            try:
                zendesk_groups = ticket_service.get_groups()
            except Exception as e:
                logger.warning(f"Failed to load Zendesk groups: {e}")

        return render_template(
            'admin_voicemail_destinations.html',
            voicemail_destinations=voicemail_destinations,
            zendesk_groups=zendesk_groups,
            current_user=user,
        )

    @bp.route('/admin/voicemail-destination/create', methods=['POST'])
    @admin_required
    def create_voicemail_destination():
        """Create a new voicemail destination."""
        name = request.form.get('name', '').strip()
        routing_type = request.form.get('routing_type', 'zendesk').strip()
        email = request.form.get('email', '').strip() or None
        description = request.form.get('description', '').strip() or None
        zendesk_group_id_str = request.form.get('zendesk_group_id', '').strip()
    
        # Convert zendesk_group_id to int or None
        zendesk_group_id = None
        if zendesk_group_id_str:
            try:
                zendesk_group_id = int(zendesk_group_id_str)
            except ValueError:
                pass
    
        # Validate based on routing type
        if not name:
            flash("Name is required", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        if routing_type == 'zendesk' and not zendesk_group_id:
            flash("Zendesk group is required for Zendesk routing", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        if routing_type == 'email' and not email:
            flash("Email address is required for Email routing", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        user = get_current_user()
        db = get_db()

        try:
            db.create_voicemail_destination(
                data={
                    'name': name,
                    'routing_type': routing_type,
                    'email': email,
                    'description': description,
                    'zendesk_group_id': zendesk_group_id,
                },
                created_by=_audit_tag(user)
            )
    
            detail = f"group_id={zendesk_group_id}" if routing_type == 'zendesk' else f"email={email}"
            db.log_activity(
                action="create_voicemail_destination",
                target=name,
                details=f"Created {routing_type} voicemail destination: {detail}",
                performed_by=_audit_tag(user)
            )
            flash(f"Voicemail destination '{name}' created", "success")
        except Exception as e:
            if 'UNIQUE constraint' in str(e):
                flash(f"A destination with email '{email}' already exists", "error")
            else:
                flash(f"Failed to create voicemail destination: {e}", "error")

        return redirect(url_for('web.admin_voicemail_destinations'))
    
    
    @bp.route('/admin/voicemail-destination/<int:destination_id>/update', methods=['POST'])
    @admin_required
    def update_voicemail_destination(destination_id):
        """Update a voicemail destination."""
        name = request.form.get('name', '').strip()
        routing_type = request.form.get('routing_type', 'zendesk').strip()
        email = request.form.get('email', '').strip() or None
        description = request.form.get('description', '').strip() or None
        zendesk_group_id_str = request.form.get('zendesk_group_id', '').strip()
    
        # Convert zendesk_group_id to int or None
        zendesk_group_id = None
        if zendesk_group_id_str:
            try:
                zendesk_group_id = int(zendesk_group_id_str)
            except ValueError:
                pass
    
        # Validate based on routing type
        if not name:
            flash("Name is required", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        if routing_type == 'zendesk' and not zendesk_group_id:
            flash("Zendesk group is required for Zendesk routing", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        if routing_type == 'email' and not email:
            flash("Email address is required for Email routing", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        user = get_current_user()
        db = get_db()

        destination = db.get_voicemail_destination(destination_id)
        if not destination:
            flash("Voicemail destination not found", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))
    
        try:
            db.update_voicemail_destination(
                destination_id=destination_id,
                data={
                    'name': name,
                    'routing_type': routing_type,
                    'email': email,
                    'description': description,
                    'zendesk_group_id': zendesk_group_id,
                },
                updated_by=_audit_tag(user)
            )
    
            detail = f"group_id={zendesk_group_id}" if routing_type == 'zendesk' else f"email={email}"
            db.log_activity(
                action="update_voicemail_destination",
                target=name,
                details=f"Updated {routing_type} destination: {detail}",
                performed_by=_audit_tag(user)
            )
            flash(f"Voicemail destination '{name}' updated", "success")
        except Exception as e:
            flash(f"Failed to update voicemail destination: {e}", "error")

        return redirect(url_for('web.admin_voicemail_destinations'))
    
    
    @bp.route('/admin/voicemail-destination/<int:destination_id>/delete', methods=['POST'])
    @admin_required
    def delete_voicemail_destination(destination_id):
        """Delete a voicemail destination."""
        user = get_current_user()
        db = get_db()
    
        destination = db.get_voicemail_destination(destination_id)
        if not destination:
            flash("Voicemail destination not found", "error")
            return redirect(url_for('web.admin_voicemail_destinations'))

        try:
            deleted = db.delete_voicemail_destination(destination_id)
            if deleted:
                db.log_activity(
                    action="delete_voicemail_destination",
                    target=destination['name'],
                    details=f"Deleted voicemail destination ID {destination_id}",
                    performed_by=_audit_tag(user)
                )
                flash(f"Voicemail destination '{destination['name']}' deleted", "success")
            else:
                flash(f"Cannot delete '{destination['name']}' - it is still in use by call flows", "error")
        except Exception as e:
            flash(f"Failed to delete voicemail destination: {e}", "error")

        return redirect(url_for('web.admin_voicemail_destinations'))
    
    
    @bp.route('/admin/phone/<sid>/section', methods=['POST'])
    @admin_required
    def update_phone_section(sid):
        """Update the section for a phone number (for caller ID assignment)."""
        section = request.form.get('section', '').strip() or None
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.update_phone_number_section(
                sid=sid,
                section=section,
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_phone_section",
                target=sid,
                details=f"Set section to '{section}'" if section else "Cleared section",
                performed_by=_audit_tag(user)
            )
            if section:
                flash(f"Section set to {section}", "success")
            else:
                flash("Section cleared", "success")
        except Exception as e:
            flash(f"Failed to update section: {e}", "error")
    
        return redirect(url_for('web.admin_phone_numbers'))
    
