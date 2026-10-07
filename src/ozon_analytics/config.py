import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "")
    web_database_url: str = os.getenv("WEB_DATABASE_URL", os.getenv("DATABASE_URL", ""))
    client_id: int = int(os.getenv("OZON_CLIENT_ID", "0"))
    api_key: str = os.getenv("OZON_API_KEY", "")
    admin_user: str = os.getenv("ADMIN_USER", "analytics")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    session_secret: str = os.getenv("SESSION_SECRET", "")
    secure_cookies: bool = os.getenv("SECURE_COOKIES", "true").lower() == "true"
    data_dir: Path = Path(os.getenv("DATA_DIR", "var"))
    interval: float = max(5, float(os.getenv("OZON_REQUEST_INTERVAL", "5")))
    daily_limit: int = min(250, int(os.getenv("OZON_DAILY_REQUEST_LIMIT", "200")))
    history_days: int = min(28, max(1, int(os.getenv("HISTORY_DAYS", "14"))))
    lead_days: int = int(os.getenv("REPLENISHMENT_LEAD_DAYS", "14"))
    safety_days: int = int(os.getenv("SAFETY_STOCK_DAYS", "7"))


settings = Settings()
