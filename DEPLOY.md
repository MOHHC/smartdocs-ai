# Deployment Guide

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | From [Google AI Studio](https://aistudio.google.com/apikey) |
| `API_KEY` | No | Static key to protect `/upload`, `/ask`, `/reset`. Leave unset to disable auth. |
| `MAX_UPLOAD_MB` | No | Max PDF size in MB (default: `20`) |

---

## Railway (recommended)

1. Push the repo to GitHub (make sure `.env` is **not** committed).
2. Create a new project at [railway.app](https://railway.app) → **Deploy from GitHub repo**.
3. In **Variables**, add:
   - `GEMINI_API_KEY` = your key
   - `API_KEY` = a strong random string (optional but recommended)
4. Railway uses `railway.toml` automatically — no extra config needed.
5. After deploy, the app is live at your Railway domain. Open it to confirm.

---

## Local Development

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up environment
cp backend/.env.example backend/.env
# Edit backend/.env and add your GEMINI_API_KEY

# 4. Run the server
cd backend
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

---

## Notes

- Uploaded PDFs and the FAISS vector store are saved to `backend/uploads/` and `backend/store/`. These are excluded from git (`.gitignore`). On Railway, they live in ephemeral storage — files are lost on redeploy. For persistent storage, mount a Railway Volume.
- The frontend is served as a static file by FastAPI — no separate frontend deployment needed.
- The `OPENAI_API_KEY` in any old `.env` is not used by this project and can be removed.
