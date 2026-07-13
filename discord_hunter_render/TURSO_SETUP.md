# 🗄️ Turso Database Setup (Free, No Card)

Turso is a hosted SQLite database — free tier gives 500MB and works perfectly
with this app. No credit card required.

---

## Step 1 — Create a Turso account

Go to **https://turso.tech** and sign up with GitHub (easiest) or email.

---

## Step 2 — Install the Turso CLI

**Mac / Linux:**
```bash
curl -sSfL https://get.tur.so/install.sh | bash
```

**Windows:** Download from https://github.com/tursodatabase/turso-cli/releases

---

## Step 3 — Log in and create a database

```bash
turso auth login
turso db create discord-hunter
```

---

## Step 4 — Get your URL and token

```bash
turso db show discord-hunter --url
turso db tokens create discord-hunter
```

The first command gives you the URL (looks like `libsql://discord-hunter-yourname.turso.io`).
The second gives you a long auth token string.

---

## Step 5 — Add to Render

In your Render service → **Environment** tab, add:

| Key | Value |
|---|---|
| `TURSO_URL` | `libsql://discord-hunter-yourname.turso.io` |
| `TURSO_TOKEN` | the long token string from step 4 |

---

## Step 6 — Remove DATA_DIR if set

If you previously had `DATA_DIR=/data` as an env var, delete it — it's no
longer needed. The app now uses Turso instead of a local SQLite file.

---

## Done

Redeploy and check logs for:
```
=== Turso URL: libsql://discord-hunter-yourname.turso.io ===
Database ready (Turso: True)
```

Your data now persists forever across all deploys and restarts, completely free.

