# 🚀 Deploying Discord Link Hunter to Render.com

## What's different from local version

| Thing | Local | Render |
|---|---|---|
| Data storage | `users.json` file | SQLite on Persistent Disk |
| Server | Flask dev server | Gunicorn |
| Config | Hardcoded | Environment variables |
| Secret key | Random per restart | Fixed env var (sessions survive) |

---

## Step 1 — Push code to GitHub

Render deploys from Git. You need a GitHub repo.

```bash
# In the discord_hunter_render folder:
git init
git add .
git commit -m "Initial deploy"
```

Then go to github.com → New repository → create one, then:

```bash
git remote add origin https://github.com/YOURUSERNAME/discord-hunter.git
git branch -M main
git push -u origin main
```

---

## Step 2 — Create a Render account

Go to **render.com** and sign up (free tier available).

---

## Step 3 — Create a Web Service

1. Click **New** → **Web Service**
2. Connect your GitHub account and select your repo
3. Render will auto-detect the `render.yaml` — click **Approve**

If it asks you to configure manually, use these settings:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --workers 2 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT` |
| Instance Type | Starter ($7/mo) or Free |

---

## Step 4 — Add the Persistent Disk

⚠️ This is critical — without it, your user accounts reset on every deploy.

1. In your service → **Disks** tab → **Add Disk**
2. Settings:
   - **Name:** `data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB (~$0.25/mo)
3. Save

---

## Step 5 — Set Environment Variables

In your service → **Environment** tab, add:

| Key | Value |
|---|---|
| `DATA_DIR` | `/data` |
| `SECRET_KEY` | Click **Generate** — Render creates a random value |

`SECRET_KEY` being fixed means user sessions survive restarts/deploys.

---

## Step 6 — Deploy

Click **Manual Deploy** → **Deploy latest commit**.

Watch the build logs. You'll see:
```
⚠️  No users found — default admin created:
    Username: admin  |  Password: admin123
```

Your app is live at `https://your-service-name.onrender.com`

---

## Step 7 — First login & setup

1. Go to your Render URL
2. Login: `admin` / `admin123`
3. Go to **⚙ Users** → change admin password immediately
4. Create user accounts with initial credits

---

## Free tier note

Render's free tier **spins down after 15 minutes of inactivity** — the first request after sleep takes ~30 seconds. Upgrade to Starter ($7/mo) if you need it always-on.

---

## Redeploying after code changes

```bash
git add .
git commit -m "update"
git push
```

Render auto-deploys on every push. Your database on `/data` is untouched — all users and history survive.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Application error" on first load | Check Render logs — usually a missing env var |
| Users reset after deploy | Persistent Disk not attached, or `DATA_DIR` not set |
| Sessions log out on redeploy | `SECRET_KEY` env var not set (using random key) |
| "No module named gunicorn" | Build didn't run — trigger Manual Deploy |
| Slow first load | Free tier sleep — upgrade to Starter or use UptimeRobot to ping it |

---

## Keeping it awake (free tier only)

Use **UptimeRobot** (free) to ping your URL every 5 minutes:
1. Go to uptimerobot.com → Add Monitor
2. Type: HTTP(s), URL: your Render URL, interval: 5 minutes
3. This prevents the free tier from sleeping

