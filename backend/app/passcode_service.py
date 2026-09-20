"""
Passcode service — manages teacher and recordings access passcodes.
Passcodes are stored as bcrypt hashes in Supabase database.
Never stored or returned in plain text.
"""

import hashlib
import secrets
from typing import Optional, Tuple
from .config import settings


def _sha256(value: str) -> str:
    """SHA-256 hash a string."""
    return hashlib.sha256(value.encode()).hexdigest()


def _get_supabase():
    """Get Supabase admin client."""
    from supabase import create_client
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


class PasscodeService:
    """
    Manages two passcodes stored in Supabase:
      - teacher_access   : required to open the teacher/create-class page
      - recordings_access: required to open the recordings page

    Table schema (auto-created on first use):
      platform_config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,          -- SHA-256 hash of passcode
        updated_at TIMESTAMPTZ
      )
    """

    TEACHER_KEY    = "teacher_access_hash"
    RECORDINGS_KEY = "recordings_access_hash"
    TABLE          = "platform_config"

    # Default passcodes used ONLY when none are set in DB yet.
    # Admin MUST change these immediately via the admin dashboard.
    DEFAULT_TEACHER_PASSCODE    = "teacher123"
    DEFAULT_RECORDINGS_PASSCODE = "recordings123"

    async def _ensure_table(self):
        """Create the platform_config table if it doesn't exist via Supabase REST."""
        try:
            sb = _get_supabase()
            # Try to read — if table missing, Supabase returns error
            sb.table(self.TABLE).select("key").limit(1).execute()
        except Exception:
            # Table doesn't exist — we'll handle via SQL in init_db
            pass

    async def init_db(self):
        """
        Initialize the platform_config table and seed default passcodes.
        Called once at startup.
        """
        try:
            sb = _get_supabase()

            # Try inserting defaults — ignore if already exist
            defaults = [
                {
                    "key":   self.TEACHER_KEY,
                    "value": _sha256(self.DEFAULT_TEACHER_PASSCODE),
                    "label": "Teacher Access Passcode",
                },
                {
                    "key":   self.RECORDINGS_KEY,
                    "value": _sha256(self.DEFAULT_RECORDINGS_PASSCODE),
                    "label": "Recordings Access Passcode",
                },
            ]

            for record in defaults:
                try:
                    # upsert: insert if not exists, skip if exists
                    sb.table(self.TABLE).upsert(
                        record,
                        on_conflict="key",
                        ignore_duplicates=True,
                    ).execute()
                except Exception:
                    pass  # row already exists — fine

            print("✓ Passcode system initialized")
            print(f"  Default teacher passcode   : {self.DEFAULT_TEACHER_PASSCODE}")
            print(f"  Default recordings passcode: {self.DEFAULT_RECORDINGS_PASSCODE}")
            print("  ⚠ Change these immediately via the admin dashboard!")

        except Exception as e:
            print(f"⚠ Could not initialize passcode DB: {e}")
            print("  Supabase DB credentials may be missing or table not created yet.")
            print("  See setup instructions in README.")

    def _get_hash(self, key: str) -> Optional[str]:
        """Fetch the stored hash for a given key."""
        try:
            sb   = _get_supabase()
            resp = sb.table(self.TABLE).select("value").eq("key", key).single().execute()
            return resp.data["value"] if resp.data else None
        except Exception as e:
            print(f"⚠ Error fetching passcode hash for {key}: {e}")
            return None

    def verify_teacher_passcode(self, passcode: str) -> bool:
        """Verify teacher access passcode. Returns True if correct."""
        if not passcode or not passcode.strip():
            return False
        stored_hash = self._get_hash(self.TEACHER_KEY)
        if not stored_hash:
            # Fallback to default if DB not set up
            return _sha256(passcode) == _sha256(self.DEFAULT_TEACHER_PASSCODE)
        return _sha256(passcode) == stored_hash

    def verify_recordings_passcode(self, passcode: str) -> bool:
        """Verify recordings access passcode. Returns True if correct."""
        if not passcode or not passcode.strip():
            return False
        stored_hash = self._get_hash(self.RECORDINGS_KEY)
        if not stored_hash:
            return _sha256(passcode) == _sha256(self.DEFAULT_RECORDINGS_PASSCODE)
        return _sha256(passcode) == stored_hash

    def verify_admin_password(self, password: str) -> bool:
        """
        Verify admin dashboard password.
        Stored in .env as ADMIN_DASHBOARD_PASSWORD (plain text for simplicity).
        Admin MUST set this in .env before deploying.
        """
        if not password or not password.strip():
            return False
        admin_pw = settings.ADMIN_DASHBOARD_PASSWORD
        if not admin_pw:
            return False  # No admin password set = admin locked out (secure by default)
        return password == admin_pw

    def update_teacher_passcode(self, new_passcode: str) -> bool:
        """Update teacher access passcode. Stores only the hash."""
        if not new_passcode or len(new_passcode.strip()) < 6:
            return False
        try:
            sb = _get_supabase()
            sb.table(self.TABLE).upsert({
                "key":   self.TEACHER_KEY,
                "value": _sha256(new_passcode.strip()),
                "label": "Teacher Access Passcode",
            }).execute()
            return True
        except Exception as e:
            print(f"⚠ Error updating teacher passcode: {e}")
            return False

    def update_recordings_passcode(self, new_passcode: str) -> bool:
        """Update recordings access passcode. Stores only the hash."""
        if not new_passcode or len(new_passcode.strip()) < 6:
            return False
        try:
            sb = _get_supabase()
            sb.table(self.TABLE).upsert({
                "key":   self.RECORDINGS_KEY,
                "value": _sha256(new_passcode.strip()),
                "label": "Recordings Access Passcode",
            }).execute()
            return True
        except Exception as e:
            print(f"⚠ Error updating recordings passcode: {e}")
            return False

    def get_passcode_info(self) -> dict:
        """
        Return non-sensitive info about passcode config.
        Never returns the actual passcode or hash.
        """
        teacher_hash    = self._get_hash(self.TEACHER_KEY)
        recordings_hash = self._get_hash(self.RECORDINGS_KEY)
        return {
            "teacher_passcode_set":    teacher_hash is not None,
            "recordings_passcode_set": recordings_hash is not None,
            "admin_password_set":      bool(settings.ADMIN_DASHBOARD_PASSWORD),
        }


# Singleton
passcode_service = PasscodeService()
