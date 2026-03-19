# SmartDocs AI

A RAG-based document assistant I built using **FastAPI**, **FAISS**, and **Google Gemini 2.5**. Upload PDFs, ask questions about them in natural language, and get answers grounded in the actual document content.

![SmartDocs AI Screenshot](docs/screenshot.png)
<!-- Add screenshot -->

---

## What it does

- Upload one or more PDFs — they get chunked, embedded, and stored in a local FAISS vector index
- Ask questions in natural language — the app retrieves the most relevant chunks and passes them as context to Gemini 2.5 Flash
- Full multi-turn conversation with chat history
- Filter questions to specific documents using the pill UI
- Responses are streamed with a typing effect and rendered as Markdown
- Duplicate detection via MD5 hash — re-uploading an unchanged file skips re-embedding
- The vector store persists to disk across server restarts

---

## How it works

The core is a RAG (Retrieval-Augmented Generation) pipeline:

```
PDF upload
  → pypdf extracts text
  → split into 1000-char chunks with 200-char overlap
  → each chunk embedded with Gemini Embedding 001 (3072-dim, RETRIEVAL_DOCUMENT task)
  → vectors stored in FAISS IndexFlatL2

Question asked
  → question embedded (RETRIEVAL_QUERY task — asymmetric for better retrieval)
  → FAISS L2 search, top 6 results, threshold ≤ 2.0
  → relevant chunks injected into prompt
  → Gemini 2.5 Flash generates answer with full chat history for context
```

I used asymmetric embeddings (separate task types for indexing vs. querying) which improves retrieval accuracy compared to using the same embedding for both.

---

## Tech stack

| Layer | Tech |
|---|---|
| Backend | Python, FastAPI, Uvicorn |
| LLM | Gemini 2.5 Flash (`gemini-2.5-flash`) |
| Embeddings | Gemini Embedding 001 (`models/gemini-embedding-001`) |
| Vector search | FAISS (`faiss-cpu`) |
| PDF parsing | pypdf |
| Frontend | Vanilla HTML/CSS/JS — single file, no framework |
| Markdown | marked.js + DOMPurify (XSS-safe rendering) |

---

## Project structure

```
smartdocs-ai/
├── backend/
│   ├── main.py          # All backend logic — routes, RAG pipeline, Gemini integration
│   ├── .env             # API keys (not committed)
│   └── .env.example     # Environment variable template
├── frontend/
│   └── index.html       # Full UI — HTML, CSS, and JS in one file
├── requirements.txt
├── Procfile
└── railway.toml
```

---

## Running locally

**Requirements:** Python 3.11+, a [Gemini API key](https://aistudio.google.com/apikey)

```bash
git clone https://github.com/YOUR_USERNAME/smartdocs-ai.git
cd smartdocs-ai

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp backend/.env.example backend/.env
# Add your GEMINI_API_KEY to backend/.env

cd backend
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | From [Google AI Studio](https://aistudio.google.com/apikey) |
| `API_KEY` | No | Enables auth on `/upload`, `/ask`, `/reset` endpoints |
| `MAX_UPLOAD_MB` | No | Upload size limit in MB (default: 20) |

---

## Deployed on Railway

The app is deployed at: _[add your Railway URL here]_

---

## Screenshots

| | |
|---|---|
| _coming soon_ | _coming soon_ |

---

## License

MIT
