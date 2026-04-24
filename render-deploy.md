# Render Deployment Guide — IST AI Admissions Agent

## Prerequisites

| What | Where |
|------|-------|
| Groq API key(s) | [console.groq.com](https://console.groq.com) — free tier works |
| Render account | [render.com](https://render.com) — free tier sufficient |

---

## 1 · Deploy on Render (Blueprint — recommended)

1. Fork / push this repo to your GitHub account.
2. In Render Dashboard → **New → Blueprint**.
3. Connect the repository.
4. Render reads `render.yaml` automatically — hit **Apply**.
5. Set the required environment variable in the Render Dashboard:

| Variable | Required | Description |
|----------|----------|-------------|
| `GROQ_API_KEY` | ✅ Yes | Groq API key for Whisper STT + LLaMA LLM |
| `GROQ_API_KEYS` | Optional | Comma-separated keys for load-balancing (reduces 429 errors) |
| `IST_ADMIN_SECRET` | Optional | Secret for `POST /api/admin/reload-kb` |

6. Wait for the build to finish (~2 min). The service URL appears in the Dashboard.

---

## 2 · Manual Deploy (Web Service)

If you prefer manual setup instead of Blueprint:

1. Render Dashboard → **New → Web Service** → connect repo.
2. Fill in:
   - **Build Command**: `pip install --upgrade pip && pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gevent --workers 1 --timeout 180 --graceful-timeout 60`
3. Add environment variables (see table above).
4. Set **Health Check Path** to `/health`.

---

## 3 · Local Testing (Laptop)

```bash
# 1 — Clone and set up environment
git clone <your-repo-url>
cd pleaseagent
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2 — Copy and fill in your API key
cp .env.example .env
# Edit .env and set GROQ_API_KEY=gsk_...

# 3 — Run the server
python app.py
# or with gunicorn (same as Render):
gunicorn app:app --worker-class gevent --workers 1 --timeout 60 --bind 0.0.0.0:5000

# 4 — Open in browser
# http://localhost:5000
```

---

## 4 · Testing on Mobile (Android / iPhone)

Because browsers require HTTPS for microphone access, you cannot just open
`http://192.168.x.x:5000` on a phone.  Use one of these methods:

### Option A — ngrok (easiest)

```bash
# Install ngrok: https://ngrok.com/download
ngrok http 5000
# Copy the https://xxxx.ngrok.io URL and open on your phone
```

### Option B — mkcert + local HTTPS

```bash
# Install mkcert: https://github.com/FiloSottile/mkcert
mkcert -install
mkcert localhost 127.0.0.1 ::1
# Then run Flask with SSL:
python -c "from app import app; app.run(ssl_context=('localhost.pem','localhost-key.pem'), host='0.0.0.0', port=5000)"
```

---

## 5 · Updating the Knowledge Base

After editing `all_kb.txt` and pushing to Render:

```bash
curl -X POST https://your-service.onrender.com/api/admin/reload-kb \
     -H "X-Admin-Secret: <IST_ADMIN_SECRET value>"
```

Or run `ist_kb_sync.py` locally then commit the updated `all_kb.txt`.

---

## 6 · Health Check

```
GET /health
→ 200 { "status": "ok" }
```

Render pings this endpoint every ~30 s. If it returns non-200 the service
is restarted automatically.

---

## 7 · Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| "speech service not configured" | Missing `GROQ_API_KEY` | Add env var in Render |
| No audio plays on iPhone | iOS audio unlock not firing | Ensure user gesture → `startCall()` is a direct click handler |
| Microphone permission denied on Android Chrome | HTTP not HTTPS | Deploy to Render (HTTPS) or use ngrok locally |
| 429 Too Many Requests from Groq | Single API key rate limit | Add multiple keys as `GROQ_API_KEYS=key1,key2,key3` |
| Build fails: `gevent` not found | Wrong start command | Use start command from section 2 above |
