import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv()

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-20b:free")
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CALENDAR_BOOKING_LINK = os.environ["CALENDAR_BOOKING_LINK"]
SENDER_NAME = os.environ.get("SENDER_NAME", "the team")
MEETING_PURPOSE = os.environ.get(
    "MEETING_PURPOSE", "a quick intro call to see if there's a fit to work together"
)

APP_PASSWORD = os.environ["APP_PASSWORD"]
APP_SECRET_KEY = os.environ["APP_SECRET_KEY"]

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")  # only needed for the lead sourcing agent

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

# Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence (first tab)
SHEET_RANGE = "A:I"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
]
CREDENTIALS_FILE = str(BASE_DIR / "credentials.json")
TOKEN_FILE = str(BASE_DIR / "token.json")
