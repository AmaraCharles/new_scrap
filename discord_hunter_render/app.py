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
# SECRET_KEY must be set as env var on Render — sessions survive restarts
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Persistent disk path ──────────────────────────────────────────
# On Render, mount a Persistent Disk at /data
# Locally it falls back to ./data
DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH  = os.path.join(DATA_DIR, "app.db")
os.makedirs(DATA_DIR, exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────
SESSION_TIMEOUT    = 60 * 60 * 8   # 8 hours
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
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                salt            TEXT NOT NULL,
                hash            TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'user',
                scrape_credits  INTEGER NOT NULL DEFAULT 0,
                created         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_history (
                username    TEXT NOT NULL,
                code        TEXT NOT NULL,
                first_seen  TEXT NOT NULL,
                PRIMARY KEY (username, code),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            );
        """)
    logger.info(f"Database ready at {DB_PATH}")

def bootstrap_admin():
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if row:
            return
        salt, hashed = hash_password("admin123")
        conn.execute(
            "INSERT INTO users (username, salt, hash, role, scrape_credits, created) VALUES (?,?,?,?,?,?)",
            ("admin", salt, hashed, "admin", 999, datetime.datetime.now().isoformat())
        )
    print("\n⚠️  No users found — default admin created:")
    print("    Username: admin  |  Password: admin123")
    print("    ⚠️  Change this immediately via /admin/users\n")

# ── Auth helpers ──────────────────────────────────────────────────
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
        uid        = session.get("user")
        login_time = session.get("login_time", 0)
        if not uid or (time.time() - login_time) > SESSION_TIMEOUT:
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
    if not user:
        return 0
    if user["role"] == "admin":
        return 999
    return user["scrape_credits"]

def deduct_credit(username):
    user = get_user(username)
    if not user:
        return False
    if user["role"] == "admin":
        return True
    if user["scrape_credits"] <= 0:
        return False
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET scrape_credits = scrape_credits - 1 WHERE username=? AND scrape_credits > 0",
            (username,)
        )
    return True

def set_credits(username, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET scrape_credits=? WHERE username=?", (max(0, amount), username))
    return True

def add_credits(username, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET scrape_credits = scrape_credits + ? WHERE username=?",
                     (max(0, amount), username))
    return True

# ── Per-user server history ───────────────────────────────────────
def get_user_history(username):
    """Returns {code: iso_date} dict."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, first_seen FROM server_history WHERE username=?", (username,)
        ).fetchall()
    return {r["code"]: r["first_seen"] for r in rows}

def add_to_user_history(username, codes_with_meta):
    now = datetime.datetime.now().isoformat()
    rows = [(username, item["code"], now) for item in codes_with_meta]
    with get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO server_history (username, code, first_seen) VALUES (?,?,?)",
            rows
        )

def is_fresh_for_user(code, username):
    with get_db() as conn:
        row = conn.execute(
            "SELECT first_seen FROM server_history WHERE username=? AND code=?",
            (username, code)
        ).fetchone()
    if not row:
        return True
    try:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(row["first_seen"])).days
        return age > SERVER_EXPIRY_DAYS
    except Exception:
        return True

def get_user_history_stats(username):
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM server_history WHERE username=?", (username,)
        ).fetchone()[0]
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=SERVER_EXPIRY_DAYS)).isoformat()
        active = conn.execute(
            "SELECT COUNT(*) FROM server_history WHERE username=? AND first_seen > ?",
            (username, cutoff)
        ).fetchone()[0]
    return {"total_seen": total, "active": active, "eligible": total - active}

# ── Global scrape state ───────────────────────────────────────────
scrape_status = {
    "running":    False,
    "progress":   [],
    "results":    [],
    "skipped":    0,
    "seen_codes": set(),
    "error":      None,
    "username":   None,
}

# ── Core helpers ──────────────────────────────────────────────────
def extract_codes(text):
    codes = []
    for m in DISCORD_PATTERN.finditer(text or ""):
        code = m.group(1) or m.group(2)
        if code and 2 < len(code) < 50:
            if code.lower() not in ("nitro","app","channels","login",
                                    "register","developers","download"):
                codes.append(code)
    return list(dict.fromkeys(codes))

def build_invite_url(code):
    return f"https://discord.gg/{code}"

def log(msg, level="info"):
    scrape_status["progress"].append({"msg": msg, "level": level, "ts": time.time()})
    getattr(logger, level)(msg)

def add_result(code, source, context="", paid=False, price_hint=""):
    username = scrape_status.get("username")
    if code in scrape_status["seen_codes"]:
        return False
    if username and not is_fresh_for_user(code, username):
        scrape_status["skipped"] += 1
        return False
    scrape_status["seen_codes"].add(code)
    scrape_status["results"].append({
        "code":       code,
        "url":        build_invite_url(code),
        "source":     source,
        "context":    context[:140],
        "found_at":   datetime.datetime.now().strftime("%H:%M:%S"),
        "paid":       paid,
        "price_hint": price_hint[:60] if price_hint else "",
    })
    return True

def http_get(url, headers=None, timeout=15, retries=3):
    base_headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base_headers.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 12 * (attempt + 1)
                log(f"  Rate limited — waiting {wait}s", "warning")
                time.sleep(wait)
            elif e.code in (403, 404, 410):
                return None
            else:
                time.sleep(3 * (attempt + 1))
        except Exception as exc:
            if attempt == retries - 1:
                log(f"  Request failed {url[:60]}…: {exc}", "warning")
            time.sleep(3)
    return None

# ── SCRAPERS ──────────────────────────────────────────────────────
def scrape_reddit_subreddit(subreddit, limit=100):
    found = 0
    for sort in ["new", "hot"]:
        html = http_get(f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}",
                        headers={"Accept": "application/json"})
        if not html: continue
        try: data = json.loads(html)
        except Exception: continue
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            pd   = post.get("data", {})
            text = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("url","")
            for code in extract_codes(text):
                if add_result(code, f"Reddit r/{subreddit}", pd.get("title","")[:80]):
                    found += 1
        for post in posts[:15]:
            pd        = post.get("data", {})
            permalink = pd.get("permalink", "")
            if not permalink: continue
            chtml = http_get(f"https://www.reddit.com{permalink}.json?limit=50",
                             headers={"Accept": "application/json"})
            if not chtml: continue
            try:
                for c in json.loads(chtml)[1]["data"]["children"]:
                    body = c.get("data", {}).get("body", "")
                    for code in extract_codes(body):
                        if add_result(code, f"Reddit r/{subreddit} comment", body[:80]):
                            found += 1
            except Exception: pass
            time.sleep(0.6)
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_reddit_search(keywords):
    found = 0
    for kw in keywords:
        html = http_get(f"https://www.reddit.com/search.json?q={urllib.parse.quote(kw)}&sort=new&limit=100&type=link,comment",
                        headers={"Accept": "application/json"})
        if not html: time.sleep(2); continue
        try: data = json.loads(html)
        except Exception: continue
        posts = data.get("data", {}).get("children", [])
        log(f"  Reddit keyword '{kw}': {len(posts)} posts")
        for post in posts:
            pd   = post.get("data", {})
            text = pd.get("title","")+" "+pd.get("selftext","")+" "+pd.get("body","")+" "+pd.get("url","")
            for code in extract_codes(text):
                if add_result(code, f"Reddit search: {kw}", pd.get("title", pd.get("body",""))[:80]):
                    found += 1
        time.sleep(random.uniform(2, 4))
    return found

def scrape_disboard(pages=3):
    found    = 0
    tags     = ["trading","forex","crypto-trading","stocks","investing",
                "day-trading","options-trading","futures","signals","prop-firm"]
    inv_re   = re.compile(r'href=["\']https?://discord(?:app)?\.com/invite/([A-Za-z0-9\-_]+)["\']'
                          r'|href=["\']https?://discord\.gg/([A-Za-z0-9\-_]+)["\']', re.IGNORECASE)
    for tag in tags:
        for page in range(1, pages + 1):
            html = http_get(f"https://disboard.org/servers/tag/{tag}?page={page}&fl=en&sort=-member_count",
                            headers={"Referer": "https://disboard.org/"})
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
            html = http_get(f"https://discord.me/servers/{page}?keyword={tag}")
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
            html = http_get(f"https://{inst}/search?q={encoded}&f=tweets")
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
    price_re   = re.compile(r'\$[\d,]+(?:\.\d{2})?(?:\s*/\s*(?:mo|month|week|wk|yr|year))?', re.IGNORECASE)
    for cat in categories:
        for page in range(1, pages + 1):
            html = http_get(f"https://whop.com/marketplace/?category={cat}&page={page}",
                            headers={"Referer": "https://whop.com/"})
            if not html: continue
            prices     = price_re.findall(html)
            price_hint = prices[0] if prices else ""
            codes = [m.group(1) for m in invite_re.finditer(html)] + [m.group(1) for m in link_re.finditer(html)]
            for code in list(dict.fromkeys(codes)):
                if add_result(code, f"Whop.com:{cat}", f"paid — {price_hint}", paid=True, price_hint=price_hint): found += 1
            prod_re = re.compile(r'href=[\"\']/([\w\-]+)[\"\'][^>]*>(?:[^<]*<[^>]*>)*[^<]*(?:trading|forex|crypto|signal|invest)', re.IGNORECASE)
            for slug in [m.group(1) for m in prod_re.finditer(html)][:8]:
                phtml = http_get(f"https://whop.com/{slug}/", headers={"Referer": "https://whop.com/marketplace/"})
                if not phtml: continue
                ph = (price_re.findall(phtml) or [""])[0]
                for code in extract_codes(phtml):
                    if add_result(code, f"Whop.com product:{slug}", f"paid — {ph}", paid=True, price_hint=ph): found += 1
                time.sleep(random.uniform(1, 2))
            time.sleep(random.uniform(2, 4))
        log(f"  Whop '{cat}': {found} total so far")
    return found

def scrape_patreon(keywords):
    found    = 0
    price_re = re.compile(r'\$[\d]+(?:\.\d{2})?(?:/mo)?', re.IGNORECASE)
    terms    = ["trading signals discord","forex discord","crypto signals discord",
                "stock trading discord","options trading discord","day trading discord",
                "funded trader discord","prop firm discord"]
    for term in terms:
        html = http_get(f"https://www.patreon.com/search?q={urllib.parse.quote(term)}",
                        headers={"Referer": "https://www.patreon.com/"})
        if not html: time.sleep(2); continue
        ph = (price_re.findall(html) or [""])[0]
        for code in extract_codes(html):
            if add_result(code, f"Patreon:{term}", f"paid creator — {ph}", paid=True, price_hint=ph): found += 1
        creator_re = re.compile(r'"url":"https://www\.patreon\.com/([a-zA-Z0-9_\-]+)"', re.IGNORECASE)
        for slug in list(dict.fromkeys(creator_re.findall(html)))[:10]:
            if slug in ("home","login","signup","explore","search","about"): continue
            chtml = http_get(f"https://www.patreon.com/{slug}", headers={"Referer": "https://www.patreon.com/search"})
            if not chtml: continue
            ch = (price_re.findall(chtml) or [""])[0]
            for code in extract_codes(chtml):
                if add_result(code, f"Patreon creator:{slug}", f"paid — {ch}", paid=True, price_hint=ch): found += 1
            time.sleep(random.uniform(1.5, 3))
        time.sleep(random.uniform(2, 4))
    return found

def scrape_gumroad():
    found    = 0
    price_re = re.compile(r'\$[\d]+(?:\.\d{2})?', re.IGNORECASE)
    terms    = ["trading signals","forex course discord","crypto signals",
                "stock trading course","options trading","day trading signals"]
    for term in terms:
        html = http_get(f"https://gumroad.com/discover?query={urllib.parse.quote(term)}&sort=featured",
                        headers={"Referer": "https://gumroad.com/"})
        if not html: time.sleep(2); continue
        ph = (price_re.findall(html) or [""])[0]
        for code in extract_codes(html):
            if add_result(code, f"Gumroad:{term}", f"paid — {ph}", paid=True, price_hint=ph): found += 1
        prod_re = re.compile(r'href=["\']https://[a-z0-9\-]+\.gumroad\.com/l/([a-zA-Z0-9_\-]+)["\']', re.IGNORECASE)
        for slug in list(dict.fromkeys(prod_re.findall(html)))[:6]:
            phtml = http_get(f"https://gumroad.com/l/{slug}", headers={"Referer": "https://gumroad.com/discover"})
            if not phtml: continue
            ph2 = (price_re.findall(phtml) or [""])[0]
            for code in extract_codes(phtml):
                if add_result(code, f"Gumroad product:{slug}", f"paid — {ph2}", paid=True, price_hint=ph2): found += 1
            time.sleep(random.uniform(1, 2))
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_skool():
    found    = 0
    price_re = re.compile(r'\$[\d,]+(?:\.\d{2})?(?:/mo)?', re.IGNORECASE)
    for term in ["trading","forex","crypto","stocks","options","signals"]:
        html = http_get(f"https://www.skool.com/discover?q={urllib.parse.quote(term)}",
                        headers={"Referer": "https://www.skool.com/"})
        if not html: time.sleep(2); continue
        prices = price_re.findall(html)
        ph     = prices[0] if prices else ""
        for code in extract_codes(html):
            if add_result(code, f"Skool.com:{term}", f"community — {ph}", paid=bool(prices), price_hint=ph): found += 1
        slug_re = re.compile(r'href=[\"\']\/([a-zA-Z0-9_\-]+)[\"\'][^>]*class=[\"|\'][^\"\']*community', re.IGNORECASE)
        for slug in list(dict.fromkeys(m.group(1) for m in slug_re.finditer(html)))[:8]:
            if slug in ("discover","login","signup","about","pricing"): continue
            chtml = http_get(f"https://www.skool.com/{slug}", headers={"Referer": "https://www.skool.com/discover"})
            if not chtml: continue
            cp = price_re.findall(chtml)
            ch = cp[0] if cp else ""
            for code in extract_codes(chtml):
                if add_result(code, f"Skool.com group:{slug}", f"community — {ch}", paid=bool(cp), price_hint=ch): found += 1
            time.sleep(random.uniform(1, 2))
        time.sleep(random.uniform(2, 3.5))
    return found

def scrape_stocktwits():
    found = 0
    for sym in ["FOREX","CRYPTO","STOCKS","OPTIONS","FUTURES","SPY","BTC.X","ETH.X"]:
        html = http_get(f"https://stocktwits.com/symbol/{sym}")
        if not html: continue
        for code in extract_codes(html):
            if add_result(code, f"StockTwits:{sym}", f"symbol stream {sym}"): found += 1
        time.sleep(random.uniform(2, 3))
    return found

# ── Orchestrator ──────────────────────────────────────────────────
def run_scrape(config, username):
    scrape_status.update({"running": True, "results": [], "skipped": 0,
                          "seen_codes": set(), "progress": [], "error": None, "username": username})
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

        if "reddit" in sources:
            log("🔍 Scraping Reddit subreddits…")
            for i, sub in enumerate(subreddits[:subs_cap]):
                if not scrape_status["running"]: break
                log(f"  [{i+1}/{subs_cap}] r/{sub}")
                n = scrape_reddit_subreddit(sub, limit=sub_limit)
                log(f"  → {n} new from r/{sub}")
                time.sleep(random.uniform(2, 4))
            log("🔍 Searching Reddit by keyword…")
            n = scrape_reddit_search(keywords[:4] if depth == "quick" else keywords)
            log(f"  → {n} new from Reddit search")

        if "disboard"   in sources: log("🔍 Scraping Disboard.org…");         n = scrape_disboard(pages=pages);    log(f"  → {n} new")
        if "discordme"  in sources: log("🔍 Scraping Discord.me…");            n = scrape_discord_me(pages=pages);  log(f"  → {n} new")
        if "twitter"    in sources: log("🔍 Scraping Twitter/X via Nitter…");  n = scrape_twitter_nitter(keywords[:3] if depth=="quick" else keywords[:10]); log(f"  → {n} new")
        if "whop"       in sources: log("💰 Scraping Whop.com…");              n = scrape_whop(pages=pages);        log(f"  → {n} new")
        if "patreon"    in sources: log("💰 Scraping Patreon…");               n = scrape_patreon(keywords);        log(f"  → {n} new")
        if "gumroad"    in sources: log("💰 Scraping Gumroad…");               n = scrape_gumroad();                log(f"  → {n} new")
        if "skool"      in sources: log("💰 Scraping Skool.com…");             n = scrape_skool();                  log(f"  → {n} new")
        if "stocktwits" in sources: log("🔍 Scraping StockTwits…");            n = scrape_stocktwits();             log(f"  → {n} new")

        total = len(scrape_status["results"])
        log(f"✅ Done! {total} new servers, {scrape_status['skipped']} skipped.", "info")
        if scrape_status["results"]:
            add_to_user_history(username, scrape_status["results"])

    except Exception as e:
        scrape_status["error"] = str(e)
        log(f"❌ Fatal error: {e}", "error")
        logger.exception("Scrape error")
    finally:
        scrape_status["running"] = False

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
        rows = conn.execute("SELECT username, role, scrape_credits, created FROM users").fetchall()
    result = {}
    for r in rows:
        st = get_user_history_stats(r["username"])
        result[r["username"]] = {
            "role":          r["role"],
            "scrape_credits": r["scrape_credits"],
            "created":       r["created"],
            "seen_total":    st["total_seen"],
            "seen_active":   st["active"],
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
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (username, salt, hash, role, scrape_credits, created) VALUES (?,?,?,?,?,?)",
            (username, salt, hashed, role, credits, datetime.datetime.now().isoformat())
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
    data   = request.get_json(silent=True) or {}
    action = data.get("action", "add")
    try:   amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Amount must be an integer"}), 400
    if amount < 0:
        return jsonify({"error": "Amount must be 0 or greater"}), 400
    if not get_user(username):
        return jsonify({"error": "User not found"}), 404
    if action == "set": set_credits(username, amount)
    else:               add_credits(username, amount)
    new_total = get_user(username)["scrape_credits"]
    return jsonify({"status": "updated", "username": username, "credits": new_total})

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
    user     = get_user(username)
    return jsonify({
        "credits":   get_user_credits(username),
        "unlimited": user["role"] == "admin",
    })

@app.route("/api/start", methods=["POST"])
@login_required
def start_scrape():
    if scrape_status["running"]:
        return jsonify({"error": "Already running"}), 400
    username = session["user"]
    if get_user_credits(username) <= 0:
        return jsonify({"error": "no_credits",
                        "message": "No scrape credits left. Contact admin to top up."}), 403
    if not deduct_credit(username):
        return jsonify({"error": "no_credits",
                        "message": "No scrape credits left. Contact admin to top up."}), 403
    config = request.get_json(silent=True) or {}
    threading.Thread(target=run_scrape, args=(config, username), daemon=True).start()
    return jsonify({"status": "started", "credits_remaining": get_user_credits(username)})

@app.route("/api/stop", methods=["POST"])
@login_required
def stop_scrape():
    scrape_status["running"] = False
    return jsonify({"status": "stopped"})

@app.route("/api/status")
@login_required
def get_status():
    return jsonify({
        "running":  scrape_status["running"],
        "count":    len(scrape_status["results"]),
        "skipped":  scrape_status["skipped"],
        "progress": scrape_status["progress"][-60:],
        "error":    scrape_status["error"],
    })

@app.route("/api/results")
@login_required
def get_results():
    return jsonify(scrape_status["results"])

@app.route("/api/export")
@login_required
def export_results():
    fmt     = request.args.get("fmt", "json")
    results = scrape_status["results"]
    if fmt == "csv":
        def esc(s): return '"' + str(s).replace('"','""') + '"'
        lines = ["code,url,source,context,paid,price,found_at"]
        for r in results:
            lines.append(",".join([esc(r["code"]), esc(r["url"]), esc(r["source"]),
                                   esc(r["context"]), esc("YES" if r.get("paid") else "NO"),
                                   esc(r.get("price_hint","")), esc(r["found_at"])]))
        return Response("\n".join(lines), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=discord_links.csv"})
    return Response(json.dumps(results, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment;filename=discord_links.json"})

@app.route("/api/clear", methods=["POST"])
@login_required
def clear_results():
    scrape_status.update({"results": [], "seen_codes": set(), "progress": [], "skipped": 0})
    return jsonify({"status": "cleared"})

# ── Startup ───────────────────────────────────────────────────────
init_db()
bootstrap_admin()

if __name__ == "__main__":
    print("\n🎯 Discord Link Hunter — Render Edition")
    print(f"   DB path: {DB_PATH}")
    print("👉  Open http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000, threaded=True)
