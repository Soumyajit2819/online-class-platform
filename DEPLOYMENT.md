# Deployment Guide — Online Class Platform

## Architecture

```
Browser
  ↓
Vercel (Next.js frontend)
  ↓
Railway / Render (FastAPI backend)
  ↓
LiveKit Cloud (live video)
  ↓
Supabase (passcode DB + S3 recording storage)
```

---

## Prerequisites

| Service | Purpose | Free Tier |
|---------|---------|-----------|
| [Vercel](https://vercel.com) | Frontend hosting | ✅ Free |
| [Railway](https://railway.app) or [Render](https://render.com) | Backend hosting | ✅ Free tier |
| [LiveKit Cloud](https://cloud.livekit.io) | Live video | ✅ Free tier |
| [Supabase](https://supabase.com) | DB + Storage | ✅ Free tier |

---

## Step 1 — Supabase Setup

### 1A. Create the database table
Go to your Supabase project → **SQL Editor** → Run:

```sql
CREATE TABLE IF NOT EXISTS platform_config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  label      TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 1B. Create the storage bucket
Go to **Storage** → **New bucket**:
- Name: `class-recordings`
- Public: `true`

### 1C. Get your API keys
Go to **Settings → API**:
- Copy `Project URL`
- Copy `service_role` key (secret)
- Copy `anon` key

### 1D. Get S3 storage credentials
Go to **Storage → S3 Configuration**:
- Copy `Endpoint`
- Go to **Access Keys** → Create new key → Copy `Key ID` and `Secret`

---

## Step 2 — Deploy Backend

### Option A: Railway (Recommended)

1. Go to https://railway.app
2. New Project → Deploy from GitHub repo
3. Select `online-class-platform`
4. Set **Root Directory** to `backend`
5. Railway auto-detects `Procfile` and runs:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
6. Add all environment variables (see table below)
7. Copy your Railway URL → needed for Vercel

---

### Option B: Render (Free Alternative)

1. Go to https://render.com
2. New → **Web Service** → Connect GitHub
3. Select `online-class-platform`
4. Set:
   - **Root Directory:** `backend`
   - **Build Command:** `apt-get update && apt-get install -y ffmpeg && pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Python Version:** 3.11
5. Add all environment variables (see table below)
6. Click **Create Web Service**
7. Copy your Render URL → needed for Vercel

> ⚠️ Free tier on Render spins down after 15 minutes of inactivity.
> First request after sleep takes ~30 seconds. Upgrade to $7/mo for always-on.

---

### Option C: Google Cloud Run (Production Scale)

1. Install [gcloud CLI](https://cloud.google.com/sdk/docs/install)
2. From `backend/` folder:
   ```bash
   gcloud run deploy online-class-backend \
     --source . \
     --region asia-south1 \
     --allow-unauthenticated \
     --port 8080
   ```
3. Set environment variables in Cloud Run console
4. Free tier: 2M requests/month

---

## Step 3 — Deploy Frontend (Vercel)

### 3A. Import project
1. Go to https://vercel.com
2. New Project → Import from GitHub
3. Select `online-class-platform`
4. Set **Root Directory** to `frontend`
5. Framework: **Next.js** (auto-detected)

### 3B. Set frontend environment variables in Vercel

Go to **Settings → Environment Variables** → Add:

```env
NEXT_PUBLIC_API_URL=https://your-railway-backend-url.up.railway.app
```

That is the **only** environment variable the frontend needs.

⚠️ Never add `LIVEKIT_API_SECRET` or any backend secrets here.

### 3C. Deploy
Click **Deploy**. Vercel builds and deploys automatically.

Your frontend URL will be something like:
```
https://online-class-platform.vercel.app
```

---

## Step 4 — Update Backend CORS

After getting your Vercel URL, update the Railway `FRONTEND_URL` variable:

```env
FRONTEND_URL=https://online-class-platform.vercel.app
```

If you have multiple frontend URLs (e.g. preview + production):
```env
FRONTEND_URL=https://online-class-platform.vercel.app,https://your-custom-domain.com
```

Railway will redeploy automatically.

---

## Step 5 — First Login

1. Open your Vercel URL
2. Go to `/admin`
3. Login with your `ADMIN_DASHBOARD_PASSWORD`
4. **Immediately change** both default passcodes:
   - Teacher Access Passcode (default: `teacher123`)
   - Recordings Access Passcode (default: `recordings123`)
5. Verify in Supabase Table Editor → `platform_config` — the hash values will update

---

## All Environment Variables Reference

### Backend (Railway)

| Variable | Description | Required |
|----------|-------------|----------|
| `LIVEKIT_URL` | LiveKit Cloud WSS URL | ✅ |
| `LIVEKIT_API_KEY` | LiveKit API key | ✅ |
| `LIVEKIT_API_SECRET` | LiveKit API secret (server only) | ✅ |
| `FRONTEND_URL` | Vercel URL(s) comma-separated for CORS | ✅ |
| `SUPABASE_URL` | Supabase project URL | ✅ |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (secret) | ✅ |
| `SUPABASE_ANON_KEY` | Supabase anon/public key | ✅ |
| `SUPABASE_S3_ENDPOINT` | Supabase S3 endpoint URL | ✅ |
| `SUPABASE_S3_ACCESS_KEY` | Supabase S3 access key ID | ✅ |
| `SUPABASE_S3_SECRET_KEY` | Supabase S3 secret key | ✅ |
| `SUPABASE_S3_REGION` | Supabase S3 region | ✅ |
| `SUPABASE_S3_BUCKET` | Storage bucket name | ✅ |
| `RECORDING_PLAYBACK_SECRET` | Random secret for short-lived private HLS playback links | ✅ |
| `ADMIN_DASHBOARD_PASSWORD` | Admin dashboard login password | ✅ |
| `ENVIRONMENT` | Set to `production` | ✅ |

### Frontend (Vercel)

| Variable | Description | Required |
|----------|-------------|----------|
| `NEXT_PUBLIC_API_URL` | Backend Railway URL | ✅ |

---

## Security Checklist Before Going Live

- [ ] `LIVEKIT_API_SECRET` only in Railway — never in Vercel
- [ ] `ADMIN_DASHBOARD_PASSWORD` is strong (min 12 chars, mixed case + symbols)
- [ ] Default passcodes changed via `/admin` dashboard
- [ ] `ENVIRONMENT=production` set in Railway (disables /docs endpoint)
- [ ] `.env` and `.env.local` are in `.gitignore` (already done)
- [ ] No real secrets in GitHub commits (already verified)
- [ ] Supabase `platform_config` table created
- [ ] Supabase `class-recordings` bucket created

---

## Health Check

After deployment verify everything works:

```
GET https://your-backend.up.railway.app/api/health
```

Expected response:
```json
{
  "status": "ok",
  "environment": "production",
  "services": {
    "livekit": true,
    "storage": true,
    "database": true
  }
}
```

All three services should be `true`.

---

## Local Development (Quick Reference)

```bash
# Backend
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm run dev
```

Frontend: http://localhost:3000
Backend: http://localhost:8000
API Docs: http://localhost:8000/docs (dev only)

---

## What's NOT Included in This MVP

The following will be added in future versions:
- Database persistence for class sessions (currently in-memory)
- User authentication / login system
- Batch enrollment / passcodes
- Payment integration
- Admin analytics dashboard
- Mobile application
- AI agents / transcription
- Recording retention beyond 20 hours
