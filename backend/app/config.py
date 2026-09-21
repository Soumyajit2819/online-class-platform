import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # LiveKit
    LIVEKIT_URL: str        = os.getenv("LIVEKIT_URL", "")
    LIVEKIT_API_KEY: str    = os.getenv("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "")

    # Frontend URL (single or comma-separated list for multiple origins)
    # e.g. "http://localhost:3000,https://your-app.vercel.app"
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

    # Supabase Storage (S3 for recordings)
    SUPABASE_S3_ENDPOINT:   str = os.getenv("SUPABASE_S3_ENDPOINT", "")
    SUPABASE_S3_ACCESS_KEY: str = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
    SUPABASE_S3_SECRET_KEY: str = os.getenv("SUPABASE_S3_SECRET_KEY", "")
    SUPABASE_S3_REGION:     str = os.getenv("SUPABASE_S3_REGION", "ap-northeast-2")
    SUPABASE_S3_BUCKET:     str = os.getenv("SUPABASE_S3_BUCKET", "class-recordings")

    # Supabase Database (for admin passcodes)
    SUPABASE_URL:              str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_ANON_KEY:         str = os.getenv("SUPABASE_ANON_KEY", "")

    # HMAC key used only for short-lived recording playback URLs.  This must be
    # independent of LiveKit and storage credentials so neither is ever sent to
    # a browser.
    RECORDING_PLAYBACK_SECRET: str = os.getenv("RECORDING_PLAYBACK_SECRET", "")

    # Admin dashboard password (set a strong value in production)
    ADMIN_DASHBOARD_PASSWORD: str = os.getenv("ADMIN_DASHBOARD_PASSWORD", "")

    # Environment flag
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def allowed_origins(self) -> list[str]:
        """Parse comma-separated FRONTEND_URL into a list."""
        return [o.strip() for o in self.FRONTEND_URL.split(",") if o.strip()]

    def validate(self) -> bool:
        required = [
            ("LIVEKIT_URL",        self.LIVEKIT_URL),
            ("LIVEKIT_API_KEY",    self.LIVEKIT_API_KEY),
            ("LIVEKIT_API_SECRET", self.LIVEKIT_API_SECRET),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        return True

    def validate_storage(self) -> bool:
        required = [
            ("SUPABASE_S3_ENDPOINT",   self.SUPABASE_S3_ENDPOINT),
            ("SUPABASE_S3_ACCESS_KEY", self.SUPABASE_S3_ACCESS_KEY),
            ("SUPABASE_S3_SECRET_KEY", self.SUPABASE_S3_SECRET_KEY),
            ("SUPABASE_S3_BUCKET",     self.SUPABASE_S3_BUCKET),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            print(f"⚠ Missing storage credentials: {', '.join(missing)}")
            return False
        return True

    def validate_supabase_db(self) -> bool:
        required = [
            ("SUPABASE_URL",              self.SUPABASE_URL),
            ("SUPABASE_SERVICE_ROLE_KEY", self.SUPABASE_SERVICE_ROLE_KEY),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            print(f"⚠ Missing Supabase DB credentials: {', '.join(missing)}")
            return False
        return True


settings = Settings()
