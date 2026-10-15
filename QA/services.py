"""RAG pipeline: extract -> chunk -> embed -> retrieve -> generate (Gemini free tier)."""
import logging
import math
import re
import time

import requests
from django.conf import settings
from pypdf import PdfReader

from .models import Chunk

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta"
EMBED_DIM = 768

SYSTEM = (
    "You answer questions using only the provided document excerpts. "
    "If the answer is not in the excerpts, say you could not find it in the uploaded documents. "
    "Cite the excerpts you used like [1], [2]. Be concise."
)


class AIServiceError(Exception):
    pass


def _post(path, payload):
    """Call the Gemini API, retrying on rate limits."""
    if not settings.GEMINI_API_KEY:
        raise AIServiceError("The server has no GEMINI_API_KEY configured.")
    for attempt in range(4):
        try:
            r = requests.post(
                f"{API}/{path}", json=payload, timeout=60,
                headers={"x-goog-api-key": settings.GEMINI_API_KEY},
            )
        except requests.RequestException as exc:
            log.warning("Gemini request failed: %s", exc)
            raise AIServiceError("Could not reach the AI service. Try again.") from exc
        if r.status_code in (429, 503) and attempt < 3:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            raise AIServiceError("Free-tier rate limit reached. Wait a minute and try again.")
        if r.status_code == 503:
            raise AIServiceError("The AI service is busy right now. Try again in a few seconds.")
        if r.status_code != 200:
            log.error("Gemini %s: %s", r.status_code, r.text[:300])
            raise AIServiceError(f"AI service error ({r.status_code}). Check the API key and model names.")
        return r.json()
    raise AIServiceError("AI service unavailable.")


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed(texts, task):
    """Turn texts into vectors. task is RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY."""
    model = f"models/{settings.GEMINI_EMBED_MODEL}"
    out = []
    for i in range(0, len(texts), 50):
        batch = texts[i:i + 50]
        data = _post(f"{model}:batchEmbedContents", {"requests": [
            {"model": model, "content": {"parts": [{"text": t}]},
             "taskType": task, "outputDimensionality": EMBED_DIM}
            for t in batch
        ]})
        out += [_normalize(e["values"]) for e in data["embeddings"]]
    return out


def generate(prompt):
    """Ask the LLM to write an answer."""
    data = _post(f"models/{settings.GEMINI_CHAT_MODEL}:generateContent", {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    })
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        text = ""
    if not text:
        raise AIServiceError("The AI returned no answer (it may have been blocked). Try rephrasing.")
    return text


def extract_text(uploaded):
    if uploaded.name.lower().endswith(".pdf"):
        reader = PdfReader(uploaded)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return uploaded.read().decode("utf-8", errors="ignore")


def chunk_text(text, size=800, overlap=150):
    """Split text into ~800 character pieces with overlap, cutting at paragraph/sentence ends."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def retrieve(question, session_key, document_id=None, k=4):
    """Find the k chunks most similar to the question (cosine similarity)."""
    qs = Chunk.objects.filter(document__session_key=session_key).select_related("document")
    if document_id:
        qs = qs.filter(document_id=document_id)
    q = embed([question], "RETRIEVAL_QUERY")[0]
    scored = [(sum(a * b for a, b in zip(q, c.embedding)), c) for c in qs]
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]


def answer(question, hits):
    context = "\n\n".join(f"[{i}] ({c.document.title})\n{c.text}" for i, (_, c) in enumerate(hits, 1))
    return generate(f"Excerpts:\n{context}\n\nQuestion: {question}")


def summarize(document):
    return generate(
        "Summarize the following document in 5-8 clear bullet points.\n\n" + document.text[:40000]
    )