import os
import re
import json
import time
import random
import hashlib
import secrets
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

# ── Turso database config ─────────────────────────────────────────
# Uses Turso's HTTP API — zero native dependencies, works on any host.
# Set these as env vars on Render:
#   TURSO_URL   = libsql://your-db-name.turso.io
#   TURSO_TOKEN = your-auth-token
TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Convert libsql:// URL to HTTPS for the REST API
def _turso_http_url():
    if not TURSO_URL:
        return ""
    return TURSO_URL.replace("libsql://", "https://")

TURSO_HTTP_URL = _turso_http_url()

logging.basicConfig(level=logging.INFO)
_startup_logger = logging.getLogger(__name__)
_startup_logger.info(f"=== Turso URL: {TURSO_URL or 'NOT SET'} ===")

SESSION_TIMEOUT    = 60 * 60 * 8
SERVER_EXPIRY_DAYS = 30

DISCORD_PATTERN = re.compile(
    r'discord(?:app)?\.com/invite/([A-Za-z0-9\-_]{2,50})'
    r'|discord\.gg/([A-Za-z0-9\-_]{2,50})',
    re.IGNORECASE
)

TRADING_SUBREDDITS = [
    # Core trading
    "Forex", "Daytrading", "stocks", "investing", "algotrading",
    "options", "StockMarket", "pennystocks", "wallstreetbets",
    "cryptocurrency", "Bitcoin", "Trading", "FuturesTrading", "scalping",
    "TradingView", "thetagang", "Spreads", "Cryptotrading",
    # Wealth / personal finance communities that share Discord links
    "personalfinance", "financialindependence", "fatFIRE", "ChubbyFIRE",
    "Bogleheads", "ValueInvesting", "SecurityAnalysis", "dividends",
    "RobinhoodPennyStocks", "smallstreetbets", "Superstonk",
    # Prop firm / funded trading
    "PropFirms", "FundedTrader", "FTMO",
    # Crypto niches
    "defi", "ethtrader", "SatoshiStreetBets", "CryptoMoonShots",
    "NFT", "altcoin", "CryptoCurrency",
    # International
    "IndiaInvestments", "UKInvesting", "EuropeFIRE", "AusFinance",
    "CanadianInvestor", "LatinAmerica",
]

# ── Keyword pools — rotated through on each scrape ───────────────
# Each list is a separate search category. The scraper picks one
# keyword per run from the active category to keep bank data clean.

TRADING_KEYWORDS = [
    # English action phrases
    "discord server trading", "discord forex signals",
    "discord crypto trading", "discord stock trading",
    "discord options trading", "discord futures trading",
    "discord day trading", "join discord trading",
    "discord prop firm", "discord funded trader",
    "discord algo trading", "discord scalping signals",
    "discord options flow", "discord swing trading",
    "discord.gg forex", "discord.gg crypto signals",
    "free trading discord server", "trading signals discord invite",
    "discord copy trading", "discord investment community",
    # Niche English — less competed
    "discord funded account signals", "discord smart money trading",
    "discord ICT trading", "discord order flow",
    "discord supply demand trading", "discord price action",
    "discord tape reading", "discord market maker",
    "discord dark pool signals", "discord unusual options activity",
    # German
    "Aktien Discord", "Börse Discord Server", "Aktienhandel Discord",
    "Daytrading Discord", "DAX Discord Community", "Trading Discord Deutschland",
    # French
    "Bourse Discord", "Trading Discord France", "CAC 40 Discord",
    "investissement Discord", "analyse technique Discord",
    # Spanish
    "Bolsa Discord", "Trading Discord España", "IBEX Discord",
    "inversión Discord", "señales trading Discord",
    # Italian
    "Borsa Discord Italia", "Trading Discord Italia",
    "azioni Discord", "investimenti Discord",
    # Portuguese / Brazil
    "Bolsa Discord Brasil", "B3 Discord", "trader Discord Brasil",
    "investimentos Discord", "sinais trading Discord",
    # Dutch
    "Beleggen Discord", "Aandelen Discord", "Trading Discord Nederland",
    # Polish
    "Giełda Discord", "Trading Discord Polska", "GPW Discord",
    # Russian
    "акции Discord", "трейдинг Discord", "инвестиции Discord сервер",
    "форекс Discord", "биржа Discord",
    # Ukrainian
    "акції Discord", "трейдинг Discord Україна",
    # Turkish
    "Borsa Discord", "Hisse Discord", "BIST Discord", "Trading Discord Türkiye",
    # Japanese
    "株式 Discord", "株 Discord サーバー", "投資 Discord",
    "デイトレード Discord", "FX Discord コミュニティ",
    # Korean
    "주식 Discord", "투자 Discord", "코스피 Discord", "단타 Discord",
    # Chinese (Simplified)
    "股票 Discord", "炒股 Discord", "投资 Discord群", "A股 Discord",
    "美股 Discord 社区",
    # Chinese (Traditional / Taiwan / HK)
    "股票 Discord 群", "台股 Discord", "港股 Discord", "投資 Discord",
    # Arabic
    "الأسهم Discord", "تداول Discord", "الاستثمار Discord",
    "سوق Discord", "فوركس Discord",
    # Hindi
    "शेयर बाजार Discord", "ट्रेडिंग Discord", "निवेश Discord",
    "NIFTY Discord", "NSE Discord",
    # Malay / Indonesian
    "Saham Discord", "Investasi Discord", "Bursa Discord",
    "Trading Discord Indonesia", "Trading Discord Malaysia",
    # Thai
    "หุ้น Discord", "ลงทุน Discord", "เทรด Discord",
    # Vietnamese
    "Chứng khoán Discord", "đầu tư Discord",
    # Greek
    "Χρηματιστήριο Discord", "Trading Discord Ελλάδα",
    # Swedish / Norwegian / Danish / Finnish
    "Aktier Discord", "Trading Discord Sverige",
    "Aksjer Discord", "Investering Discord",
    # Romanian / Czech / Hungarian
    "Acțiuni Discord", "Trading Discord România",
    "Akcie Discord", "Trading Discord Česko",
    "Részvény Discord", "Trading Discord Magyarország",
    # Appendage variants — combine with source names
    "discord trading community hub",
    "discord investors academy",
    "discord traders club signals",
    "discord market alerts group",
]

# ── Database — Turso HTTP API (no native deps) ───────────────────
import sqlite3 as _sqlite3

class _FakeCursor:
    """Mimics sqlite3 cursor so fetchone/fetchall work as expected."""
    def __init__(self, rows, cols, lastrowid=None, rowcount=0):
        self._rows       = rows or []
        self._cols       = cols or []
        self.lastrowid   = lastrowid
        self.rowcount    = len(rows) if rows else rowcount
        self.description = [(c,) for c in self._cols]
        self._idx        = 0

    def fetchone(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return dict(zip(self._cols, row)) if self._cols else None

    def fetchall(self):
        if not self._rows:
            return []
        return [dict(zip(self._cols, r)) for r in self._rows]

    def __iter__(self):
        return iter(self.fetchall())


class TursoConn:
    """
    Executes SQL against Turso's HTTP API.
    Falls back to local SQLite when TURSO_URL/TOKEN are not set (dev mode).
    Mimics sqlite3 connection interface so all existing code works unchanged.
    """
    def __init__(self):
        self._use_http  = bool(TURSO_HTTP_URL and TURSO_TOKEN)
        self._local     = None
        self._pending   = []   # batch statements for context manager
        if not self._use_http:
            os.makedirs("./data", exist_ok=True)
            self._local = _sqlite3.connect("./data/app.db", check_same_thread=False)
            self._local.row_factory = _sqlite3.Row

    # ── HTTP execution ────────────────────────────────────────────
    def _http_exec(self, statements):
        """
        POST to Turso HTTP API pipeline endpoint.
        Turso v2 pipeline format:
          Request:  {"requests": [{"type":"execute","stmt":{"sql":"...","args":[...]}}]}
          Response: {"results": [{"type":"ok","response":{"type":"execute","result":{...}}}]}
                 or {"results": [{"type":"error","error":{"message":"..."}}]}
        """
        payload = json.dumps({"requests": statements}).encode()
        req     = urllib.request.Request(
            f"{TURSO_HTTP_URL}/v2/pipeline",
            data    = payload,
            headers = {
                "Authorization": f"Bearer {TURSO_TOKEN}",
                "Content-Type":  "application/json",
            },
            method  = "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read())
                # Log full response in debug mode
                logger.debug(f"Turso response: {json.dumps(raw)[:500]}")
                return raw
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            logger.error(f"Turso HTTP {e.code}: {body[:300]}")
            raise RuntimeError(f"Turso HTTP {e.code}: {body}") from e

    def _make_stmt(self, sql, params=()):
        args = []
        for p in (params or []):
            if p is None:
                args.append({"type": "null"})
            elif isinstance(p, bool):
                args.append({"type": "integer", "value": "1" if p else "0"})
            elif isinstance(p, int):
                args.append({"type": "integer", "value": str(p)})
            elif isinstance(p, float):
                args.append({"type": "float", "value": str(p)})
            else:
                args.append({"type": "text", "value": str(p)})
        return {"type": "execute", "stmt": {"sql": sql, "args": args}}

    def _parse_result(self, result):
        """
        Parse one entry from the Turso pipeline results array.
        Turso v2 wraps each result as:
          {"type": "ok", "response": {"type": "execute", "result": {cols, rows, ...}}}
        or:
          {"type": "error", "error": {"message": "..."}}
        """
        # Handle error responses
        if result.get("type") == "error":
            msg = result.get("error", {}).get("message", "Unknown error")
            if any(x in msg.lower() for x in
                   ["already exists", "unique", "duplicate", "constraint"]):
                return _FakeCursor([], [], rowcount=0)
            logger.warning(f"Turso stmt error: {msg}")
            return _FakeCursor([], [], rowcount=0)

        # Unwrap the ok response — handle both flat and nested formats
        if result.get("type") == "ok":
            inner = result.get("response", {})
            # v2 format: response.result
            if "result" in inner:
                res_data = inner["result"]
            else:
                res_data = inner
        else:
            # Fallback: maybe result IS the data directly (older format)
            res_data = result

        rows     = res_data.get("rows", [])
        cols     = [c["name"] for c in res_data.get("cols", [])]
        lrid     = res_data.get("last_insert_rowid")
        affected = res_data.get("affected_row_count", 0)

        parsed_rows = []
        for row in rows:
            parsed_row = []
            for cell in row:
                if isinstance(cell, dict):
                    t = cell.get("type", "text")
                    v = cell.get("value")
                    if t == "null" or v is None:
                        parsed_row.append(None)
                    elif t == "integer":
                        parsed_row.append(int(v))
                    elif t == "float":
                        parsed_row.append(float(v))
                    else:
                        parsed_row.append(v)
                else:
                    # Already a plain value
                    parsed_row.append(cell)
            parsed_rows.append(parsed_row)

        return _FakeCursor(parsed_rows, cols, lastrowid=lrid, rowcount=affected)

    # ── Public interface ──────────────────────────────────────────
    def execute(self, sql, params=()):
        if not self._use_http:
            cur = self._local.execute(sql, params or ())
            # wrap in FakeCursor for uniform dict access
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchall()]
                return _FakeCursor(rows, cols, lastrowid=cur.lastrowid)
            return _FakeCursor([], [], lastrowid=cur.lastrowid)
        self._pending.append(self._make_stmt(sql, params))
        # Execute immediately (not batched) so callers get results back
        resp    = self._http_exec([self._make_stmt(sql, params)])
        results = resp.get("results", [])
        self._pending = []
        return self._parse_result(results[0]) if results else _FakeCursor([], [])

    def executemany(self, sql, params_list):
        if not self._use_http:
            self._local.executemany(sql, params_list)
            return
        params_list = list(params_list)
        if not params_list:
            return
        stmts = [self._make_stmt(sql, p) for p in params_list]
        try:
            resp    = self._http_exec(stmts)
            results = resp.get("results", [])
            for r in results:
                if r.get("type") == "error":
                    msg = r.get("error", {}).get("message", "")
                    # Ignore expected constraint errors
                    if not any(x in msg.lower() for x in
                               ["already exists", "unique", "duplicate"]):
                        logger.warning(f"Turso executemany error: {msg}")
        except Exception as e:
            logger.error(f"Turso executemany failed: {e}")

    def commit(self):
        if not self._use_http and self._local:
            self._local.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self._use_http and self._local:
            if exc_type is None:
                self._local.commit()
        return False


def get_db():
    return TursoConn()

def init_db():
    """Create tables. Each statement run separately — Turso doesn't support executescript."""
    stmts = [
        """CREATE TABLE IF NOT EXISTS users (
            username        TEXT PRIMARY KEY,
            salt            TEXT NOT NULL,
            hash            TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'user',
            scrape_credits  INTEGER NOT NULL DEFAULT 0,
            daily_credits   INTEGER NOT NULL DEFAULT 0,
            last_reset      TEXT NOT NULL DEFAULT '',
            created         TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS server_history (
            username    TEXT NOT NULL,
            code        TEXT NOT NULL,
            first_seen  TEXT NOT NULL,
            PRIMARY KEY (username, code)
        )""",
        """CREATE TABLE IF NOT EXISTS scrape_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            code        TEXT NOT NULL,
            url         TEXT NOT NULL,
            source      TEXT NOT NULL,
            context     TEXT NOT NULL DEFAULT '',
            found_at    TEXT NOT NULL,
            session_ts  TEXT NOT NULL
        )""",
        # Global server bank table
        """CREATE TABLE IF NOT EXISTS server_bank (
            code        TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT '',
            context     TEXT NOT NULL DEFAULT '',
            keywords    TEXT NOT NULL DEFAULT '[]',
            added_at    TEXT NOT NULL,
            hit_count   INTEGER NOT NULL DEFAULT 0
        )""",
        # Add columns if they don't exist yet (safe to run on existing DBs)
        "ALTER TABLE users ADD COLUMN daily_credits INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_reset TEXT NOT NULL DEFAULT ''",
    ]
    with get_db() as conn:
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # Column/table already exists
    logger.info(f"Database ready (Turso: {bool(TURSO_URL and TURSO_TOKEN)})")

def bootstrap_admin():
    with get_db() as conn:
        if conn.execute("SELECT 1 as x FROM users LIMIT 1").fetchone():
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
    if user.get("scrape_credits", 0) <= 0: return False
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE users SET scrape_credits = scrape_credits - 1 "
            "WHERE username=? AND scrape_credits > 0",
            (username,)
        )
        # Check rowcount — works for both Turso and local sqlite
        if hasattr(cur, "rowcount") and cur.rowcount == 0:
            return False
    # Verify by re-reading (definitive check for Turso)
    updated = get_user(username)
    return updated is not None

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
    saved = 0
    with get_db() as conn:
        for r in results:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO server_history (username, code, first_seen) VALUES (?,?,?)",
                    (username, r["code"], now)
                )
                saved += 1
            except Exception as e:
                logger.warning(f"History insert failed for {r['code']}: {e}")
    logger.info(f"Saved {saved}/{len(results)} servers to history for {username}")

def is_fresh_for_user(code, username):
    with get_db() as conn:
        row = conn.execute(
            "SELECT first_seen FROM server_history WHERE username=? AND code=?",
            (username, code)
        ).fetchone()
    if not row:
        return True
    try:
        fs  = row["first_seen"] if isinstance(row, dict) else row[0]
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(fs)).days
        return age > SERVER_EXPIRY_DAYS
    except Exception:
        return True

def get_user_history_stats(username):
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=SERVER_EXPIRY_DAYS)).isoformat()
    with get_db() as conn:
        r_total  = conn.execute("SELECT COUNT(*) as c FROM server_history WHERE username=?", (username,)).fetchone()
        r_active = conn.execute(
            "SELECT COUNT(*) as c FROM server_history WHERE username=? AND first_seen > ?",
            (username, cutoff)
        ).fetchone()
    total  = r_total["c"]  if r_total  else 0
    active = r_active["c"] if r_active else 0
    return {"total_seen": total, "active": active, "eligible": total - active}

# ── Persisted scrape results ─────────────────────────────────────
def save_results_to_db(username, results, session_ts):
    """Persist current scrape results to DB so they survive restarts."""
    if not results:
        return
    saved = 0
    with get_db() as conn:
        try:
            conn.execute("DELETE FROM scrape_results WHERE username=?", (username,))
        except Exception as e:
            logger.warning(f"Could not clear old results: {e}")
        for r in results:
            try:
                conn.execute(
                    "INSERT INTO scrape_results (username, code, url, source, context, found_at, session_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (username, r["code"], r["url"], r["source"],
                     r.get("context", ""), r["found_at"], session_ts)
                )
                saved += 1
            except Exception as e:
                logger.warning(f"Result insert failed for {r['code']}: {e}")
    logger.info(f"Saved {saved}/{len(results)} results to DB for {username}")

def load_results_from_db(username):
    """Load last scrape results from DB."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, url, source, context, found_at FROM scrape_results "
            "WHERE username=? ORDER BY id ASC", (username,)
        ).fetchall()
    if not rows:
        return []
    # rows are dicts (from row_factory)
    return [{"code": r["code"], "url": r["url"], "source": r["source"],
             "context": r["context"], "found_at": r["found_at"]} for r in rows]

def clear_results_from_db(username):
    with get_db() as conn:
        conn.execute("DELETE FROM scrape_results WHERE username=?", (username,))

# ── Keyword translation ───────────────────────────────────────────
# Translates non-English keywords to English before saving to bank
# so bank matching works across languages.
# Results cached in memory to avoid repeated API calls.
_translation_cache = {}

def translate_to_english(keyword):
    """
    Translate a keyword to English using Claude API.
    Returns the English version. If already English or API unavailable,
    returns the original unchanged.
    """
    if not keyword or not ANTHROPIC_API_KEY:
        return keyword

    kw = keyword.strip().lower()

    # Cache hit
    if kw in _translation_cache:
        return _translation_cache[kw]

    # Quick check — if it's ASCII-only and short, likely already English
    try:
        kw.encode('ascii')
        # ASCII — probably English, skip API call
        _translation_cache[kw] = keyword
        return keyword
    except UnicodeEncodeError:
        pass  # Non-ASCII — needs translation

    try:
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 50,
            "messages": [{
                "role": "user",
                "content": (
                    f"Translate this search keyword to English. "
                    f"Reply with ONLY the English translation, nothing else. "
                    f"If it is already English, reply with it unchanged. "
                    f"Keyword: {keyword}"
                )
            }]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        translated = data["content"][0]["text"].strip().lower()
        _translation_cache[kw] = translated
        logger.info(f"Translated keyword: '{keyword}' → '{translated}'")
        return translated
    except Exception as e:
        logger.warning(f"Translation failed for '{keyword}': {e}")
        _translation_cache[kw] = keyword  # Cache original on failure
        return keyword

# ── Global Server Bank ───────────────────────────────────────────
BANK_SERVE_THRESHOLD  = 20   # if bank has >= this many matches, skip scraping
BANK_SCRAPE_THRESHOLD = 10   # if bank has < this many matches, run scrape

def bank_search(keywords):
    """
    Search the bank for servers matching any of the given keywords.
    keywords: list of strings
    Returns list of result dicts, up to BANK_SERVE_THRESHOLD * 2.
    Each bank row's keywords field is a JSON array of strings.
    A server matches if ANY of its stored keywords overlaps with the query keywords.
    """
    if not keywords:
        return []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT code, url, source, context, keywords, added_at "
                "FROM server_bank ORDER BY hit_count DESC LIMIT 2000"
            ).fetchall()
        if not rows:
            return []

        kw_lower  = [k.lower() for k in keywords]
        matched   = []
        for r in rows:
            stored_kws = []
            try:
                stored_kws = json.loads(r["keywords"] if isinstance(r, dict) else r[4])
            except Exception:
                pass
            stored_lower = [k.lower() for k in stored_kws]
            # Only match if keywords are closely related (one contains the other)
            # Prevents "crypto" matching "hotel crypto conference" style pollution
            if any(q == s or q in s.split() or s in q.split()
                   or (len(q) > 4 and q in s) or (len(s) > 4 and s in q)
                   for q in kw_lower for s in stored_lower):
                matched.append({
                    "code":     r["code"]    if isinstance(r, dict) else r[0],
                    "url":      r["url"]     if isinstance(r, dict) else r[1],
                    "source":   r["source"] if isinstance(r, dict) else r[2],
                    "context":  r["context"] if isinstance(r, dict) else r[3],
                    "found_at": datetime.datetime.now().strftime("%H:%M:%S"),
                })
            if len(matched) >= BANK_SERVE_THRESHOLD * 3:
                break

        return matched
    except Exception as e:
        logger.warning(f"Bank search failed: {e}")
        return []

def bank_push(results, keywords):
    """
    Push new servers into the bank.
    For existing entries, merge in the new keywords.
    results:  list of result dicts (code, url, source, context)
    keywords: list of keyword strings used in the scrape that found them
    """
    if not results:
        return
    now = datetime.datetime.now().isoformat()
    pushed = 0
    try:
        for r in results:
            code = r["code"]
            with get_db() as conn:
                existing = conn.execute(
                    "SELECT keywords FROM server_bank WHERE code=?", (code,)
                ).fetchone()

            if existing:
                # Merge keywords
                try:
                    stored = json.loads(
                        existing["keywords"] if isinstance(existing, dict) else existing[0]
                    )
                except Exception:
                    stored = []
                merged = list(dict.fromkeys(stored + keywords))
                with get_db() as conn:
                    conn.execute(
                        "UPDATE server_bank SET keywords=?, hit_count=hit_count+1 WHERE code=?",
                        (json.dumps(merged), code)
                    )
            else:
                # New entry
                with get_db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO server_bank "
                        "(code, url, source, context, keywords, added_at, hit_count) "
                        "VALUES (?,?,?,?,?,?,0)",
                        (code, r["url"], r.get("source",""), r.get("context",""),
                         json.dumps(keywords), now)
                    )
                pushed += 1

        logger.info(f"Bank push: {pushed} new servers added, {len(results)-pushed} updated")
    except Exception as e:
        logger.warning(f"Bank push failed: {e}")

def bank_increment_hits(codes):
    """Increment hit_count for codes served from bank."""
    for code in codes:
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE server_bank SET hit_count=hit_count+1 WHERE code=?", (code,)
                )
        except Exception:
            pass

def get_bank_stats():
    """Return total server count in bank."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM server_bank").fetchone()
            return row["c"] if isinstance(row, dict) else row[0]
    except Exception:
        return 0

# ── Per-user scrape state ─────────────────────────────────────────
# Each user gets their own state so multiple users can scrape simultaneously.
user_scrapes: dict = {}   # {username: {running, progress, results, ...}}
_scrapes_lock = threading.Lock()

def get_scrape(username):
    """Get or create scrape state for a user.
    On first access, restores persisted results from DB."""
    with _scrapes_lock:
        if username not in user_scrapes:
            try:
                persisted = load_results_from_db(username)
            except Exception as e:
                logger.warning(f"Could not load persisted results for {username}: {e}")
                persisted = []
            user_scrapes[username] = {
                "running":    False,
                "finished":   False,
                "progress":   [],
                "results":    persisted,
                "skipped":    0,
                "seen_codes": {r["code"] for r in persisted},
                "error":      None,
                "bank_served": False,
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
    result = {
        "code":     code,
        "url":      build_invite_url(code),
        "source":   source,
        "context":  context[:140],
        "found_at": datetime.datetime.now().strftime("%H:%M:%S"),
    }
    st["results"].append(result)

    # Save to Turso immediately — don't wait for scrape to finish
    try:
        now = datetime.datetime.now().isoformat()
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_history (username, code, first_seen) VALUES (?,?,?)",
                (username, code, now)
            )
    except Exception as e:
        logger.warning(f"Real-time history save failed for {code}: {e}")

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
            pd        = post.get("data", {})
            text      = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("url","")
            created   = pd.get("created_utc", 0)
            post_date = datetime.datetime.utcfromtimestamp(created).strftime("%Y-%m-%d") if created else ""
            title     = pd.get("title","")[:65]
            context   = f"{title} [{post_date}]" if post_date else title
            for code in extract_codes(text):
                if add_result(code, f"Reddit r/{subreddit}", context): found += 1
        for post in posts[:15]:
            pd        = post.get("data", {})
            permalink = pd.get("permalink", "")
            if not permalink: continue
            chtml = reddit_get(f"{permalink}.json?limit=50")
            if not chtml: continue
            try:
                for c in json.loads(chtml)[1]["data"]["children"]:
                    cd        = c.get("data", {})
                    body      = cd.get("body", "")
                    created   = cd.get("created_utc", 0)
                    post_date = datetime.datetime.utcfromtimestamp(created).strftime("%Y-%m-%d") if created else ""
                    ctx       = f"{body[:60]} [{post_date}]" if post_date else body[:80]
                    for code in extract_codes(body):
                        if add_result(code, f"Reddit r/{subreddit} comment", ctx): found += 1
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


# ══════════════════════════════════════════════════════════════════
# TIER 2 — FORUMS & STRUCTURED SITES
# ══════════════════════════════════════════════════════════════════

def scrape_forexfactory():
    """ForexFactory.com — one of the largest forex forums on earth.
    Public threads are scrapeable. Searches the community forum."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.forexfactory.com/"}
    terms   = ["discord", "discord.gg", "discord server", "discord invite"]
    for term in terms:
        html = http_get(
            f"https://www.forexfactory.com/search?q={urllib.parse.quote(term)}&o=0",
            headers=headers, rate_wait=5
        )
        if not html: time.sleep(3); continue
        for code in extract_codes(html):
            if add_result(code, "ForexFactory", term): found += 1
        time.sleep(random.uniform(4, 7))
    # Also scrape the trading discussion and signal-services subforums
    for forum_path in ["forex-signals", "trading-systems", "trading-discussion"]:
        for page in range(1, 4):
            html = http_get(
                f"https://www.forexfactory.com/{forum_path}?page={page}",
                headers=headers, rate_wait=5
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"ForexFactory/{forum_path}", f"page {page}"): found += 1
            time.sleep(random.uniform(3, 6))
    return found

def scrape_elitetrader():
    """EliteTrader.com — professional traders forum with active Discord sharing."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.elitetrader.com/"}
    forums  = ["trading", "forex-trading", "cryptocurrencies",
               "prop-trading", "automated-trading", "options"]
    for forum in forums:
        for page in range(1, 4):
            html = http_get(
                f"https://www.elitetrader.com/et/forums/{forum}.{page}/",
                headers=headers, rate_wait=5
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"EliteTrader/{forum}", f"page {page}"): found += 1
            time.sleep(random.uniform(3, 6))
    return found

def scrape_babypips():
    """BabyPips.com — huge forex community forum, very active Discord sharing."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://community.babypips.com/"}
    cats    = ["trading-systems", "free-forex-trading-systems",
               "trading-discussion", "cryptocurrency"]
    for cat in cats:
        for page in range(1, 4):
            html = http_get(
                f"https://community.babypips.com/c/{cat}?page={page}",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"BabyPips/{cat}", f"page {page}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_bitcointalk():
    """Bitcointalk.org — oldest crypto forum. Every altcoin project links Discord.
    Hits the Altcoins, Tokens, and Speculation boards."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://bitcointalk.org/"}
    boards  = [
        ("67", "altcoins"),
        ("159", "altcoin-discussion"),
        ("238", "speculation"),
        ("53",  "announcements-altcoins"),
    ]
    for board_id, board_name in boards:
        for page in range(0, 3):
            html = http_get(
                f"https://bitcointalk.org/index.php?board={board_id}.{page*20}",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"Bitcointalk/{board_name}", f"page {page+1}"): found += 1
            time.sleep(random.uniform(3, 6))
    return found

def scrape_coinmarketcap():
    """CoinMarketCap — each coin's page has official Discord link in project info.
    Scrapes the top coins by market cap and trending."""
    found   = 0
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer":    "https://coinmarketcap.com/",
        "Accept":     "application/json, text/plain, */*",
    }
    # CMC has a public API endpoint for listings
    for start in [1, 101, 201, 301]:
        html = http_get(
            f"https://coinmarketcap.com/all/views/all/?start={start}",
            headers=headers, rate_wait=4
        )
        if not html: time.sleep(3); continue
        for code in extract_codes(html):
            if add_result(code, "CoinMarketCap", f"top coins {start}-{start+100}"): found += 1
        time.sleep(random.uniform(3, 5))
    # Also scrape trending
    html = http_get("https://coinmarketcap.com/trending-cryptocurrencies/",
                    headers=headers, rate_wait=4)
    if html:
        for code in extract_codes(html):
            if add_result(code, "CoinMarketCap/trending", "trending"): found += 1
    return found

def scrape_coingecko():
    """CoinGecko — coin pages list official Discord. Public API + web pages."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.coingecko.com/"}
    # Use CoinGecko's public coins list API
    html = http_get(
        "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1",
        headers={"Accept": "application/json", "User-Agent": random.choice(USER_AGENTS)},
        rate_wait=5
    )
    if html:
        try:
            coins = json.loads(html)
            coin_ids = [c["id"] for c in coins[:50]]  # Top 50 by market cap
            for coin_id in coin_ids:
                chtml = http_get(
                    f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=false&community_data=true",
                    headers={"Accept": "application/json", "User-Agent": random.choice(USER_AGENTS)},
                    rate_wait=3
                )
                if not chtml: continue
                for code in extract_codes(chtml):
                    if add_result(code, f"CoinGecko/{coin_id}", "coin page"): found += 1
                time.sleep(random.uniform(1.5, 3))
        except Exception as e:
            logger.warning(f"CoinGecko error: {e}")
    return found

def scrape_quora():
    """Quora — trading/investing Q&A pages have direct Discord links in answers."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.quora.com/"}
    queries = [
        "best-Discord-server-for-forex-trading",
        "best-Discord-server-for-stock-trading",
        "best-Discord-server-for-crypto-trading",
        "best-Discord-trading-community",
        "best-Discord-server-for-options-trading",
        "best-Discord-server-for-day-trading",
    ]
    for q in queries:
        html = http_get(
            f"https://www.quora.com/{q}",
            headers=headers, rate_wait=5
        )
        if not html: time.sleep(3); continue
        for code in extract_codes(html):
            if add_result(code, "Quora", q.replace("-", " ")): found += 1
        time.sleep(random.uniform(4, 7))
    return found

def scrape_substack():
    """Substack finance newsletters — about pages and posts list Discord communities."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://substack.com/"}
    # Substack has a discovery page for finance newsletters
    for cat in ["finance", "investing", "crypto"]:
        html = http_get(
            f"https://substack.com/browse/{cat}?utm_source=discover",
            headers=headers, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Substack/{cat}", "newsletter discovery"): found += 1
        time.sleep(random.uniform(3, 5))
    # Also search Substack
    for term in ["trading discord", "forex discord", "crypto discord", "investing discord"]:
        html = http_get(
            f"https://substack.com/search?query={urllib.parse.quote(term)}&type=post",
            headers=headers, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, "Substack/search", term): found += 1
        time.sleep(random.uniform(3, 5))
    return found

# ══════════════════════════════════════════════════════════════════
# TIER 3 — INTERNATIONAL & INDIGENOUS PLATFORMS
# ══════════════════════════════════════════════════════════════════

def scrape_hotcopper():
    """HotCopper.com — largest Australian stock market forum. Very active.
    Public threads, completely untapped by other scrapers."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://hotcopper.com.au/"}
    for page in range(1, 5):
        html = http_get(
            f"https://hotcopper.com.au/threads/?page={page}",
            headers=headers, rate_wait=4
        )
        if not html: break
        for code in extract_codes(html):
            if add_result(code, "HotCopper", f"page {page}"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_smartlab():
    """Smart-lab.ru — largest Russian-language trading community.
    Completely untapped. Public blog posts and forum threads."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://smart-lab.ru/",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    for path in ["blog/all/", "forum/", "blog/trades/"]:
        for page in range(1, 4):
            html = http_get(
                f"https://smart-lab.ru/{path}page{page}/",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"Smart-lab.ru/{path}", f"page {page}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_nairaland():
    """Nairaland.com — Nigeria's largest forum with 3M+ members.
    Investment and Business sections have active Discord sharing."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.nairaland.com/"}
    sections = ["investment", "business", "phones", "crypto"]
    for section in sections:
        for page in range(0, 4):
            html = http_get(
                f"https://www.nairaland.com/{section}/{page}",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"Nairaland/{section}", f"page {page+1}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_valuePickr():
    """ValuePickr.com — Indian stock market community. High-quality discussions."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://forum.valuepickr.com/"}
    for cat in ["portfolio-management", "stock-opportunities", "trading"]:
        html = http_get(
            f"https://forum.valuepickr.com/c/{cat}.json",
            headers={**headers, "Accept": "application/json"}, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"ValuePickr/{cat}", "indian stock community"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_traderji():
    """Traderji.com — India's oldest trading forum."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.traderji.com/"}
    for cat in ["equity", "derivatives", "forex", "commodities"]:
        for page in range(1, 4):
            html = http_get(
                f"https://www.traderji.com/community/forums/{cat}.{page}/",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"Traderji/{cat}", f"page {page}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_myfxbook():
    """Myfxbook.com — forex performance tracking community with active forum."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.myfxbook.com/"}
    for page in range(1, 5):
        html = http_get(
            f"https://www.myfxbook.com/community/general?page={page}",
            headers=headers, rate_wait=4
        )
        if not html: break
        for code in extract_codes(html):
            if add_result(code, "Myfxbook", f"page {page}"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_investagrams():
    """Investagrams.com — Philippine stock community. Public posts with Discord links."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.investagrams.com/"}
    html = http_get(
        "https://www.investagrams.com/Social",
        headers=headers, rate_wait=4
    )
    if html:
        for code in extract_codes(html):
            if add_result(code, "Investagrams", "PH stock community"): found += 1
    return found

def scrape_wallstonline():
    """Wallstreet-Online.de — Germany's largest stock market community."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.wallstreet-online.de/",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    for cat in ["trading", "fonds-etfs", "zertifikate"]:
        for page in range(1, 4):
            html = http_get(
                f"https://www.wallstreet-online.de/community/{cat}?page={page}",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"WallstOnline/{cat}", f"DE page {page}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_boursorama():
    """Boursorama.com — France's largest financial community."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.boursorama.com/",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
    for forum in ["trading", "devises", "cryptomonnaies"]:
        for page in range(1, 4):
            html = http_get(
                f"https://www.boursorama.com/forum/{forum}?page={page}",
                headers=headers, rate_wait=4
            )
            if not html: break
            for code in extract_codes(html):
                if add_result(code, f"Boursorama/{forum}", f"FR page {page}"): found += 1
            time.sleep(random.uniform(3, 5))
    return found

def scrape_xueqiu():
    """Xueqiu.com (雪球) — China's largest stock community. Public posts."""
    found   = 0
    headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Referer":         "https://xueqiu.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    for topic in ["discord", "discord.gg", "股票discord"]:
        html = http_get(
            f"https://xueqiu.com/search/query.json?queryType=post&q={urllib.parse.quote(topic)}&count=20",
            headers={**headers, "Accept": "application/json"}, rate_wait=5
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, "Xueqiu/雪球", topic): found += 1
        time.sleep(random.uniform(4, 6))
    return found

def scrape_stockhouse():
    """StockHouse.com — Canadian stock market community."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://stockhouse.com/"}
    for board in ["marijuana-stocks", "tsx-venture", "mining"]:
        html = http_get(
            f"https://stockhouse.com/opinion/bullboards/trending",
            headers=headers, rate_wait=4
        )
        if not html: break
        for code in extract_codes(html):
            if add_result(code, "StockHouse", "CA community"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_coinGeckoNFT():
    """CoinGecko NFT section — NFT collection pages list Discord invites."""
    found   = 0
    headers = {"Accept": "application/json", "User-Agent": random.choice(USER_AGENTS)}
    html = http_get(
        "https://api.coingecko.com/api/v3/nfts/list?per_page=100&page=1",
        headers=headers, rate_wait=5
    )
    if not html: return 0
    try:
        nfts = json.loads(html)
        for nft in nfts[:30]:
            nft_id = nft.get("id","")
            if not nft_id: continue
            dhtml = http_get(
                f"https://api.coingecko.com/api/v3/nfts/{nft_id}",
                headers=headers, rate_wait=3
            )
            if not dhtml: continue
            for code in extract_codes(dhtml):
                if add_result(code, f"CoinGecko/NFT:{nft_id}", "nft collection"): found += 1
            time.sleep(random.uniform(1.5, 3))
    except Exception as e:
        logger.warning(f"CoinGecko NFT error: {e}")
    return found

def scrape_mirror_xyz():
    """Mirror.xyz — Web3 blogging. Every project posts Discord links."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://mirror.xyz/"}
    for tag in ["defi", "nft", "trading", "crypto", "dao"]:
        html = http_get(
            f"https://mirror.xyz/tag/{tag}",
            headers=headers, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"Mirror.xyz/{tag}", "web3 blog"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

def scrape_producthunt():
    """Product Hunt — fintech/trading tools listed here link to Discord communities."""
    found   = 0
    headers = {"User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://www.producthunt.com/"}
    for topic in ["finance", "crypto", "investing", "trading"]:
        html = http_get(
            f"https://www.producthunt.com/topics/{topic}",
            headers=headers, rate_wait=4
        )
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"ProductHunt/{topic}", "fintech product"): found += 1
        time.sleep(random.uniform(3, 5))
    return found

# ── Orchestrator ──────────────────────────────────────────────────
def run_scrape(config, username):
    _tl.username = username
    st = get_scrape(username)
    st.update({"running": True, "finished": False, "results": [], "skipped": 0,
               "seen_codes": set(), "progress": [], "error": None, "bank_served": False})
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

        hist = get_user_history_stats(username)
        log(f"👤 {username} — {hist['total_seen']} total seen, {hist['active']} blocked (<{SERVER_EXPIRY_DAYS}d)")

        # ── Bank check ────────────────────────────────────────────
        # Translate keywords to English for bank matching
        translated_search_kws = [translate_to_english(k) for k in keywords]
        bank_results = bank_search(translated_search_kws)
        # Filter out servers already seen by this user
        bank_results = [r for r in bank_results if is_fresh_for_user(r["code"], username)]
        log(f"🏦 Bank: {len(bank_results)} fresh matching servers found")

        if len(bank_results) >= BANK_SERVE_THRESHOLD:
            # Enough in bank — serve directly, skip scraping
            log(f"✅ Serving {len(bank_results)} servers from bank — no scrape needed.")
            served = bank_results[:BANK_SERVE_THRESHOLD]
            for r in served:
                st["seen_codes"].add(r["code"])
                st["results"].append(r)
            bank_increment_hits([r["code"] for r in served])
            add_to_user_history(username, served)
            save_results_to_db(username, served, datetime.datetime.now().isoformat())
            st["bank_served"] = True
            log(f"🏦 Done! {len(served)} servers served from bank.")
        else:
            # Not enough in bank — run scrape, then merge
            if bank_results:
                log(f"🏦 Only {len(bank_results)} in bank — running scrape to top up.")
            else:
                log(f"🏦 Bank miss — running full scrape.")

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

        # ── TIER 2 — Forums & Structured Sites ───────────────────
        # Run every 3rd scrape to balance bandwidth usage
        scrape_count = get_bank_stats() // 20  # rough proxy for run count
        run_tier2    = (scrape_count % 3 == 0)
        run_tier3    = (scrape_count % 7 == 0)

        if run_tier2 or "forexfactory"  in sources: log("🔍 Scraping ForexFactory…");     n = scrape_forexfactory();    log(f"  → {n} new")
        if run_tier2 or "elitetrader"   in sources: log("🔍 Scraping EliteTrader…");      n = scrape_elitetrader();     log(f"  → {n} new")
        if run_tier2 or "babypips"      in sources: log("🔍 Scraping BabyPips…");         n = scrape_babypips();        log(f"  → {n} new")
        if run_tier2 or "bitcointalk"   in sources: log("🔍 Scraping Bitcointalk…");      n = scrape_bitcointalk();     log(f"  → {n} new")
        if run_tier2 or "coinmarketcap" in sources: log("🔍 Scraping CoinMarketCap…");    n = scrape_coinmarketcap();   log(f"  → {n} new")
        if run_tier2 or "coingecko"     in sources: log("🔍 Scraping CoinGecko…");        n = scrape_coingecko();       log(f"  → {n} new")
        if run_tier2 or "quora"         in sources: log("🔍 Scraping Quora…");            n = scrape_quora();           log(f"  → {n} new")
        if run_tier2 or "substack"      in sources: log("🔍 Scraping Substack…");         n = scrape_substack();        log(f"  → {n} new")
        if run_tier2 or "myfxbook"      in sources: log("🔍 Scraping Myfxbook…");         n = scrape_myfxbook();        log(f"  → {n} new")
        if run_tier2 or "producthunt"   in sources: log("🔍 Scraping Product Hunt…");     n = scrape_producthunt();     log(f"  → {n} new")

        # ── TIER 3 — International & Indigenous Platforms ─────────
        # Run every 7th scrape — high yield, slower
        if run_tier3 or "hotcopper"     in sources: log("🔍 Scraping HotCopper (AU)…");   n = scrape_hotcopper();       log(f"  → {n} new")
        if run_tier3 or "smartlab"      in sources: log("🔍 Scraping Smart-lab (RU)…");   n = scrape_smartlab();        log(f"  → {n} new")
        if run_tier3 or "nairaland"     in sources: log("🔍 Scraping Nairaland (NG)…");   n = scrape_nairaland();       log(f"  → {n} new")
        if run_tier3 or "valuepickr"    in sources: log("🔍 Scraping ValuePickr (IN)…");  n = scrape_valuePickr();      log(f"  → {n} new")
        if run_tier3 or "traderji"      in sources: log("🔍 Scraping Traderji (IN)…");    n = scrape_traderji();        log(f"  → {n} new")
        if run_tier3 or "wallstonline"  in sources: log("🔍 Scraping WallstOnline (DE)…");n = scrape_wallstonline();    log(f"  → {n} new")
        if run_tier3 or "boursorama"    in sources: log("🔍 Scraping Boursorama (FR)…");  n = scrape_boursorama();      log(f"  → {n} new")
        if run_tier3 or "xueqiu"        in sources: log("🔍 Scraping Xueqiu 雪球 (CN)…"); n = scrape_xueqiu();          log(f"  → {n} new")
        if run_tier3 or "stockhouse"    in sources: log("🔍 Scraping StockHouse (CA)…");  n = scrape_stockhouse();      log(f"  → {n} new")
        if run_tier3 or "investagrams"  in sources: log("🔍 Scraping Investagrams (PH)…");n = scrape_investagrams();    log(f"  → {n} new")
        if run_tier3 or "mirroxyz"      in sources: log("🔍 Scraping Mirror.xyz…");       n = scrape_mirror_xyz();      log(f"  → {n} new")
        if run_tier3 or "coingeckonft"  in sources: log("🔍 Scraping CoinGecko NFTs…");   n = scrape_coinGeckoNFT();    log(f"  → {n} new")

            # Merge bank results with scraped results
        if bank_results:
            existing_codes = st["seen_codes"]
            for r in bank_results:
                if r["code"] not in existing_codes:
                    existing_codes.add(r["code"])
                    st["results"].append(r)
            bank_increment_hits([r["code"] for r in bank_results])
            log(f"🏦 Merged {len(bank_results)} bank results with scrape results.")

        total = len(st["results"])
        log(f"✅ Done! {total} new servers found, {st['skipped']} already-seen skipped.", "info")
        logger.info(f"[{username}] Scrape complete — {total} results in memory")

        if st["results"]:
            save_results_to_db(username, st["results"], datetime.datetime.now().isoformat())
            # Push scraped results to bank for future users
            scrape_only = [r for r in st["results"] if not r.get("source","").endswith("[bank]")]
            if scrape_only:
                bank_push(scrape_only, keywords)
                log(f"🏦 {len(scrape_only)} new servers pushed to bank.")

        # end of else (scrape) block

    except Exception as e:
        st["error"] = str(e)
        log(f"❌ Fatal error: {e}", "error")
        logger.exception("Scrape error")
    finally:
        # Save scrape_results for session restore even if stopped/crashed
        if st["results"]:
            try:
                save_results_to_db(username, st["results"], datetime.datetime.now().isoformat())
            except Exception as e:
                logger.warning(f"[{username}] Finally save_results failed: {e}")
        st["running"]  = False
        st["finished"] = True

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
        u  = r["username"] if isinstance(r, dict) else r[0]
        st = get_user_history_stats(u)
        result[u] = {
            "role":           r["role"]           if isinstance(r, dict) else r[1],
            "scrape_credits": r["scrape_credits"] if isinstance(r, dict) else r[2],
            "daily_credits":  r["daily_credits"]  if isinstance(r, dict) else r[3],
            "last_reset":     r["last_reset"]     if isinstance(r, dict) else r[4],
            "created":        r["created"]         if isinstance(r, dict) else r[5],
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
    try:
        st = get_scrape(username)
        if st["running"]:
            return jsonify({"error": "Already running"}), 400
        credits = get_user_credits(username)
        if credits <= 0:
            return jsonify({"error": "no_credits",
                            "message": "No scrape credits left. Contact admin to top up."}), 403
        if not deduct_credit(username):
            return jsonify({"error": "no_credits",
                            "message": "No scrape credits left. Contact admin to top up."}), 403
        config = request.get_json(silent=True) or {}
        threading.Thread(target=run_scrape, args=(config, username), daemon=True).start()
        return jsonify({"status": "started",
                        "credits_remaining": get_user_credits(username)})
    except Exception as e:
        logger.exception("start_scrape error")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

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
        "running":     st["running"],
        "finished":    st.get("finished", False),
        "bank_served": st.get("bank_served", False),
        "count":       len(st["results"]),
        "skipped":     st["skipped"],
        "progress":    st["progress"][-60:],
        "error":       st["error"],
        "bank_total":  get_bank_stats(),
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

@app.route("/api/db-test")
@login_required
def db_test():
    """Diagnostic endpoint — tests Turso connectivity and write/read."""
    results = {}
    try:
        # Test 1: basic connectivity
        with get_db() as conn:
            row = conn.execute("SELECT 1 as x").fetchone()
            results["connect"] = "OK" if row else "FAIL — no row returned"
            results["select_value"] = row.get("x") if row else None

        # Test 2: write to server_history
        test_code = f"__test_{int(time.time())}"
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_history (username,code,first_seen) VALUES (?,?,?)",
                (session["user"], test_code, datetime.datetime.now().isoformat())
            )
        results["write"] = "OK"

        # Test 3: read it back
        with get_db() as conn:
            row = conn.execute(
                "SELECT code FROM server_history WHERE username=? AND code=?",
                (session["user"], test_code)
            ).fetchone()
        results["read_back"] = "OK" if row else "FAIL — write didn't persist"

        # Cleanup
        with get_db() as conn:
            conn.execute(
                "DELETE FROM server_history WHERE username=? AND code=?",
                (session["user"], test_code)
            )

        # Test 4: count history
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM server_history WHERE username=?",
                (session["user"],)
            ).fetchone()
        results["history_count"] = row.get("c") if row else 0

        results["turso_url"]    = TURSO_HTTP_URL or "NOT SET"
        results["using_turso"]  = bool(TURSO_HTTP_URL and TURSO_TOKEN)
        results["status"] = "ALL OK" if results["read_back"] == "OK" else "WRITE FAILED"

    except Exception as e:
        results["error"] = str(e)
        results["status"] = "ERROR"

    return jsonify(results)

@app.route("/api/bank/stats")
@login_required
def bank_stats():
    try:
        with get_db() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM server_bank").fetchone()
            total = total["c"] if isinstance(total, dict) else total[0]
        return jsonify({"total": total})
    except Exception as e:
        return jsonify({"total": 0, "error": str(e)})

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
