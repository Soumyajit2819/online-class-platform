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

## Step 2 — Deploy Backend (Railway)

### 2A. Push to GitHub (if not done)
```bash
git remote add origin https://github.com/YOUR_USERNAME/online-class-platform.git
git push -u origin main
```

### 2B. Create Railway project
1. Go to https://railway.app
2. New Project → Deploy from GitHub repo
3. Select `online-class-platform`
4. Set **Root Directory** to `backend`
5. Railway auto-detects `Procfile` and runs:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

### 2C. Set backend environment variables in Railway

Go to your Railway service → **Variables** → Add all of these:

```env
LIVEKIT_URL=wss://onlineclassplatform-v7ztr6qb.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_ANON_KEY=your_anon_key

SUPABASE_S3_ENDPOINT=https://your-project.supabase.co/storage/v1/s3
SUPABASE_S3_ACCESS_KEY=your_s3_access_key
SUPABASE_S3_SECRET_KEY=your_s3_secret_key
SUPABASE_S3_REGION=ap-northeast-2
SUPABASE_S3_BUCKET=class-recordings

FRONTEND_URL=https://your-app.vercel.app
ADMIN_DASHBOARD_PASSWORD=YourStrongAdminPassword
ENVIRONMENT=production
```

### 2D. Get your backend URL
After deployment Railway gives you a URL like:
```
https://online-class-platform-production.up.railway.app
```
**Save this — you need it for the frontend.**

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
