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
    or '#dt-dl-modal .dt-dl-options a[href], a[download]'
)

# ask = show buttons | best | all | 1080p / 720p / 480p
QUALITY = os.getenv("QUALITY", "ask").strip().lower()
TURNSTILE_TIMEOUT = int(os.getenv("TURNSTILE_TIMEOUT", "60"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "2"))
MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB bot upload limit

# a URL matching this is treated as an episode page; anything else as a series page
EPISODE_URL_PATTERN = os.getenv("EPISODE_URL_PATTERN", "").strip() or "/episodes?/"
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "30"))  # episode buttons per page

SITE_URL = os.getenv("SITE_URL", "").strip() or "https://hentaimama.io/"
BLOCK_ADS = os.getenv("BLOCK_ADS", "true").strip().lower() != "false"

# ---- concurrency
BROWSER_WORKERS = max(1, int(os.getenv("BROWSER_WORKERS", "2")))      # Chrome instances (~400-600 MB RAM each)
MAX_TRANSFERS = max(1, int(os.getenv("MAX_TRANSFERS", "3")))          # simultaneous download+upload jobs
MAX_JOBS_PER_USER = max(1, int(os.getenv("MAX_JOBS_PER_USER", "3")))  # active jobs one user may have
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))                        # seconds to reuse a scraped page/links

# ---- list pages (/hentai-series/, /genre/x/, ...)
SERIES_URL_PATTERN = os.getenv("SERIES_URL_PATTERN", "").strip() or r"/tvshows?/[^/?#]+"
LISTING_PER_SCREEN = max(2, min(12, int(os.getenv("LISTING_PER_SCREEN", "8"))))  # posters per Telegram screen
BROWSE_PATH = os.getenv("BROWSE_PATH", "").strip() or "hentai-series/"           # what /browse opens