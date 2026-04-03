"""
Master database for multi-tenant mode.

Stores tenants, users, and phone number -> tenant mappings.
Tenant-specific data lives in per-tenant SQLite databases.

Only used when RINQ_MULTI_TENANT=true.
"""

import os
import sqlite3
from pathlib import Path
try:
    from shared.migrations import MigrationRunner
except ImportError:
    from rinq.vendor.migrations import MigrationRunner


class MasterDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        migrations_dir = str(Path(__file__).parent / 'migrations' / 'master')
        runner = MigrationRunner(db_path=db_path, migrations_dir=migrations_dir)
        runner.run_pending_migrations(verbose=True)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # =========================================================================
    # Tenants
    # =========================================================================

    def get_tenants(self):
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT * FROM tenants WHERE is_active = 1 ORDER BY name").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_tenant(self, tenant_id: str):
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_tenant_by_domain(self, domain: str):
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE domain = ? AND is_active = 1", (domain,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_tenant(self, tenant_id: str, name: str, **kwargs):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO tenants (id, name, twilio_account_sid, twilio_auth_token,
                    twilio_api_key, twilio_api_secret, twilio_twiml_app_sid,
                    twilio_default_caller_id, twilio_sip_credential_list_sid,
                    webhook_base_url, integration_provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, name,
                  kwargs.get('twilio_account_sid'),
                  kwargs.get('twilio_auth_token'),
                  kwargs.get('twilio_api_key'),
                  kwargs.get('twilio_api_secret'),
                  kwargs.get('twilio_twiml_app_sid'),
                  kwargs.get('twilio_default_caller_id'),
                  kwargs.get('twilio_sip_credential_list_sid'),
                  kwargs.get('webhook_base_url'),
                  kwargs.get('integration_provider', 'none')))
            conn.commit()
        finally:
            conn.close()

    def update_tenant(self, tenant_id: str, **kwargs):
        conn = self._get_conn()
        try:
            sets = []
            vals = []
            for key, val in kwargs.items():
                sets.append(f"{key} = ?")
                vals.append(val)
            vals.append(tenant_id)
            conn.execute(f"UPDATE tenants SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()

    # =========================================================================
    # Users
    # =========================================================================

    def get_or_create_user(self, email: str, name: str = None, picture: str = None,
                           google_sub: str = None):
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                conn.execute("""
                    UPDATE users SET name = COALESCE(?, name),
                        picture = COALESCE(?, picture),
                        google_sub = COALESCE(?, google_sub)
                    WHERE email = ?
                """, (name, picture, google_sub, email))
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                return dict(row)
            else:
                conn.execute("""
                    INSERT INTO users (email, name, picture, google_sub)
                    VALUES (?, ?, ?, ?)
                """, (email, name, picture, google_sub))
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                return dict(row)
        finally:
            conn.close()

    def get_user_by_email(self, email: str):
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # =========================================================================
    # Tenant Users
    # =========================================================================

    def get_user_tenants(self, user_id: int):
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT t.*, tu.role FROM tenants t
                JOIN tenant_users tu ON t.id = tu.tenant_id
                WHERE tu.user_id = ? AND t.is_active = 1
                ORDER BY t.name
            """, (user_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def add_user_to_tenant(self, tenant_id: str, user_id: int, role: str = 'admin'):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO tenant_users (tenant_id, user_id, role)
                VALUES (?, ?, ?)
            """, (tenant_id, user_id, role))
            conn.commit()
        finally:
            conn.close()

    def get_user_role_in_tenant(self, user_id: int, tenant_id: str):
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT role FROM tenant_users
                WHERE user_id = ? AND tenant_id = ?
            """, (user_id, tenant_id)).fetchone()
            return row['role'] if row else None
        finally:
            conn.close()

    def get_tenant_users(self, tenant_id: str):
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT u.*, tu.role FROM users u
                JOIN tenant_users tu ON u.id = tu.user_id
                WHERE tu.tenant_id = ?
                ORDER BY u.email
            """, (tenant_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # =========================================================================
    # Phone Number -> Tenant mapping
    # =========================================================================

    def get_tenant_for_number(self, phone_number: str):
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT t.* FROM tenants t
                JOIN tenant_phone_numbers tpn ON t.id = tpn.tenant_id
                WHERE tpn.phone_number = ?
            """, (phone_number,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def register_phone_number(self, phone_number: str, tenant_id: str):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO tenant_phone_numbers (phone_number, tenant_id)
                VALUES (?, ?)
            """, (phone_number, tenant_id))
            conn.commit()
        finally:
            conn.close()


# Singleton
_master_db = None


def get_master_db() -> MasterDatabase:
    global _master_db
    if _master_db is None:
        from rinq.config import config
        _master_db = MasterDatabase(config.master_db_path)
    return _master_db
