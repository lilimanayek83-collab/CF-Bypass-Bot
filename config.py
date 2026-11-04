import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

AUTH_USERS = {int(x) for x in os.getenv("AUTH_USERS", "").replace(" ", "").split(",") if x}
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
PROXY = os.getenv("PROXY") or None
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

SITE_USER = os.getenv("SITE_USER", "").strip()
SITE_PASS = os.getenv("SITE_PASS", "")
PROFILE_NAME = os.getenv("PROFILE_NAME", "site_acc")
LOGIN_ICON_SELECTOR = os.getenv("LOGIN_ICON_SELECTOR", "").strip()
LOGIN_SUBMIT_SELECTOR = os.getenv("LOGIN_SUBMIT_SELECTOR", "").strip()

DOWNLOAD_BTN_SELECTOR = os.getenv("DOWNLOAD_BTN_SELECTOR", "").strip()
DOWNLOAD_LINK_SELECTOR = (
    os.getenv("DOWNLOAD_LINK_SELECTOR", "").strip()
    or 'a[download], [role="dialog"] a[href], .modal a[href], [class*="modal"] a[href]'
)

TURNSTILE_TIMEOUT = int(os.getenv("TURNSTILE_TIMEOUT", "60"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "2"))
MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB bot upload limit
