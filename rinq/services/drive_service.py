"""
Google Drive Service for Rinq.

Handles file operations on Google Drive using the Drive API v3.
Used for storing call recordings with 12-month retention.
"""

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google_auth_httplib2 import AuthorizedHttp
import httplib2
from rinq.config import config
import os
import io
import logging

logger = logging.getLogger(__name__)

# Increased timeout for Google API calls
API_TIMEOUT = 120  # seconds


class GoogleDriveService:
    """Service for managing files on Google Drive via Drive API v3."""

    # Scopes for Drive access
    SCOPES = [
        'https://www.googleapis.com/auth/drive.file',  # Access files created by the app
    ]

    def __init__(self):
        self.credentials = None
        self.drive_service = None
        self._recordings_folder_id = None
        self._initialized = False

    def _ensure_initialized(self):
        """Lazy initialization - only connect to Drive when first needed."""
        if self._initialized:
            return

        try:
            logger.info("=== Rinq Google Drive Service Initialization ===")
            logger.info(f"Credentials file: {config.google_credentials_file}")
            logger.info(f"Admin email for delegation: {config.google_admin_email}")

            if not os.path.exists(config.google_credentials_file):
                logger.warning(f"Credentials file not found at {config.google_credentials_file}")
                self._initialized = True
                return

            # Create credentials with domain-wide delegation
            credentials = service_account.Credentials.from_service_account_file(
                config.google_credentials_file,
                scopes=self.SCOPES
            )
            logger.info(f"Service account email: {credentials.service_account_email}")

            # Delegate credentials to admin user
            if config.google_admin_email:
                self.credentials = credentials.with_subject(config.google_admin_email)
                logger.info(f"Delegated to admin: {config.google_admin_email}")
            else:
                self.credentials = credentials
                logger.warning("No admin email configured - using service account directly")

            # Create authorized HTTP client with longer timeout
            http = httplib2.Http(timeout=API_TIMEOUT)
            authorized_http = AuthorizedHttp(self.credentials, http=http)

            # Build the Drive service
            self.drive_service = build(
                'drive', 'v3',
                http=authorized_http
            )

            logger.info("Google Drive service initialized successfully")

        except Exception as e:
            logger.error(f"Error initializing Google Drive service: {e}")
            self.drive_service = None

        self._initialized = True

    def is_initialized(self) -> bool:
        """Check if the service is properly initialized."""
        self._ensure_initialized()
        return self.drive_service is not None

    # ─── Folder Operations ─────────────────────────────────────────────────────

    def get_or_create_folder(self, folder_name: str, parent_id: str = None) -> dict:
        """Get a folder by name, creating it if it doesn't exist."""
        self._ensure_initialized()
        if not self.drive_service:
            return {'error': 'Google Drive service not initialized'}

        try:
            # Search for existing folder
            query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            if parent_id:
                query += f" and '{parent_id}' in parents"

            results = self.drive_service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)'
            ).execute()

            files = results.get('files', [])
            if files:
                folder = files[0]
                logger.info(f"Found existing folder: {folder_name} (id={folder['id']})")
                return {'id': folder['id'], 'name': folder['name']}

            # Create new folder
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            if parent_id:
                file_metadata['parents'] = [parent_id]

            folder = self.drive_service.files().create(
                body=file_metadata,
                fields='id, name'
            ).execute()

            logger.info(f"Created folder: {folder_name} (id={folder['id']})")
            return {'id': folder['id'], 'name': folder['name']}

        except HttpError as e:
            logger.error(f"API error getting/creating folder: {e}")
            return {'error': f'API error: {e}'}
        except Exception as e:
            logger.error(f"Error getting/creating folder: {e}")
            return {'error': str(e)}

    def get_recordings_folder_id(self) -> str | None:
        """Get the configured recordings folder ID from settings.

        The folder must be created manually in Google Drive and configured
        in Rinq's admin settings. This ensures explicit control over where
        recordings are stored.

        Returns:
            The folder ID if configured, None otherwise.
        """
        if self._recordings_folder_id:
            return self._recordings_folder_id

        # Get configured folder ID from database settings
        from rinq.database.db import get_db
        folder_id = get_db().get_bot_setting('drive_recordings_folder_id')

        if not folder_id:
            logger.warning("Drive recordings folder not configured. "
                           "Create a folder in Google Drive and configure its ID in Rinq Admin > Settings.")
            return None

        self._recordings_folder_id = folder_id
        logger.info(f"Using configured recordings folder: {folder_id}")
        return self._recordings_folder_id

    # ─── File Operations ───────────────────────────────────────────────────────

    def upload_file(self, filename: str, content: bytes, mime_type: str,
                    folder_id: str = None, description: str = None) -> dict:
        """Upload a file to Google Drive."""
        self._ensure_initialized()
        if not self.drive_service:
            return {'error': 'Google Drive service not initialized'}

        try:
            file_metadata = {'name': filename}
            if folder_id:
                file_metadata['parents'] = [folder_id]
            if description:
                file_metadata['description'] = description

            # Create media upload from bytes
            media = MediaIoBaseUpload(
                io.BytesIO(content),
                mimetype=mime_type,
                resumable=True
            )

            file = self.drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name, webViewLink, size'
            ).execute()

            logger.info(f"Uploaded file: {filename} (id={file['id']}, size={file.get('size')})")
            return {
                'id': file['id'],
                'name': file['name'],
                'web_view_link': file.get('webViewLink'),
                'size': file.get('size'),
            }

        except HttpError as e:
            logger.error(f"API error uploading file: {e}")
            return {'error': f'API error: {e}'}
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            return {'error': str(e)}

    def download_file(self, file_id: str) -> dict:
        """Download a file from Google Drive."""
        self._ensure_initialized()
        if not self.drive_service:
            return {'error': 'Google Drive service not initialized'}

        try:
            # Get file metadata first
            file_metadata = self.drive_service.files().get(
                fileId=file_id,
                fields='name, mimeType, size'
            ).execute()

            # Download file content
            request = self.drive_service.files().get_media(fileId=file_id)
            file_buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(file_buffer, request)

            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug(f"Download progress: {int(status.progress() * 100)}%")

            content = file_buffer.getvalue()
            logger.info(f"Downloaded file: {file_metadata['name']} ({len(content)} bytes)")

            return {
                'content': content,
                'name': file_metadata['name'],
                'mime_type': file_metadata.get('mimeType'),
                'size': len(content),
            }

        except HttpError as e:
            if e.resp.status == 404:
                return {'error': 'File not found'}
            logger.error(f"API error downloading file: {e}")
            return {'error': f'API error: {e}'}
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return {'error': str(e)}

    def delete_file(self, file_id: str) -> dict:
        """Delete a file from Google Drive."""
        self._ensure_initialized()
        if not self.drive_service:
            return {'error': 'Google Drive service not initialized'}

        try:
            self.drive_service.files().delete(fileId=file_id).execute()
            logger.info(f"Deleted file: {file_id}")
            return {'success': True}

        except HttpError as e:
            if e.resp.status == 404:
                return {'error': 'File not found'}
            logger.error(f"API error deleting file: {e}")
            return {'error': f'API error: {e}'}
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            return {'error': str(e)}

    # ─── Recording-Specific Methods ────────────────────────────────────────────

    def upload_recording(self, recording_sid: str, content: bytes,
                         metadata: dict = None) -> dict:
        """Upload a call recording to the Rinq Recordings folder."""
        folder_id = self.get_recordings_folder_id()
        if not folder_id:
            return {'error': 'Could not get/create recordings folder'}

        filename = f"{recording_sid}.mp3"

        # Build description from metadata
        description = None
        if metadata:
            desc_parts = []
            if metadata.get('call_type'):
                desc_parts.append(f"Type: {metadata['call_type']}")
            if metadata.get('from_number'):
                desc_parts.append(f"From: {metadata['from_number']}")
            if metadata.get('to_number'):
                desc_parts.append(f"To: {metadata['to_number']}")
            if metadata.get('duration'):
                desc_parts.append(f"Duration: {metadata['duration']}s")
            if metadata.get('staff_name'):
                desc_parts.append(f"Staff: {metadata['staff_name']}")
            if metadata.get('call_sid'):
                desc_parts.append(f"Call SID: {metadata['call_sid']}")
            description = '\n'.join(desc_parts)

        return self.upload_file(
            filename=filename,
            content=content,
            mime_type='audio/mpeg',
            folder_id=folder_id,
            description=description
        )

    def download_recording(self, file_id: str) -> dict:
        """Download a recording from Drive."""
        return self.download_file(file_id)

    def list_old_recordings(self, days: int = 365) -> dict:
        """List recordings older than the specified number of days."""
        self._ensure_initialized()
        if not self.drive_service:
            return {'error': 'Google Drive service not initialized'}

        folder_id = self.get_recordings_folder_id()
        if not folder_id:
            return {'error': 'Could not get recordings folder'}

        try:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%S')

            query = f"'{folder_id}' in parents and trashed = false and createdTime < '{cutoff_str}'"

            results = self.drive_service.files().list(
                q=query,
                spaces='drive',
                pageSize=500,
                fields='files(id, name, createdTime, size)'
            ).execute()

            files = results.get('files', [])
            return {
                'files': [{
                    'id': f['id'],
                    'name': f['name'],
                    'created_time': f.get('createdTime'),
                    'size': f.get('size'),
                } for f in files],
                'count': len(files),
            }

        except HttpError as e:
            logger.error(f"API error listing old recordings: {e}")
            return {'error': f'API error: {e}'}
        except Exception as e:
            logger.error(f"Error listing old recordings: {e}")
            return {'error': str(e)}

    def purge_old_recordings(self, days: int = 365) -> dict:
        """Delete recordings older than the specified number of days."""
        old_recordings = self.list_old_recordings(days=days)
        if 'error' in old_recordings:
            return old_recordings

        files = old_recordings.get('files', [])
        logger.info(f"Found {len(files)} recordings older than {days} days to purge")

        deleted_count = 0
        errors = []

        for file in files:
            result = self.delete_file(file['id'])
            if 'error' in result:
                errors.append(f"{file['name']}: {result['error']}")
            else:
                deleted_count += 1

        logger.info(f"Purged {deleted_count} recordings from Drive, {len(errors)} errors")

        return {
            'success': len(errors) == 0,
            'deleted_count': deleted_count,
            'errors': errors if errors else None,
        }


# Singleton instance (lazy initialization)
drive_service = GoogleDriveService()
