# Deployment Guide — Nexora

## Option 1: Render (Recommended — Free, Always-On)

### Backend
1. Go to https://render.com — sign up free (no credit card)
2. Click "New" → "Web Service"
3. Connect your GitHub repo (Nexora-backend)
4. Render will read `render.yaml` automatically
5. Click "Create Web Service"
6. It builds, runs migrations, and starts the server
7. Your backend URL: `https://nexora-backend.onrender.com`

### Frontend
1. Go to https://vercel.com — sign up free
2. Import your Nexora-Frontend repo
3. Set environment variable: `VITE_API_BASE_URL=https://nexora-backend.onrender.com`
4. Deploy
5. Your frontend URL: `https://nexora-frontend.vercel.app`

## Option 2: Docker + Any VPS

```bash
# Build and run
docker build -t nexora-backend .
docker run -p 8000:8000 -e DATABASE_URL=sqlite+aiosqlite:///./database.db nexora-backend
```

## UptimeRobot (Keep Server Warm)

If you use a serverless option (not recommended for Playwright), add a free UptimeRobot monitor:

1. Go to https://uptimerobot.com — free account
2. Add Monitor → "Cron Job"
3. Set:
   - Type: `Cron`
   - Cron: `*/5 * * * *`
   - Command: `curl -s https://your-backend.onrender.com/health`
4. Free tier covers 50 monitors

Render and Railway are always-on — no UptimeRobot needed for them.

## Environment Variables

Copy `.env.example` to `.env` and fill in:
- `DATABASE_URL` — SQLite or PostgreSQL
- `REDIS_URL` — leave empty for stateless mode
- `JWT_SECRET_KEY` — generate with `openssl rand -base64 32`
- `CORS_ORIGINS` — your frontend URL

## Health Check

After deployment, verify:
```
curl https://your-backend.onrender.com/health
```
Expected: `{"status":"ok"}`

## Playwright on Render

Render's free tier supports Playwright. The `render.yaml` build command installs Chromium with system deps. If you see "browser not found" errors, check the build logs — sometimes you need to add more system packages.