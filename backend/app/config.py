import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    LIVEKIT_URL: str = os.getenv("LIVEKIT_URL", "")
    LIVEKIT_API_KEY: str = os.getenv("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    
    # Supabase Storage (S3-compatible) for recordings
    SUPABASE_S3_ENDPOINT: str = os.getenv("SUPABASE_S3_ENDPOINT", "")
    SUPABASE_S3_ACCESS_KEY: str = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
    SUPABASE_S3_SECRET_KEY: str = os.getenv("SUPABASE_S3_SECRET_KEY", "")
    SUPABASE_S3_REGION: str = os.getenv("SUPABASE_S3_REGION", "us-east-1")
    SUPABASE_S3_BUCKET: str = os.getenv("SUPABASE_S3_BUCKET", "class-recordings")
    
    def validate(self) -> bool:
        """Validate that all required LiveKit credentials are present."""
        required = [
            ("LIVEKIT_URL", self.LIVEKIT_URL),
            ("LIVEKIT_API_KEY", self.LIVEKIT_API_KEY),
            ("LIVEKIT_API_SECRET", self.LIVEKIT_API_SECRET),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        return True
    
    def validate_storage(self) -> bool:
        """Validate Supabase Storage credentials for recording."""
        required = [
            ("SUPABASE_S3_ENDPOINT", self.SUPABASE_S3_ENDPOINT),
            ("SUPABASE_S3_ACCESS_KEY", self.SUPABASE_S3_ACCESS_KEY),
            ("SUPABASE_S3_SECRET_KEY", self.SUPABASE_S3_SECRET_KEY),
            ("SUPABASE_S3_BUCKET", self.SUPABASE_S3_BUCKET),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            print(f"⚠ Warning: Missing storage credentials: {', '.join(missing)}")
            print("  Recording feature will not work without storage configuration")
            return False
        return True


settings = Settings()
