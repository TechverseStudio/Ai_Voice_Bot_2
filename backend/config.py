"""Configuration and environment settings for Margadarshak AI Voice Automation Platform."""

import os
from dotenv import load_dotenv

load_dotenv()

# Simulation & Safety Controls
# Simulation is DEFAULT. Live calling requires explicit configuration switch.
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "true").lower() in ("true", "1", "yes")
LIVE_CALLS_ENABLED = os.getenv("LIVE_CALLS_ENABLED", "false").lower() in ("true", "1", "yes")

# Database Configuration with PostgreSQL / SQLite support & connection pooling
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/voice_agent.db")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))

# AI Voice Services (Sarvam AI for STT/TTS & Indic Voice)
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_FALLBACK_API_KEY = os.getenv("SARVAM_FALLBACK_API_KEY", "")

# In simulation mode, allow zero credentials without crashing on import
if not SARVAM_API_KEY and not SARVAM_FALLBACK_API_KEY:
    if not SIMULATION_MODE and LIVE_CALLS_ENABLED:
        raise ValueError("SARVAM_API_KEY is not set in the environment variables for live mode.")
    SARVAM_API_KEY = "mock_simulation_key"

# Telephony Provider Selection
# If live calls are disabled or simulation mode is on, force provider to mock unless explicit mock override
raw_provider = os.getenv("TELEPHONY_PROVIDER", "mock").lower()
if not LIVE_CALLS_ENABLED or SIMULATION_MODE:
    TELEPHONY_PROVIDER = "mock"
    TELEPHONY_MOCK_MODE = True
else:
    TELEPHONY_PROVIDER = raw_provider
    TELEPHONY_MOCK_MODE = os.getenv("TELEPHONY_MOCK_MODE", "false").lower() == "true"

# Carrier Configuration (Exotel / Twilio)
EXOTEL_BASE_URL = os.getenv("EXOTEL_BASE_URL", "https://api.exotel.com")
EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY", "")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN", "")
EXOTEL_CALLER_ID = os.getenv("EXOTEL_CALLER_ID", "")
EXOTEL_APPLET_ID = os.getenv("EXOTEL_APPLET_ID", "")

# Public Tunnel / Deployment URLs
AGENTSTREAM_PUBLIC_URL = os.getenv("AGENTSTREAM_PUBLIC_URL", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", AGENTSTREAM_PUBLIC_URL).rstrip("/")
CALLBACK_BASE_URL = os.getenv("CALLBACK_BASE_URL", f"{PUBLIC_BASE_URL}/api/telephony/webhook")
MEDIA_BASE_URL = os.getenv("MEDIA_BASE_URL", f"{PUBLIC_BASE_URL}/media")
