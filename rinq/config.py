"""
Configuration loader for Rinq.
"""

import os
import subprocess
from pathlib import Path
import yaml
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).resolve().parents[1]
_env_path = _project_root / '.env'
if _env_path.exists():
    load_dotenv(_env_path)


class Config:
    """Configuration for Rinq."""

    def __init__(self):
        self.base_dir = Path(__file__).parent
        self._load_config()

    def _load_config(self):
        """Load configuration from YAML file."""
        config_path = self.base_dir / "config.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        self.name = config["name"]
        self.product_name = config.get("product_name", config["name"])
        self.description = config["description"]
        self.version = config["version"]
        self.personality = config.get("personality", "")
        self.git_hash = self._read_git_hash()

        # Data paths
        data_dir = os.environ.get("RINQ_DATA_DIR", str(self.base_dir.parent / "data"))
        self.master_db_path = os.path.join(data_dir, "master.db")
        self.tenants_dir = os.path.join(data_dir, "tenants")

        # Server settings
        server = config.get("server", {})
        self.server_host = server.get("host", "0.0.0.0")
        self.server_port = int(os.environ.get("PORT", server.get("port", 5000)))

        # Auth settings
        self._auth_config = config.get("auth", {})

        # Twilio settings
        self.twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        self.twilio_api_key = os.environ.get("TWILIO_API_KEY")
        self.twilio_api_secret = os.environ.get("TWILIO_API_SECRET")
        self.twilio_twiml_app_sid = os.environ.get("TWILIO_TWIML_APP_SID")
        self._webhook_base_url = os.environ.get("RINQ_WEBHOOK_URL")
        self.twilio_default_caller_id = os.environ.get("TWILIO_DEFAULT_CALLER_ID")
        self.sip_credential_list_sid = os.environ.get("TWILIO_SIP_CREDENTIAL_LIST_SID")

        # TTS settings
        self.elevenlabs_api_key = os.environ.get("ELEVENLABS_API_KEY")
        self.cartesia_api_key = os.environ.get("CARTESIA_API_KEY")

        # Google Workspace credentials
        self.google_credentials_file = os.environ.get(
            "GOOGLE_CREDENTIALS_FILE",
            str(self.base_dir.parent / ".secrets" / "google" / "credentials.json")
        )
        self.google_admin_email = os.environ.get("GOOGLE_WORKSPACE_ADMIN_EMAIL", "")

        # Google OAuth (standalone auth)
        self.google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

        # Call recording settings
        recordings = config.get("recordings", {})
        self.recordings_group_email = recordings.get("group_email", "")
        self.recordings_default_enabled = recordings.get("default_enabled", True)

    def _read_git_hash(self) -> str | None:
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True,
                cwd=str(self.base_dir.parent)
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    @property
    def webhook_base_url(self):
        """Webhook base URL for Twilio callbacks.

        Priority: env var > tenant config > request host > None
        """
        if self._webhook_base_url:
            return self._webhook_base_url
        # Check tenant config
        try:
            from flask import g
            tenant = getattr(g, 'tenant', None)
            if tenant and tenant.get('webhook_base_url'):
                return tenant['webhook_base_url'].rstrip('/')
        except RuntimeError:
            pass
        # Fall back to request host
        try:
            from flask import request
            return request.host_url.rstrip('/')
        except RuntimeError:
            return None

    @property
    def auth(self):
        return {
            "mode": self._auth_config.get("mode", "standalone"),
            "allowed_domains": self.allowed_domains,
            "admin_emails": self.admin_emails,
        }

    @property
    def allowed_domains(self):
        # Standalone: configure via env var (comma-separated)
        domains = os.environ.get("RINQ_ALLOWED_DOMAINS", "")
        if domains:
            return [d.strip() for d in domains.split(",") if d.strip()]
        return []

    @property
    def admin_emails(self):
        emails = os.environ.get("RINQ_ADMIN_EMAILS", "")
        if emails:
            return [e.strip() for e in emails.split(",") if e.strip()]
        return []

    @property
    def twilio_configured(self):
        return bool(self.twilio_account_sid and self.twilio_auth_token)


config = Config()
