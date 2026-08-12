import os
from pathlib import Path
from pydantic_settings import BaseSettings

# Find root directory containing .env
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
ENV_FILE = ROOT_DIR / ".env"

class Settings(BaseSettings):
    PROJECT_NAME: str = "IncidentPilot API"
    VERSION: str = "1.0.0"
    
    # API Keys & DB URLs
    GROQ_API_KEY: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379"
    
    # Circuit Breakers
    MAX_INCIDENT_BUDGET_USD: float = 5.00
    MAX_AGENT_STEPS: int = 15
    MAX_FIX_ATTEMPTS: int = 3
    
    class Config:
        env_file = str(ENV_FILE) if ENV_FILE.exists() else ".env"
        extra = "ignore"

settings = Settings()
