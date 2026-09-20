import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # LiveKit
    LIVEKIT_URL: str        = os.getenv("LIVEKIT_URL", "")
    LIVEKIT_API_KEY: str    = os.getenv("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "")
    FRONTEND_URL: str       = os.getenv("FRONTEND_URL", "http://localhost:3000")

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

    # Admin passcode for the admin dashboard itself
    ADMIN_DASHBOARD_PASSWORD: str = os.getenv("ADMIN_DASHBOARD_PASSWORD", "")

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
            print("  Admin passcode system will not work without these")
            return False
        return True


settings = Settings()
