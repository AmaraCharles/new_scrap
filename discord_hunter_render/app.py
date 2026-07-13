import os
import re
import json
import time
import random
import hashlib
import secrets
import sqlite3
import logging
import datetime
import threading
import urllib.request
import urllib.parse
import urllib.error
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, Response)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH  = os.path.join(DATA_DIR, "app.db")

# /data already exists on Render (mounted by the disk) — only create if missing
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except PermissionError:
    pass  # directory exists and is already mounted — that's fine

# Log DB path immediately so it's visible in Render logs
logging.basicConfig(level=logging.INFO)
_startup_logger = logging.getLogger(__name__)
_startup_logger.info(f"=== DB PATH: {DB_PATH} ===")
_startup_logger.info(f"=== DATA_DIR env: {os.environ.get('DATA_DIR', 'NOT SET — using ./data')} ===")

SESSION_TIMEOUT    = 60 * 60 * 8
SERVER_EXPIRY_DAYS = 30

DISCORD_PATTERN = re.compile(
    r'discord(?:app)?\.com/invite/([A-Za-z0-9\-_]{2,50})'
    r'|discord\.gg/([A-Za-z0-9\-_]{2,50})',
    re.IGNORECASE
)

TRADING_SUBREDDITS = [
    "Forex", "Daytrading", "stocks", "investing", "Cryptotrading",
    "algotrading", "options", "StockMarket", "pennystocks", "Wallstreetbets",
    "cryptocurrency", "Bitcoin", "Trading", "FuturesTrading", "scalping",
    "TradingView", "Etoro", "Robinhood", "thetagang", "Spreads",
]

TRADING_KEYWORDS = [
    "discord server trading", "discord forex signals", "discord crypto trading",
    "discord stock trading", "discord options trading", "discord futures trading",
    "discord day trading", "join our discord trading", "discord.gg trading signals",
    "trading discord invite", "free trading discord", "discord swing trading",
    "discord prop firm", "discord funded trader", "discord algo trading",
    "discord scalping signals", "discord options flow", "paid discord trading",
    "discord.gg forex", "discord.gg crypto signals",
]

# ── Database ──────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                salt            TEXT NOT NULL,
                hash            TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'user',
                scrape_credits  INTEGER NOT NULL DEFAULT 0,
                daily_credits   INTEGER NOT NULL DEFAULT 0,
                last_reset      TEXT NOT NULL DEFAULT '',
                created         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS server_history (
                username    TEXT NOT NULL,
                code        TEXT NOT NULL,
                first_seen  TEXT NOT NULL,
                PRIMARY KEY (username, code),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS scrape_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL,
                code        TEXT NOT NULL,
                url         TEXT NOT NULL,
                source      TEXT NOT NULL,
                context     TEXT NOT NULL DEFAULT '',
                found_at    TEXT NOT NULL,
                session_ts  TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            );
        """)
        # Migrate existing DBs that don't have new columns yet
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "daily_credits" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN daily_credits INTEGER NOT NULL DEFAULT 0")
        if "last_reset" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_reset TEXT NOT NULL DEFAULT ''")
    logger.info(f"Database ready at {DB_PATH}")

def bootstrap_admin():
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        salt, hashed = hash_password("admin123")
        conn.execute(
            "INSERT INTO users (username, salt, hash, role, scrape_credits, daily_credits, last_reset, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("admin", salt, hashed, "admin", 999, 0, "", datetime.datetime.now().isoformat())
        )
    print("\n⚠️  No users found — default admin created:")
    print("    Username: admin  |  Password: admin123")
    print("    ⚠️  Change this immediately via /admin/users\n")

# ── Auth ──────────────────────────────────────────────────────────
def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return salt, h.hex()

def verify_password(password, salt, hashed):
    _, h = hash_password(password, salt)
    return secrets.compare_digest(h, hashed)

def get_user(username):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user")
        if not uid or (time.time() - session.get("login_time", 0)) > SESSION_TIMEOUT:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated

# ── Credits ───────────────────────────────────────────────────────
def get_user_credits(username):
    user = get_user(username)
    if not user: return 0
    return 999 if user["role"] == "admin" else user["scrape_credits"]

def deduct_credit(username):
    """Atomically deduct 1 credit. Returns True if successful."""
    user = get_user(username)
    if not user: return False
    if user["role"] == "admin": return True
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE users SET scrape_credits = scrape_credits - 1 "
            "WHERE username=? AND scrape_credits > 0",
            (username,)
        )
        # rowcount=0 means the WHERE scrape_credits>0 guard blocked it — already 0
        return cur.rowcount > 0

def set_credits(username, amount, also_set_daily=False):
    """Set a user's current credits. Optionally also update their daily allowance."""
    amount = max(0, int(amount))
    with get_db() as conn:
        if also_set_daily:
            conn.execute(
                "UPDATE users SET scrape_credits=?, daily_credits=? WHERE username=?",
                (amount, amount, username)
            )
        else:
            conn.execute(
                "UPDATE users SET scrape_credits=? WHERE username=?",
                (amount, username)
            )
    return get_user(username)["scrape_credits"]

def add_credits(username, amount):
    """Add credits to a user's current balance."""
    amount = max(0, int(amount))
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET scrape_credits = scrape_credits + ? WHERE username=?",
            (amount, username)
        )
    return get_user(username)["scrape_credits"]

def set_daily_allowance(username, amount):
    """Set the daily reset allowance WITHOUT touching current balance."""
    amount = max(0, int(amount))
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET daily_credits=? WHERE username=?",
            (amount, username)
        )

# ── Midnight reset thread ─────────────────────────────────────────
def _midnight_reset_loop():
    """Background thread: at midnight, reset every non-admin user's
    scrape_credits back to their daily_credits allowance."""
    while True:
        now    = datetime.datetime.now()
        # Seconds until next midnight
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sleep_secs = (midnight - now).total_seconds()
        logger.info(f"[reset] Next credit reset in {int(sleep_secs//3600)}h "
                    f"{int((sleep_secs%3600)//60)}m")
        time.sleep(sleep_secs)
        # Reset
        today = datetime.date.today().isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET scrape_credits = daily_credits, last_reset = ? "
                "WHERE role != 'admin' AND daily_credits > 0",
                (today,)
            )
        logger.info(f"[reset] Daily credit reset complete for {today}")

def start_reset_thread():
    t = threading.Thread(target=_midnight_reset_loop, daemon=True)
    t.start()

# ── Server history ────────────────────────────────────────────────
def add_to_user_history(username, results):
    now  = datetime.datetime.now().isoformat()
    rows = [(username, r["code"], now) for r in results]
    with get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO server_history (username, code, first_seen) VALUES (?,?,?)", rows
        )

def is_fresh_for_user(code, username):
    with get_db() as conn:
        row = conn.execute(
            "SELECT first_seen FROM server_history WHERE username=? AND code=?",
            (username, code)
        ).fetchone()
    if not row: return True
    try:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(row["first_seen"])).days
        return age > SERVER_EXPIRY_DAYS
    except Exception:
        return True

def get_user_history_stats(username):
    with get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM server_history WHERE username=?", (username,)).fetchone()[0]
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=SERVER_EXPIRY_DAYS)).isoformat()
        active = conn.execute(
            "SELECT COUNT(*) FROM server_history WHERE username=? AND first_seen > ?",
            (username, cutoff)
        ).fetchone()[0]
    return {"total_seen": total, "active": active, "eligible": total - active}

# ── Persisted scrape results ─────────────────────────────────────
def save_results_to_db(username, results, session_ts):
    """Persist current scrape results to DB so they survive restarts."""
    if not results:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM scrape_results WHERE username=?", (username,))
        conn.executemany(
            "INSERT INTO scrape_results (username, code, url, source, context, found_at, session_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            [(username, r["code"], r["url"], r["source"],
              r.get("context",""), r["found_at"], session_ts) for r in results]
        )

def load_results_from_db(username):
    """Load last scrape results from DB."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, url, source, context, found_at FROM scrape_results "
            "WHERE username=? ORDER BY id ASC", (username,)
        ).fetchall()
    return [{"code": r["code"], "url": r["url"], "source": r["source"],
             "context": r["context"], "found_at": r["found_at"]} for r in rows]

def clear_results_from_db(username):
    with get_db() as conn:
        conn.execute("DELETE FROM scrape_results WHERE username=?", (username,))

# ── Per-user scrape state ─────────────────────────────────────────
# Each user gets their own state so multiple users can scrape simultaneously.
user_scrapes: dict = {}   # {username: {running, progress, results, ...}}
_scrapes_lock = threading.Lock()

def get_scrape(username):
    """Get or create scrape state for a user.
    On first access, restores persisted results from DB."""
    with _scrapes_lock:
        if username not in user_scrapes:
            persisted = load_results_from_db(username)
            user_scrapes[username] = {
                "running":    False,
                "progress":   [],
                "results":    persisted,
                "skipped":    0,
                "seen_codes": {r["code"] for r in persisted},
                "error":      None,
            }
        return user_scrapes[username]

# ── Helpers ───────────────────────────────────────────────────────
def extract_codes(text):
    codes = []
    for m in DISCORD_PATTERN.finditer(text or ""):
        code = m.group(1) or m.group(2)
        if code and 2 < len(code) < 50:
            if code.lower() not in ("nitro","app","channels","login","register","developers","download"):
                codes.append(code)
    return list(dict.fromkeys(codes))

def build_invite_url(code):
    return f"https://discord.gg/{code}"

# Thread-local storage so each scrape thread knows its own username
_tl = threading.local()

def log(msg, level="info"):
    username = getattr(_tl, "username", None)
    if username:
        st = get_scrape(username)
        st["progress"].append({"msg": msg, "level": level, "ts": time.time()})
    getattr(logger, level)(f"[{username}] {msg}" if username else msg)

def add_result(code, source, context=""):
    username = getattr(_tl, "username", None)
    if not username:
        return False
    st = get_scrape(username)
    if code in st["seen_codes"]:
        return False
    if not is_fresh_for_user(code, username):
        st["skipped"] += 1
        return False
    st["seen_codes"].add(code)
    st["results"].append({
        "code":     code,
        "url":      build_invite_url(code),
        "source":   source,
        "context":  context[:140],
        "found_at": datetime.datetime.now().strftime("%H:%M:%S"),
    })
    return True

# ── Proxy configuration ──────────────────────────────────────────
# Set WEBSHARE_USER and WEBSHARE_PASS as env vars on Render.
# Get free credentials at webshare.io (10 proxies, 1GB/mo free tier).
# Without these set, http_get falls back to direct (will get 403 on cloud).
PROXY_USER = os.environ.get("WEBSHARE_USER", "")
PROXY_PASS = os.environ.get("WEBSHARE_PASS", "")
PROXY_HOST = "p.webshare.io"
PROXY_PORT = "80"

# Rotate through different user-agents to reduce fingerprinting
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def _get_proxy_handler():
    """Build a urllib proxy handler from Webshare credentials, or None if not configured."""
    if not PROXY_USER or not PROXY_PASS:
        return None
    proxy_url = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return urllib.request.ProxyHandler({
        "http":  proxy_url,
        "https": proxy_url,
    })

def http_get(url, headers=None, timeout=20, retries=3, rate_wait=3):
    """
    General HTTP GET. Routes through Webshare residential proxy if
    WEBSHARE_USER / WEBSHARE_PASS are set, otherwise direct.
    rate_wait: seconds to sleep on 429 before retry.
    """
    base_headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        base_headers.update(headers)

    proxy_handler = _get_proxy_handler()
    if proxy_handler:
        opener = urllib.request.build_opener(proxy_handler)
    else:
        opener = urllib.request.build_opener()

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                # Handle gzip transparently
                try:
                    import gzip as _gz
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = _gz.decompress(raw)
                except Exception:
                    pass
                return raw.decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = rate_wait * (attempt + 1)
                log(f"  Rate limited by {url[:45]}… — waiting {wait}s", "warning")
                time.sleep(wait)
            elif e.code in (403, 404, 410):
                return None
            elif e.code == 503:
                time.sleep(rate_wait)
            else:
                time.sleep(2 * (attempt + 1))
        except Exception as exc:
            if attempt == retries - 1:
                log(f"  Request failed {url[:55]}…: {exc}", "warning")
            time.sleep(2)
    return None

# ── SCRAPERS ──────────────────────────────────────────────────────

# ── Reddit OAuth API ──────────────────────────────────────────────
# Reddit OAuth works from any cloud server — no proxy needed.
# Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET as env vars on Render.
# Create a free app at: https://www.reddit.com/prefs/apps
# (choose "script" type, any redirect URI e.g. http://localhost)
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_UA            = "python:discord-hunter:v1.0 (by /u/discordlinkbot)"

_reddit_token        = None
_reddit_token_expiry = 0
_reddit_token_lock   = threading.Lock()

def _get_reddit_token():
    """Fetch/cache a Reddit OAuth bearer token using client credentials."""
    global _reddit_token, _reddit_token_expiry
    with _reddit_token_lock:
        if _reddit_token and time.time() < _reddit_token_expiry - 60:
            return _reddit_token
        if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
            return None
        try:
            import base64
            creds    = base64.b64encode(f"{REDDIT_CLIENT_ID}:{REDDIT_CLIENT_SECRET}".encode()).decode()
            payload  = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            req      = urllib.request.Request(
                "https://www.reddit.com/api/v1/access_token",
                data=payload,
                headers={
                    "Authorization": f"Basic {creds}",
                    "User-Agent":    REDDIT_UA,
                    "Content-Type":  "application/x-www-form-urlencoded",
                }
            )
            # Token endpoint works direct from Render — no proxy
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            _reddit_token        = data["access_token"]
            _reddit_token_expiry = time.time() + data.get("expires_in", 3600)
            logger.info("Reddit OAuth token obtained")
            return _reddit_token
        except Exception as e:
            logger.warning(f"Reddit OAuth token failed: {e}")
            return None

def reddit_get(path, retries=4):
    """
    Reddit OAuth API calls go DIRECT (no proxy) — oauth.reddit.com
    accepts Render's datacenter IPs fine when authenticated.
    Falls back to proxy-routed public JSON API if no OAuth credentials.
    """
    token = _get_reddit_token()

    if token:
        # OAuth path — always direct, no proxy
        url     = f"https://oauth.reddit.com{path}" if path.startswith("/") else f"https://oauth.reddit.com/{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent":    REDDIT_UA,
            "Accept":        "application/json",
        }
        opener = urllib.request.build_opener()  # direct, no proxy
    else:
        # No OAuth — use public JSON API through proxy
        base = path if path.startswith("http") else f"https://www.reddit.com{path}"
        url  = base if ".json" in base else base + ".json"
        headers = {
            "User-Agent": REDDIT_UA,
            "Accept":     "application/json",
        }
        proxy_handler = _get_proxy_handler()
        opener = urllib.request.build_opener(proxy_handler) if proxy_handler else urllib.request.build_opener()

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 401 and token:
                # Token expired — refresh and retry
                global _reddit_token
                _reddit_token = None
                token = _get_reddit_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            elif e.code == 429:
                wait = min(60, 5 * (2 ** attempt))
                log(f"  Reddit rate limit — backing off {wait}s", "warning")
                time.sleep(wait)
            elif e.code in (403, 404):
                return None
            else:
                time.sleep(5 * (attempt + 1))
        except Exception as exc:
            if attempt == retries - 1:
                log(f"  Reddit request failed: {exc}", "warning")
            time.sleep(5)
    return None

def scrape_reddit_subreddit(subreddit, limit=100):
    found = 0
    for sort in ["new", "hot"]:
        html = reddit_get(f"/r/{subreddit}/{sort}.json?limit={limit}")
        if not html: continue
        try: data = json.loads(html)
        except Exception: continue
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            pd   = post.get("data", {})
            text = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("url","")
            for code in extract_codes(text):
                if add_result(code, f"Reddit r/{subreddit}", pd.get("title","")[:80]): found += 1
        for post in posts[:15]:
            pd        = post.get("data", {})
            permalink = pd.get("permalink", "")
            if not permalink: continue
            chtml = reddit_get(f"{permalink}.json?limit=50")
            if not chtml: continue
            try:
                for c in json.loads(chtml)[1]["data"]["children"]:
                    body = c.get("data", {}).get("body", "")
                    for code in extract_codes(body):
                        if add_result(code, f"Reddit r/{subreddit} comment", body[:80]): found += 1
            except Exception: pass
            time.sleep(1.2)
        time.sleep(random.uniform(4, 7))
    return found

def scrape_reddit_search(keywords):
    found = 0
    for kw in keywords:
        html = reddit_get(
            f"/search.json?q={urllib.parse.quote(kw)}&sort=new&limit=100&type=link,comment"
        )
        if not html: time.sleep(2); continue
        try: data = json.loads(html)
        except Exception: continue
        posts = data.get("data", {}).get("children", [])
        log(f"  Reddit keyword '{kw}': {len(posts)} posts")
        for post in posts:
            pd   = post.get("data", {})
            text = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("body","")+" "+pd.get("url","")
            for code in extract_codes(text):
                if add_result(code, f"Reddit search: {kw}", pd.get("title", pd.get("body",""))[:80]): found += 1
        time.sleep(random.uniform(4, 7))
    return found

def scrape_disboard(pages=3):
    found   = 0
    tags    = ["trading","forex","crypto-trading","stocks","investing",
               "day-trading","options-trading","futures","signals","prop-firm"]
    inv_re  = re.compile(
        r'href=["\']https?://discord(?:app)?\.com/invite/([A-Za-z0-9\-_]+)["\']'
        r'|href=["\']https?://discord\.gg/([A-Za-z0-9\-_]+)["\']',
        re.IGNORECASE
    )
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(f"https://disboard.org/servers/tag/{tag}?page={page}&fl=en&sort=-member_count",
                            headers={"Referer": "https://disboard.org/"}, rate_wait=4)
            if not html: continue
            for m in inv_re.finditer(html):
                code = m.group(1) or m.group(2)
                if code and add_result(code, f"Disboard:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(2, 4))
    return found

def scrape_discord_me(pages=5):
    found = 0
    for tag in ["trading","crypto","forex","stocks","investing","signals"]:
        for page in range(1, pages + 1):
            html = http_get(f"https://discord.me/servers/{page}?keyword={tag}", rate_wait=2)
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Discord.me:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_twitter_nitter(keywords):
    instances = ["nitter.poast.org","nitter.privacydev.net","nitter.cz","nitter.nl"]
    found = 0
    for kw in keywords:
        encoded = urllib.parse.quote(f"{kw} discord.gg")
        for inst in instances:
            html = http_get(f"https://{inst}/search?q={encoded}&f=tweets", rate_wait=3)
            if html:
                for code in extract_codes(html):
                    if add_result(code, f"Twitter/X:{kw}", kw): found += 1
                break
            time.sleep(1)
        time.sleep(random.uniform(2, 4))
    return found

def scrape_whop(pages=5):
    found      = 0
    categories = ["trading","forex","crypto","stocks","investing","options","futures","signals","finance"]
    invite_re  = re.compile(r'discord\.gg/([A-Za-z0-9\-_]{2,50})', re.IGNORECASE)
    link_re    = re.compile(r'href=["\'][^"\']*discord(?:app)?\.com/invite/([A-Za-z0-9\-_]{2,50})["\']', re.IGNORECASE)
    for cat in categories:
        for page in range(1, pages + 1):
            html = http_get(f"https://whop.com/marketplace/?category={cat}&page={page}",
                            headers={"Referer": "https://whop.com/"}, rate_wait=5)
            if not html: continue
            codes = [m.group(1) for m in invite_re.finditer(html)] + [m.group(1) for m in link_re.finditer(html)]
            for code in list(dict.fromkeys(codes)):
                if add_result(code, f"Whop.com:{cat}", f"category {cat} p{page}"): found += 1
            prod_re = re.compile(r'href=[\"\']/([\w\-]+)[\"\']+[^>]*>(?:[^<]*<[^>]*>)*[^<]*(?:trading|forex|crypto|signal|invest)', re.IGNORECASE)
            for slug in [m.group(1) for m in prod_re.finditer(html)][:8]:
                phtml = http_get(f"https://whop.com/{slug}/", headers={"Referer": "https://whop.com/marketplace/"})
                if not phtml: continue
                for code in extract_codes(phtml):
                    if add_result(code, f"Whop.com:{slug}", "product page"): found += 1
                time.sleep(random.uniform(1, 2))
            time.sleep(random.uniform(2, 4))
        log(f"  Whop '{cat}': {found} total so far")
    return found

def scrape_patreon(keywords):
    found  = 0
    terms  = ["trading signals discord","forex discord","crypto signals discord",
               "stock trading discord","options trading discord","day trading discord",
               "funded trader discord","prop firm discord"]
    for term in terms:
        html = http_get(f"https://www.patreon.com/search?q={urllib.parse.quote(term)}",
                        headers={"Referer": "https://www.patreon.com/"}, rate_wait=5)
        if not html: time.sleep(2); continue
        for code in extract_codes(html):
            if add_result(code, f"Patreon:{term}", term): found += 1
        creator_re = re.compile(r'"url":"https://www\.patreon\.com/([a-zA-Z0-9_\-]+)"', re.IGNORECASE)
        for slug in list(dict.fromkeys(creator_re.findall(html)))[:10]:
            if slug in ("home","login","signup","explore","search","about"): continue
            chtml = http_get(f"https://www.patreon.com/{slug}",
                             headers={"Referer": "https://www.patreon.com/search"})
            if not chtml: continue
            for code in extract_codes(chtml):
                if add_result(code, f"Patreon:{slug}", f"creator page"): found += 1
            time.sleep(random.uniform(1.5, 3))
        time.sleep(random.uniform(2, 4))
    return found

def scrape_gumroad():
    found = 0
    terms = ["trading signals","forex course discord","crypto signals",
             "stock trading course","options trading","day trading signals"]
    for term in terms:
        html = http_get(f"https://gumroad.com/discover?query={urllib.parse.quote(term)}&sort=featured",
                        headers={"Referer": "https://gumroad.com/"}, rate_wait=4)
        if not html: time.sleep(2); continue
        for code in extract_codes(html):
            if add_result(code, f"Gumroad:{term}", term): found += 1
        prod_re = re.compile(r'href=["\']https://[a-z0-9\-]+\.gumroad\.com/l/([a-zA-Z0-9_\-]+)["\']', re.IGNORECASE)
        for slug in list(dict.fromkeys(prod_re.findall(html)))[:6]:
            phtml = http_get(f"https://gumroad.com/l/{slug}",
                             headers={"Referer": "https://gumroad.com/discover"})
            if not phtml: continue
            for code in extract_codes(phtml):
                if add_result(code, f"Gumroad:{slug}", "product page"): found += 1
            time.sleep(random.uniform(1, 2))
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_skool():
    found = 0
    for term in ["trading","forex","crypto","stocks","options","signals"]:
        html = http_get(f"https://www.skool.com/discover?q={urllib.parse.quote(term)}",
                        headers={"Referer": "https://www.skool.com/"}, rate_wait=4)
        if not html: time.sleep(2); continue
        for code in extract_codes(html):
            if add_result(code, f"Skool.com:{term}", term): found += 1
        slug_re = re.compile(r'href=[\"\']/([a-zA-Z0-9_\-]+)[\"\']+[^>]*class=[\"|\'][^\"\']*community', re.IGNORECASE)
        for slug in list(dict.fromkeys(m.group(1) for m in slug_re.finditer(html)))[:8]:
            if slug in ("discover","login","signup","about","pricing"): continue
            chtml = http_get(f"https://www.skool.com/{slug}",
                             headers={"Referer": "https://www.skool.com/discover"})
            if not chtml: continue
            for code in extract_codes(chtml):
                if add_result(code, f"Skool.com:{slug}", "community page"): found += 1
            time.sleep(random.uniform(1, 2))
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_stocktwits():
    found = 0
    for sym in ["FOREX","CRYPTO","STOCKS","OPTIONS","FUTURES","SPY","BTC.X","ETH.X"]:
        html = http_get(f"https://stocktwits.com/symbol/{sym}", rate_wait=3)
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"StockTwits:{sym}", "symbol stream"): found += 1
        time.sleep(random.uniform(2, 3))
    return found

def scrape_topgg(pages=5):
    """top.gg — the largest Discord server and bot listing site."""
    found = 0
    tags  = ["trading","forex","crypto","stocks","investing","finance","signals"]
    inv_re = re.compile(
        r'href=["\']https?://discord(?:app)?\.com/invite/([A-Za-z0-9\-_]+)["\']'
        r'|href=["\']https?://discord\.gg/([A-Za-z0-9\-_]+)["\']',
        re.IGNORECASE
    )
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://top.gg/servers/tag/{urllib.parse.quote(tag)}?page={page}",
                headers={"Referer": "https://top.gg/"}, rate_wait=4
            )
            if not html: continue
            for m in inv_re.finditer(html):
                code = m.group(1) or m.group(2)
                if code and add_result(code, f"Top.gg:{tag}", f"page {page}"): found += 1
            for code in extract_codes(html):
                if add_result(code, f"Top.gg:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(2, 3.5))
    return found

def scrape_disforge(pages=5):
    """Disforge.com — Discord server listing directory."""
    found  = 0
    cats   = ["trading","crypto","forex","stocks","finance","investing","signals"]
    for cat in cats:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://disforge.com/servers?type={cat}&page={page}",
                headers={"Referer": "https://disforge.com/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Disforge:{cat}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_discords_com(pages=5):
    """Discords.com — public server listing."""
    found = 0
    tags  = ["trading","crypto","forex","stocks","finance","investing","signals"]
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discords.com/servers?tag={urllib.parse.quote(tag)}&page={page}",
                headers={"Referer": "https://discords.com/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Discords.com:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_discord_boats(pages=4):
    """Discord.boats — server listing site."""
    found = 0
    tags  = ["trading","crypto","forex","stocks","finance","investing"]
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discord.boats/servers/tag/{urllib.parse.quote(tag)}?page={page}",
                headers={"Referer": "https://discord.boats/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Discord.boats:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_discordhome(pages=4):
    """DiscordHome.com — server listing."""
    found = 0
    cats  = ["trading","crypto","finance","investing"]
    for cat in cats:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discordhome.com/server?category={urllib.parse.quote(cat)}&page={page}",
                headers={"Referer": "https://discordhome.com/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"DiscordHome:{cat}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_discord_st(pages=4):
    """Discord.st — server listing site."""
    found = 0
    tags  = ["trading","crypto","forex","stocks","finance","signals"]
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discord.st/servers/{urllib.parse.quote(tag)}/{page}/",
                headers={"Referer": "https://discord.st/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Discord.st:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_discordservers_com(pages=4):
    """Discordservers.com — server listing."""
    found = 0
    tags  = ["trading","crypto","forex","stocks","finance","investing","signals"]
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discordservers.com/tag/{urllib.parse.quote(tag)}/{page}",
                headers={"Referer": "https://discordservers.com/"}, rate_wait=3
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"DiscordServers.com:{tag}", f"page {page}"): found += 1
            time.sleep(random.uniform(1.5, 3))
    return found

def scrape_bing(keywords):
    """
    Bing search for discord.gg links — Bing is more scraper-friendly than Google.
    Searches for trading keywords + discord.gg.
    """
    found = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/",
    }
    for kw in keywords:
        query   = urllib.parse.quote(f"{kw} discord.gg")
        for offset in [0, 10, 20]:
            html = http_get(
                f"https://www.bing.com/search?q={query}&first={offset}",
                headers=headers, rate_wait=8
            )
            if not html: continue
            for code in extract_codes(html):
                if add_result(code, f"Bing:{kw}", kw): found += 1
            time.sleep(random.uniform(3, 6))
    return found

def scrape_youtube_search(keywords):
    """
    YouTube search results page — trading channels often post Discord links
    in video descriptions and about pages. Scrapes the search results HTML.
    """
    found   = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    terms = [f"{kw} discord" for kw in keywords[:8]]
    for term in terms:
        encoded = urllib.parse.quote(term)
        html    = http_get(
            f"https://www.youtube.com/results?search_query={encoded}",
            headers=headers, rate_wait=5
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"YouTube:{term}", "search results"): found += 1
        # Also check channel about pages linked from results
        channel_re = re.compile(r'"channelId":"([A-Za-z0-9_\-]{20,30})"', re.IGNORECASE)
        channel_ids = list(dict.fromkeys(channel_re.findall(html)))[:6]
        for cid in channel_ids:
            chtml = http_get(
                f"https://www.youtube.com/channel/{cid}/about",
                headers=headers
            )
            if not chtml: continue
            for code in extract_codes(chtml):
                if add_result(code, f"YouTube channel:{cid}", "channel about page"): found += 1
            time.sleep(random.uniform(1, 2))
        time.sleep(random.uniform(3, 5))
    return found

def scrape_telegram_public(keywords):
    """
    Telegram public channel search via t.me/s/ (public message mirror).
    Trading Telegram channels frequently post Discord server links.
    """
    found   = 0
    channels = [
        "forexsignals", "cryptosignalz", "stocksignals", "tradingroom",
        "forexfactory", "cryptoalerts", "daytraderz", "optionsflow",
        "futurestrading", "algotrading", "propfirmnews", "fundedtraders",
    ]
    for ch in channels:
        html = http_get(
            f"https://t.me/s/{ch}",
            headers={"Referer": "https://t.me/"}
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Telegram:{ch}", "public channel"): found += 1
        time.sleep(random.uniform(2, 3.5))

    # Also search via Telegram search proxy
    for kw in keywords[:6]:
        encoded = urllib.parse.quote(kw)
        html = http_get(
            f"https://tgstat.com/search?q={encoded}+discord",
            headers={"Referer": "https://tgstat.com/"}, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Telegram search:{kw}", kw): found += 1
        time.sleep(random.uniform(2, 4))
    return found

def scrape_reddit_extra_keywords(keywords):
    """
    Additional Reddit searches specifically targeting discord invite phrases
    that get missed by the main keyword sweep.
    """
    found       = 0
    extra_terms = [
        "discord.gg trading", "join discord forex", "discord crypto community",
        "discord trading group link", "free signals discord link",
        "discord.gg invite stocks", "funded trader discord link",
        "prop firm discord server", "discord swing trade alerts",
        "discord options alerts free", "copy trading discord",
    ]
    for term in extra_terms:
        html = reddit_get(
            f"/search.json?q={urllib.parse.quote(term)}&sort=new&limit=100"
        )
        if not html: time.sleep(2); continue
        try: data = json.loads(html)
        except Exception: continue
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            pd   = post.get("data", {})
            text = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("body","")
            for code in extract_codes(text):
                if add_result(code, f"Reddit extra:{term}", pd.get("title", term)[:80]):
                    found += 1
        time.sleep(random.uniform(2, 4))
    return found

def scrape_google_custom(keywords):
    """
    Scrapes Google search results for discord.gg trading links.
    Uses varied user agents and query phrasing to reduce blocking.
    """
    found   = 0
    agents  = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    ]
    for i, kw in enumerate(keywords[:10]):
        query   = urllib.parse.quote(f'"{kw}" discord.gg')
        agent   = agents[i % len(agents)]
        html    = http_get(
            f"https://www.google.com/search?q={query}&num=30",
            headers={
                "User-Agent":      agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Referer":         "https://www.google.com/",
            }, rate_wait=15
        )
        if not html: time.sleep(5); continue
        for code in extract_codes(html):
            if add_result(code, f"Google:{kw}", kw): found += 1
        time.sleep(random.uniform(5, 10))  # Google is strict — longer delays
    return found

def scrape_find_discord(pages=5):
    """Find.discord.com — official Discord server discovery page."""
    found  = 0
    cats   = ["trading","crypto","finance","investing","stocks","forex"]
    inv_re = re.compile(
        r'href=["\']https?://discord(?:app)?\.com/invite/([A-Za-z0-9\-_]+)["\']'
        r'|href=["\']https?://discord\.gg/([A-Za-z0-9\-_]+)["\']',
        re.IGNORECASE
    )
    for cat in cats:
        for page in range(1, pages + 1):
            html = http_get(
                f"https://discord.com/servers?query={urllib.parse.quote(cat)}&page={page}",
                headers={"Referer": "https://discord.com/servers"}, rate_wait=3
            )
            if not html: continue
            for m in inv_re.finditer(html):
                code = m.group(1) or m.group(2)
                if code and add_result(code, f"Find.Discord:{cat}", f"page {page}"): found += 1
            for code in extract_codes(html):
                if add_result(code, f"Find.Discord:{cat}", f"page {page}"): found += 1
            time.sleep(random.uniform(2, 3.5))
    return found

def scrape_duckduckgo(keywords):
    """
    DuckDuckGo HTML search — much more scraper-friendly than Google.
    Uses the lite endpoint which returns plain HTML with no JS.
    """
    found   = 0
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://duckduckgo.com/",
    }
    for kw in keywords:
        query = urllib.parse.quote(f"{kw} discord.gg")
        html  = http_get(
            f"https://html.duckduckgo.com/html/?q={query}&kl=us-en",
            headers=headers, rate_wait=4
        )
        if not html: time.sleep(3); continue
        for code in extract_codes(html):
            if add_result(code, f"DuckDuckGo:{kw}", kw): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_4chan_biz():
    """
    4chan /biz/ (business & finance board) — active trading community,
    frequently shares Discord server links in threads.
    """
    found = 0
    # Catalog JSON gives all active threads
    html = http_get(
        "https://a.4cdn.org/biz/catalog.json",
        headers={"Accept": "application/json", "Referer": "https://boards.4channel.org/biz/"},
        rate_wait=3
    )
    if not html:
        return found
    try:
        pages_data = json.loads(html)
    except Exception:
        return found

    thread_ids = []
    for page in pages_data:
        for thread in page.get("threads", []):
            text = (thread.get("com","") + " " + thread.get("sub",""))
            # Only fetch threads mentioning trading/discord keywords
            if any(w in text.lower() for w in ["discord","forex","crypto","trading","signal","stock","btc","eth"]):
                thread_ids.append(thread["no"])

    log(f"  4chan /biz/: {len(thread_ids)} relevant threads found")
    for tid in thread_ids[:40]:  # cap to avoid too many requests
        thtml = http_get(
            f"https://a.4cdn.org/biz/thread/{tid}.json",
            headers={"Accept": "application/json", "Referer": "https://boards.4channel.org/biz/"},
            rate_wait=2
        )
        if not thtml: continue
        try:
            tdata = json.loads(thtml)
            for post in tdata.get("posts", []):
                text = post.get("com","") + " " + post.get("filename","")
                for code in extract_codes(text):
                    if add_result(code, "4chan /biz/", post.get("com","")[:80]): found += 1
        except Exception:
            pass
        time.sleep(random.uniform(0.8, 1.5))
    return found

def scrape_tradingview():
    """
    TradingView public chat and ideas — large trading community,
    users frequently post Discord links in idea descriptions and comments.
    """
    found   = 0
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/json",
        "Referer":         "https://www.tradingview.com/",
        "Origin":          "https://www.tradingview.com",
    }
    # Public ideas API
    terms = ["forex","crypto","btc","stocks","futures","options","signals"]
    for term in terms:
        html = http_get(
            f"https://www.tradingview.com/ideas/{urllib.parse.quote(term)}/",
            headers={"User-Agent": headers["User-Agent"], "Referer": "https://www.tradingview.com/"},
            rate_wait=4
        )
        if not html: time.sleep(2); continue
        for code in extract_codes(html):
            if add_result(code, f"TradingView:{term}", "public idea"): found += 1
        time.sleep(random.uniform(2, 4))

    # Public screener/chat page
    for sym in ["FOREXCOM:EURUSD","BINANCE:BTCUSDT","NASDAQ:QQQ"]:
        html = http_get(
            f"https://www.tradingview.com/symbols/{sym}/",
            headers={"User-Agent": headers["User-Agent"]},
            rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"TradingView:{sym}", "symbol page"): found += 1
        time.sleep(random.uniform(2, 3))
    return found

def scrape_hive():
    """
    Hive.blog — decentralised blogging platform popular with crypto/trading
    community. Many posts contain Discord invite links.
    """
    found   = 0
    tags    = ["forex","crypto","trading","stocks","investing","defi","bitcoin","signals"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://hive.blog/",
    }
    for tag in tags:
        html = http_get(f"https://hive.blog/trending/{tag}", headers=headers, rate_wait=3)
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Hive.blog:{tag}", "trending post"): found += 1

        # Also hit the API for raw post content
        api_html = http_get(
            f"https://api.hive.blog",
            headers={
                "Content-Type": "application/json",
                "User-Agent":   headers["User-Agent"],
            },
            rate_wait=3
        )
        # Use the condenser API via POST — build request manually
        import urllib.request as _ur
        try:
            payload = json.dumps({
                "jsonrpc": "2.0", "method": "condenser_api.get_discussions_by_trending",
                "params": [{"tag": tag, "limit": 20}], "id": 1
            }).encode()
            req = _ur.Request(
                "https://api.hive.blog",
                data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"}
            )
            with _ur.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            for post in data.get("result", []):
                body = post.get("body","") + " " + post.get("json_metadata","")
                for code in extract_codes(body):
                    if add_result(code, f"Hive.blog:{tag}", post.get("title","")[:80]): found += 1
        except Exception:
            pass
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_medium(keywords):
    """
    Medium.com — many trading writers post Discord links in articles.
    Scrapes tag pages and search results.
    """
    found   = 0
    tags    = ["forex","cryptocurrency","stock-market","trading","day-trading",
               "crypto-trading","investing","options-trading","algorithmic-trading"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://medium.com/",
    }
    for tag in tags:
        html = http_get(f"https://medium.com/tag/{tag}/", headers=headers, rate_wait=4)
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Medium:{tag}", "tag page"): found += 1
        time.sleep(random.uniform(2, 4))

    # Medium search
    for kw in keywords[:8]:
        encoded = urllib.parse.quote(kw)
        html = http_get(
            f"https://medium.com/search?q={encoded}+discord",
            headers=headers, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Medium search:{kw}", kw): found += 1
        time.sleep(random.uniform(2, 4))
    return found

def scrape_github(keywords):
    """
    GitHub — READMEs, issues, and wikis for trading bots and signal tools
    frequently contain Discord server invite links.
    Uses GitHub's public search without auth.
    """
    found   = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "text/html",
        "Referer":    "https://github.com/",
    }
    search_terms = [
        "trading discord.gg", "forex signals discord.gg",
        "crypto trading bot discord", "stock signals discord invite",
        "algo trading discord", "options flow discord.gg",
        "funded trader discord", "prop firm discord",
    ]
    for term in search_terms:
        encoded = urllib.parse.quote(term)
        # Search code
        html = http_get(
            f"https://github.com/search?q={encoded}&type=code",
            headers=headers, rate_wait=5
        )
        if not html: time.sleep(3); continue
        for code in extract_codes(html):
            if add_result(code, f"GitHub:{term}", "code search"): found += 1

        # Search repos
        html2 = http_get(
            f"https://github.com/search?q={encoded}&type=repositories",
            headers=headers, rate_wait=5
        )
        if html2:
            for code in extract_codes(html2):
                if add_result(code, f"GitHub:{term}", "repo search"): found += 1
        time.sleep(random.uniform(4, 7))
    return found

def scrape_pastebin(keywords):
    """
    Pastebin public pastes — traders frequently paste Discord invite lists,
    signal group compilations, and server directories on Pastebin.
    Searches via Google dork targeting pastebin.com.
    """
    found   = 0
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.bing.com/",
    }
    search_terms = [
        "trading discord servers list",
        "forex discord invites",
        "crypto discord links",
        "free trading signals discord",
        "stock trading discord servers",
    ]
    # Use Bing to find relevant Pastebin pastes (more reliable than scraping Pastebin directly)
    for term in search_terms:
        query = urllib.parse.quote(f"site:pastebin.com {term} discord.gg")
        html  = http_get(
            f"https://www.bing.com/search?q={query}&count=20",
            headers=headers, rate_wait=6
        )
        if not html: time.sleep(4); continue
        # Extract pastebin URLs from search results
        paste_re = re.compile(r'pastebin\.com/([A-Za-z0-9]{6,12})', re.IGNORECASE)
        paste_ids = list(dict.fromkeys(paste_re.findall(html)))[:10]
        for pid in paste_ids:
            # Use raw paste endpoint
            phtml = http_get(
                f"https://pastebin.com/raw/{pid}",
                headers={"User-Agent": headers["User-Agent"]},
                rate_wait=3
            )
            if not phtml: continue
            for code in extract_codes(phtml):
                if add_result(code, f"Pastebin:{term}", f"paste {pid}"): found += 1
            time.sleep(random.uniform(1.5, 3))
        time.sleep(random.uniform(4, 7))
    return found

# ── Orchestrator ──────────────────────────────────────────────────
def run_scrape(config, username):
    _tl.username = username
    st = get_scrape(username)
    st.update({"running": True, "results": [], "skipped": 0,
               "seen_codes": set(), "progress": [], "error": None})
    try:
        sources    = config.get("sources", ["reddit","disboard"])
        custom_kw  = config.get("keywords", [])
        custom_sub = config.get("subreddits", [])
        depth      = config.get("depth", "normal")
        subreddits = custom_sub or TRADING_SUBREDDITS
        keywords   = custom_kw  or TRADING_KEYWORDS
        pages      = {"quick": 1, "normal": 3, "deep": 7}.get(depth, 3)
        sub_limit  = {"quick": 50, "normal": 100, "deep": 200}.get(depth, 100)
        subs_cap   = 5 if depth == "quick" else len(subreddits)

        st = get_user_history_stats(username)
        log(f"👤 {username} — {st['total_seen']} total seen, {st['active']} blocked (<{SERVER_EXPIRY_DAYS}d)")

        # ── FAST SOURCES FIRST (results appear quickly) ───────────
        if "disboard"      in sources: log("🔍 Scraping Disboard.org…");            n = scrape_disboard(pages=pages);                                               log(f"  → {n} new")
        if "topgg"         in sources: log("🔍 Scraping Top.gg…");                  n = scrape_topgg(pages=pages);                                                  log(f"  → {n} new")
        if "discordme"     in sources: log("🔍 Scraping Discord.me…");              n = scrape_discord_me(pages=pages);                                             log(f"  → {n} new")
        if "disforge"      in sources: log("🔍 Scraping Disforge.com…");            n = scrape_disforge(pages=pages);                                               log(f"  → {n} new")
        if "find_discord"  in sources: log("🔍 Scraping Discord Discovery…");       n = scrape_find_discord(pages=pages);                                           log(f"  → {n} new")
        if "discordscom"   in sources: log("🔍 Scraping Discords.com…");            n = scrape_discords_com(pages=pages);                                           log(f"  → {n} new")
        if "discordboats"  in sources: log("🔍 Scraping Discord.boats…");           n = scrape_discord_boats(pages=pages);                                          log(f"  → {n} new")
        if "discordhome"   in sources: log("🔍 Scraping DiscordHome.com…");         n = scrape_discordhome(pages=pages);                                            log(f"  → {n} new")
        if "discordst"     in sources: log("🔍 Scraping Discord.st…");              n = scrape_discord_st(pages=pages);                                             log(f"  → {n} new")
        if "discordservers" in sources: log("🔍 Scraping DiscordServers.com…");     n = scrape_discordservers_com(pages=pages);                                     log(f"  → {n} new")
        if "whop"          in sources: log("🔍 Scraping Whop.com…");                n = scrape_whop(pages=pages);                                                   log(f"  → {n} new")
        if "patreon"       in sources: log("🔍 Scraping Patreon…");                 n = scrape_patreon(keywords);                                                   log(f"  → {n} new")
        if "gumroad"       in sources: log("🔍 Scraping Gumroad…");                 n = scrape_gumroad();                                                           log(f"  → {n} new")
        if "skool"         in sources: log("🔍 Scraping Skool.com…");               n = scrape_skool();                                                             log(f"  → {n} new")
        if "stocktwits"    in sources: log("🔍 Scraping StockTwits…");              n = scrape_stocktwits();                                                        log(f"  → {n} new")
        if "tradingview"   in sources: log("🔍 Scraping TradingView…");             n = scrape_tradingview();                                                       log(f"  → {n} new")
        if "fourchan"      in sources: log("🔍 Scraping 4chan /biz/…");             n = scrape_4chan_biz();                                                         log(f"  → {n} new")
        if "hive"          in sources: log("🔍 Scraping Hive.blog…");               n = scrape_hive();                                                              log(f"  → {n} new")

        # ── SEARCH ENGINES (moderate speed) ──────────────────────
        if "duckduckgo"    in sources: log("🔍 Scraping DuckDuckGo…");              n = scrape_duckduckgo(keywords[:5] if depth=="quick" else keywords[:15]);       log(f"  → {n} new")
        if "bing"          in sources: log("🔍 Scraping Bing…");                    n = scrape_bing(keywords[:5] if depth=="quick" else keywords[:12]);              log(f"  → {n} new")
        if "twitter"       in sources: log("🔍 Scraping Twitter/X via Nitter…");    n = scrape_twitter_nitter(keywords[:3] if depth=="quick" else keywords[:10]);   log(f"  → {n} new")
        if "youtube"       in sources: log("🔍 Scraping YouTube…");                 n = scrape_youtube_search(keywords[:4] if depth=="quick" else keywords[:8]);    log(f"  → {n} new")
        if "telegram"      in sources: log("🔍 Scraping Telegram…");                n = scrape_telegram_public(keywords);                                           log(f"  → {n} new")
        if "medium"        in sources: log("🔍 Scraping Medium…");                  n = scrape_medium(keywords);                                                    log(f"  → {n} new")
        if "github"        in sources: log("🔍 Scraping GitHub…");                  n = scrape_github(keywords);                                                    log(f"  → {n} new")
        if "pastebin"      in sources: log("🔍 Scraping Pastebin…");                n = scrape_pastebin(keywords);                                                  log(f"  → {n} new")
        if "google"        in sources: log("🔍 Scraping Google…");                  n = scrape_google_custom(keywords[:3] if depth=="quick" else keywords[:10]);    log(f"  → {n} new")

        # ── REDDIT LAST (slowest due to per-subreddit delays) ─────
        if "reddit" in sources:
            log("🔍 Scraping Reddit subreddits…")
            for i, sub in enumerate(subreddits[:subs_cap]):
                if not get_scrape(username)["running"]: break
                log(f"  [{i+1}/{subs_cap}] r/{sub}")
                n = scrape_reddit_subreddit(sub, limit=sub_limit)
                log(f"  → {n} new from r/{sub}")
                time.sleep(random.uniform(2, 4))
            log("🔍 Searching Reddit by keyword…")
            n = scrape_reddit_search(keywords[:4] if depth == "quick" else keywords)
            log(f"  → {n} new from Reddit search")
        if "reddit_extra"  in sources: log("🔍 Scraping Reddit extra keywords…");  n = scrape_reddit_extra_keywords(keywords);                                     log(f"  → {n} new")

        st    = get_scrape(username)
        total = len(st["results"])
        log(f"✅ Done! {total} new servers found, {st['skipped']} already-seen skipped.", "info")
        if st["results"]:
            add_to_user_history(username, st["results"])
            session_ts = datetime.datetime.now().isoformat()
            save_results_to_db(username, st["results"], session_ts)

    except Exception as e:
        st = get_scrape(username)
        st["error"] = str(e)
        log(f"❌ Fatal error: {e}", "error")
        logger.exception("Scrape error")
    finally:
        get_scrape(username)["running"] = False

# ── Auth routes ───────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if session.get("user"): return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def do_login():
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    user     = get_user(username)
    if not user or not verify_password(password, user["salt"], user["hash"]):
        time.sleep(0.5)
        return jsonify({"error": "Invalid username or password"}), 401
    session["user"]       = username
    session["role"]       = user["role"]
    session["login_time"] = time.time()
    return jsonify({"status": "ok", "role": user["role"]})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ── Admin routes ──────────────────────────────────────────────────
@app.route("/admin/users")
@login_required
@admin_required
def admin_users_page():
    return render_template("admin_users.html")

@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, role, scrape_credits, daily_credits, last_reset, created FROM users"
        ).fetchall()
    result = {}
    for r in rows:
        st = get_user_history_stats(r["username"])
        result[r["username"]] = {
            "role":           r["role"],
            "scrape_credits": r["scrape_credits"],
            "daily_credits":  r["daily_credits"],
            "last_reset":     r["last_reset"],
            "created":        r["created"],
            "seen_total":     st["total_seen"],
            "seen_active":    st["active"],
        }
    return jsonify(result)

@app.route("/api/admin/users", methods=["POST"])
@login_required
@admin_required
def add_user():
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    role     = data.get("role", "user")
    credits  = 999 if role == "admin" else int(data.get("credits", 0))
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(username) < 3 or not username.isalnum():
        return jsonify({"error": "Username must be 3+ alphanumeric chars"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if get_user(username):
        return jsonify({"error": "Username already exists"}), 409
    salt, hashed = hash_password(password)
    daily  = credits if role != "admin" else 0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, salt, hash, role, scrape_credits, daily_credits, last_reset, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (username, salt, hashed, role, credits, daily, "", datetime.datetime.now().isoformat())
        )
    return jsonify({"status": "created", "username": username})

@app.route("/api/admin/users/<username>", methods=["DELETE"])
@login_required
@admin_required
def delete_user(username):
    if username == session.get("user"):
        return jsonify({"error": "Cannot delete yourself"}), 400
    if not get_user(username):
        return jsonify({"error": "User not found"}), 404
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE username=?", (username,))
    return jsonify({"status": "deleted"})

@app.route("/api/admin/users/<username>/password", methods=["PUT"])
@login_required
@admin_required
def reset_password(username):
    data   = request.get_json(silent=True) or {}
    new_pw = data.get("password", "")
    if len(new_pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if not get_user(username):
        return jsonify({"error": "User not found"}), 404
    salt, hashed = hash_password(new_pw)
    with get_db() as conn:
        conn.execute("UPDATE users SET salt=?, hash=? WHERE username=?", (salt, hashed, username))
    return jsonify({"status": "updated"})

@app.route("/api/admin/users/<username>/clear-history", methods=["POST"])
@login_required
@admin_required
def clear_user_history(username):
    if not get_user(username):
        return jsonify({"error": "User not found"}), 404
    with get_db() as conn:
        conn.execute("DELETE FROM server_history WHERE username=?", (username,))
    return jsonify({"status": "cleared"})

@app.route("/api/admin/users/<username>/credits", methods=["PUT"])
@login_required
@admin_required
def manage_credits(username):
    data         = request.get_json(silent=True) or {}
    action       = data.get("action", "add")   # add | set | set_daily
    set_daily_fl = data.get("set_daily", False) # True = also update daily allowance
    try:   amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Amount must be an integer"}), 400
    if amount < 0:
        return jsonify({"error": "Amount must be 0 or greater"}), 400
    if not get_user(username):
        return jsonify({"error": "User not found"}), 404
    if action == "set":
        new_val = set_credits(username, amount, also_set_daily=set_daily_fl)
    elif action == "set_daily":
        set_daily_allowance(username, amount)
        new_val = get_user(username)["scrape_credits"]
    else:  # add
        new_val = add_credits(username, amount)
    user = get_user(username)
    return jsonify({
        "status":        "updated",
        "username":      username,
        "credits":       user["scrape_credits"],
        "daily_credits": user["daily_credits"],
    })

@app.route("/api/me/password", methods=["PUT"])
@login_required
def change_own_password():
    data     = request.get_json(silent=True) or {}
    current  = data.get("current", "")
    new_pw   = data.get("new_password", "")
    username = session["user"]
    user     = get_user(username)
    if not verify_password(current, user["salt"], user["hash"]):
        return jsonify({"error": "Current password incorrect"}), 401
    if len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    salt, hashed = hash_password(new_pw)
    with get_db() as conn:
        conn.execute("UPDATE users SET salt=?, hash=? WHERE username=?", (salt, hashed, username))
    return jsonify({"status": "updated"})

@app.route("/api/me/history")
@login_required
def my_history():
    return jsonify(get_user_history_stats(session["user"]))

# ── Main routes ───────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("user"), role=session.get("role"))

@app.route("/api/credits")
@login_required
def get_credits():
    username = session["user"]
    return jsonify({
        "credits":   get_user_credits(username),
        "unlimited": get_user(username)["role"] == "admin",
    })

@app.route("/api/start", methods=["POST"])
@login_required
def start_scrape():
    username = session["user"]
    st = get_scrape(username)
    if st["running"]:
        return jsonify({"error": "Already running"}), 400
    if get_user_credits(username) <= 0:
        return jsonify({"error": "no_credits", "message": "No scrape credits left. Contact admin to top up."}), 403
    if not deduct_credit(username):
        return jsonify({"error": "no_credits", "message": "No scrape credits left. Contact admin to top up."}), 403
    config = request.get_json(silent=True) or {}
    threading.Thread(target=run_scrape, args=(config, username), daemon=True).start()
    return jsonify({"status": "started", "credits_remaining": get_user_credits(username)})

@app.route("/api/stop", methods=["POST"])
@login_required
def stop_scrape():
    username = session["user"]
    get_scrape(username)["running"] = False
    return jsonify({"status": "stopped"})

@app.route("/api/status")
@login_required
def get_status():
    st = get_scrape(session["user"])
    return jsonify({
        "running":  st["running"],
        "count":    len(st["results"]),
        "skipped":  st["skipped"],
        "progress": st["progress"][-60:],
        "error":    st["error"],
    })

@app.route("/api/results")
@login_required
def get_results():
    return jsonify(get_scrape(session["user"])["results"])

@app.route("/api/export")
@login_required
def export_results():
    fmt     = request.args.get("fmt", "json")
    results = get_scrape(session["user"])["results"]
    if fmt == "csv":
        def esc(s): return '"' + str(s).replace('"','""') + '"'
        lines = ["code,url,source,context,found_at"]
        for r in results:
            lines.append(",".join([esc(r["code"]), esc(r["url"]), esc(r["source"]),
                                   esc(r["context"]), esc(r["found_at"])]))
        return Response("\n".join(lines), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=discord_links.csv"})
    return Response(json.dumps(results, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment;filename=discord_links.json"})

@app.route("/api/clear", methods=["POST"])
@login_required
def clear_results():
    username = session["user"]
    st = get_scrape(username)
    st.update({"results": [], "seen_codes": set(), "progress": [], "skipped": 0})
    clear_results_from_db(username)
    return jsonify({"status": "cleared"})

# ── Startup ───────────────────────────────────────────────────────
init_db()
bootstrap_admin()
start_reset_thread()

if __name__ == "__main__":
    print("\n🎯 Discord Link Hunter — Render Edition")
    print(f"   DB path: {DB_PATH}")
    print("👉  Open http://127.0.0.1:5000\n")
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)), host="0.0.0.0", threaded=True)
