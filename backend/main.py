from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import os
import json
import logging
import asyncio
import hashlib
from pathlib import Path
from pypdf import PdfReader
import faiss
import numpy as np
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("smartdocs")

# ── Config ───────────────────────────────────────────────────
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
API_KEY             = os.getenv("API_KEY")           # optional — leave unset to disable auth
MAX_UPLOAD_MB       = int(os.getenv("MAX_UPLOAD_MB", 20))
EMBED_MODEL         = "models/gemini-embedding-001"
CHAT_MODEL          = "gemini-2.5-flash"
EMBEDDING_DIM       = 3072
RELEVANCE_THRESHOLD = 2.0
TOP_K               = 6
FRONTEND_DIR        = Path(__file__).parent.parent / "frontend"
PERSIST_DIR         = Path("store")
INDEX_PATH          = PERSIST_DIR / "index.faiss"
STORE_PATH          = PERSIST_DIR / "store.json"
MAX_CHAT_HISTORY    = 40   # max turns kept in memory (20 exchanges)

# ── Gemini client ─────────────────────────────────────────────
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = (
    "You are an expert AI assistant with deep analytical and reasoning abilities.\n\n"
    "## How to answer\n"
    "- Read the question carefully. Think through it step by step before writing.\n"
    "- Provide complete, accurate answers. Never truncate or give vague summaries.\n"
    "- If the question has multiple parts, answer every part.\n"
    "- If asked to summarise a document, produce a thorough structured summary with sections.\n"
    "- If asked to explain something, give a clear explanation with examples where helpful.\n"
    "- If the provided context does not contain the answer, say so briefly, then answer from your own knowledge.\n\n"
    "## Formatting\n"
    "- Use **bold** for key terms and important points.\n"
    "- Use ## or ### headers to break up long answers.\n"
    "- Use bullet points or numbered lists to present multiple items clearly.\n"
    "- Use fenced code blocks (```language) for all code.\n"
    "- Use tables when comparing multiple items.\n"
    "- Keep paragraphs short (3-4 sentences max)."
)

# ── App ──────────────────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ─────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(key: str = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")

# ── Vector store ─────────────────────────────────────────────
index          = faiss.IndexFlatL2(EMBEDDING_DIM)
stored_chunks:  list[str]  = []
chunk_metadata: list[dict] = []
uploaded_docs:  list[str]  = []   # filenames
doc_hashes:     dict[str, str] = {}  # filename → md5 hash

PERSIST_DIR.mkdir(exist_ok=True)
Path("uploads").mkdir(exist_ok=True)


def load_store() -> None:
    global index, stored_chunks, chunk_metadata, uploaded_docs, doc_hashes
    if not (INDEX_PATH.exists() and STORE_PATH.exists()):
        return
    with open(STORE_PATH) as f:
        data = json.load(f)
    if data.get("dimension") != EMBEDDING_DIM:
        log.warning(
            "Embedding dimension changed (%s → %s). Resetting store — please re-upload your PDFs.",
            data.get("dimension"), EMBEDDING_DIM,
        )
        return
    index          = faiss.read_index(str(INDEX_PATH))
    stored_chunks  = data["chunks"]
    chunk_metadata = data["metadata"]
    uploaded_docs  = data["docs"]
    doc_hashes     = data.get("hashes", {})


def save_store() -> None:
    faiss.write_index(index, str(INDEX_PATH))
    with open(STORE_PATH, "w") as f:
        json.dump({
            "dimension": EMBEDDING_DIM,
            "chunks":    stored_chunks,
            "metadata":  chunk_metadata,
            "docs":      uploaded_docs,
            "hashes":    doc_hashes,
        }, f)


load_store()

# ── Chat history ──────────────────────────────────────────────
chat_history: list[types.Content] = []

# ── Helpers ──────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size])
        start += chunk_size - overlap
    return chunks


def generate_embedding(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> np.ndarray:
    """
    Uses Gemini text-embedding-004.
    task_type="RETRIEVAL_DOCUMENT" for indexing, "RETRIEVAL_QUERY" for searching.
    Asymmetric embeddings improve RAG retrieval quality.
    """
    result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return np.array(result.embeddings[0].values, dtype="float32")


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    document_filter: list[str] | None = Field(default=None, max_length=50)


def build_prompt(question: str, chunks: list[dict]) -> str:
    if not chunks:
        return question  # no documents — Gemini answers from its own knowledge

    context_parts = [
        f"[Source: {c['filename']} | chunk {c['chunk_index']}]\n{c['text']}"
        for c in chunks
    ]
    context = "\n\n---\n\n".join(context_parts)
    return (
        f"Context:\n{context}\n\n"
        f"Question:\n{question}\n\n"
        "Instructions:\n"
        "Answer ONLY based on the context provided above. "
        "If the answer is not in the context, say you don't know."
    )


def call_gemini(prompt: str) -> str:
    global chat_history
    chat_history.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    # Trim history to prevent unbounded growth (keep system message + last N turns)
    if len(chat_history) > MAX_CHAT_HISTORY:
        chat_history = chat_history[-MAX_CHAT_HISTORY:]
    try:
        response = client.models.generate_content(
            model=CHAT_MODEL,
            contents=chat_history,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.4,
                top_p=0.95,
                max_output_tokens=8192,
            ),
        )
        answer = response.text
        chat_history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        return answer
    except Exception as e:
        log.error("Gemini API error: %s", e)
        raise HTTPException(status_code=502, detail=f"Gemini API error: {str(e)}")

# ── Routes ───────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "vectors": index.ntotal, "docs": len(uploaded_docs)}


@app.get("/documents")
def list_documents():
    return {"documents": uploaded_docs, "total_chunks": index.ntotal}


@app.post("/reset", dependencies=[Depends(verify_api_key)])
def reset_chat():
    global chat_history
    chat_history = []
    return {"message": "Chat history cleared."}


@app.delete("/documents/{filename}", dependencies=[Depends(verify_api_key)])
def delete_document(filename: str):
    global index, stored_chunks, chunk_metadata, uploaded_docs, doc_hashes

    if filename not in uploaded_docs:
        raise HTTPException(status_code=404, detail="Document not found.")

    # Find indices to keep (chunks NOT belonging to this file)
    keep = [i for i, m in enumerate(chunk_metadata) if m["filename"] != filename]

    if keep:
        kept_vectors = np.array([index.reconstruct(i) for i in keep], dtype="float32")
        new_index = faiss.IndexFlatL2(EMBEDDING_DIM)
        new_index.add(kept_vectors)
    else:
        new_index = faiss.IndexFlatL2(EMBEDDING_DIM)

    index          = new_index
    stored_chunks  = [stored_chunks[i]  for i in keep]
    chunk_metadata = [chunk_metadata[i] for i in keep]
    uploaded_docs.remove(filename)
    doc_hashes.pop(filename, None)

    # Remove the uploaded file from disk
    file_path = Path("uploads") / filename
    if file_path.exists():
        file_path.unlink()

    save_store()
    return {"deleted": filename, "remaining_docs": uploaded_docs}


@app.post("/upload", dependencies=[Depends(verify_api_key)])
async def upload_file(file: UploadFile = File(...)):
    global index, stored_chunks, chunk_metadata, uploaded_docs, doc_hashes
    try:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="Only PDF files are supported.")

        contents = await file.read()

        if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.")

        # Detect duplicate by content hash — skip re-embedding if unchanged
        file_hash = hashlib.md5(contents).hexdigest()
        if file.filename in uploaded_docs and doc_hashes.get(file.filename) == file_hash:
            existing_chunks = sum(1 for m in chunk_metadata if m["filename"] == file.filename)
            return {
                "filename":              file.filename,
                "chunks_stored":         existing_chunks,
                "vector_dimension":      EMBEDDING_DIM,
                "total_vectors_in_index": index.ntotal,
                "note":                  "Document unchanged — using existing embeddings.",
            }

        # If same filename but different content, remove old version first
        if file.filename in uploaded_docs:
            keep = [i for i, m in enumerate(chunk_metadata) if m["filename"] != file.filename]
            if keep:
                kept_vectors = np.array([index.reconstruct(i) for i in keep], dtype="float32")
                new_index = faiss.IndexFlatL2(EMBEDDING_DIM)
                new_index.add(kept_vectors)
            else:
                new_index = faiss.IndexFlatL2(EMBEDDING_DIM)
            index          = new_index
            stored_chunks  = [stored_chunks[i]  for i in keep]
            chunk_metadata = [chunk_metadata[i] for i in keep]
            uploaded_docs.remove(file.filename)

        file_path = Path("uploads") / file.filename
        file_path.write_bytes(contents)

        try:
            reader = PdfReader(file_path)
        except Exception:
            raise HTTPException(status_code=422, detail="The PDF appears to be empty or unreadable.")

        if not reader.pages:
            raise HTTPException(status_code=422, detail="The PDF appears to be empty or unreadable.")

        try:
            text = "".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            raise HTTPException(status_code=422, detail="Failed to extract text from this PDF.")

        if not text.strip():
            raise HTTPException(status_code=422, detail="No text could be extracted from this PDF. It may be a scanned image — try an OCR-processed version.")

        chunks = chunk_text(text)
        embeddings = []

        for i, chunk in enumerate(chunks):
            emb = await asyncio.to_thread(generate_embedding, chunk, "RETRIEVAL_DOCUMENT")
            embeddings.append(emb)
            stored_chunks.append(chunk)
            chunk_metadata.append({"filename": file.filename, "chunk_index": i})

        index.add(np.array(embeddings, dtype="float32"))
        uploaded_docs.append(file.filename)
        doc_hashes[file.filename] = file_hash

        save_store()
        log.info("Uploaded '%s': %d chunks, %d total vectors", file.filename, len(chunks), index.ntotal)

        return {
            "filename":              file.filename,
            "chunks_stored":         len(chunks),
            "vector_dimension":      EMBEDDING_DIM,
            "total_vectors_in_index": index.ntotal,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("Upload error for '%s': %s", file.filename, e, exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred during upload.")


@app.post("/ask", dependencies=[Depends(verify_api_key)])
async def ask_question(request: QuestionRequest):
    context_chunks = []

    if index.ntotal > 0:
        query_vector = np.array(
            [await asyncio.to_thread(generate_embedding, request.question, "RETRIEVAL_QUERY")],
            dtype="float32",
        )
        top_k = min(TOP_K, index.ntotal)
        distances, indices = index.search(query_vector, top_k)

        allowed = set(request.document_filter) if request.document_filter else None

        context_chunks = [
            {
                "chunk_index":    int(idx),
                "filename":       chunk_metadata[int(idx)]["filename"],
                "relevance_score": float(distances[0][rank]),
                "text":           stored_chunks[int(idx)],
            }
            for rank, idx in enumerate(indices[0])
            if idx != -1
            and float(distances[0][rank]) <= RELEVANCE_THRESHOLD
            and (allowed is None or chunk_metadata[int(idx)]["filename"] in allowed)
        ]

        if not context_chunks:
            log.info("No chunks met relevance threshold (%.1f) for query: %.80r", RELEVANCE_THRESHOLD, request.question)

    answer = await asyncio.to_thread(call_gemini, build_prompt(request.question, context_chunks))

    return {
        "question":      request.question,
        "answer":        answer,
        "context_chunks": context_chunks,
    }
