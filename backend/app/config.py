import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    LIVEKIT_URL: str = os.getenv("LIVEKIT_URL", "")
    LIVEKIT_API_KEY: str = os.getenv("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    
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


settings = Settings()
