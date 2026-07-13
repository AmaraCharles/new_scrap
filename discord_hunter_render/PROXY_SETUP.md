# 🔧 Setting Up Credentials for the Scraper

The scraper needs two sets of credentials to work from Render:
- **Reddit OAuth** — for Reddit (works natively from any cloud server)
- **Webshare proxy** — for all other sites (routes traffic through residential IPs)

---

## 1. Reddit OAuth (Free)

Reddit's official API works from cloud servers when you authenticate properly.

### Steps:
1. Log into Reddit and go to: **https://www.reddit.com/prefs/apps**
2. Scroll down and click **"Create another app..."**
3. Fill in:
   - **Name:** discord-hunter
   - **Type:** Select **script**
   - **Redirect URI:** `http://localhost`
4. Click **Create app**
5. You'll see:
   - **Client ID** — the short string under "personal use script"
   - **Client secret** — shown as "secret"

### Add to Render:
In your Render service → **Environment** tab:
- `REDDIT_CLIENT_ID` = your client ID
- `REDDIT_CLIENT_SECRET` = your client secret

---

## 2. Webshare Proxy (Needed for all other sites)

Every other site (Disboard, Bing, DuckDuckGo, Whop, etc.) blocks Render's datacenter IP.
Webshare routes your requests through real residential IPs.

### Steps:
1. Go to **https://webshare.io** and sign up (free account)
2. Free tier gives you: 10 proxies, 1GB/month bandwidth
3. Go to **Proxy → Residential** in the dashboard
4. Find your **proxy username** and **proxy password**
   (They look like: `abc123def` / `xyz789uvw`)
5. The proxy endpoint is always: `p.webshare.io:80`

### Add to Render:
In your Render service → **Environment** tab:
- `WEBSHARE_USER` = your proxy username
- `WEBSHARE_PASS` = your proxy password

---

## Bandwidth estimate

At Normal depth scraping all sources, one full scrape uses roughly:
- Reddit: ~5MB (direct, doesn't use proxy bandwidth)
- All other sources: ~80–150MB proxy bandwidth

On Webshare's free tier (1GB/month), you can run roughly **6–10 full scrapes/month**.
Their cheapest paid plan ($2.99/mo) gives 1GB more, ~10–15 scrapes.
The $9.99/mo plan (10GB) is effectively unlimited for this use case.

---

## After adding credentials

Redeploy on Render (or it picks them up automatically within a minute).
You should see in the logs:
```
Reddit OAuth token obtained
```
And scrapes will start returning results from all sources.

