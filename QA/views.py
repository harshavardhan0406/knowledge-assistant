import json

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from . import services
from .models import Chunk, Document

ALLOWED_EXT = (".pdf", ".txt", ".md")


def _err(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _session_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _json(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


@ensure_csrf_cookie
def index(request):
    return render(request, "QA/index.html")


def health(request):
    return JsonResponse({"status": "ok", "llm_configured": bool(settings.GEMINI_API_KEY)})


@require_http_methods(["GET", "POST"])
def documents(request):
    sk = _session_key(request)
    if request.method == "GET":
        docs = Document.objects.filter(session_key=sk).annotate(n=Count("chunks"))
        return JsonResponse({"documents": [{"id": d.id, "title": d.title, "chunks": d.n} for d in docs]})

    f = request.FILES.get("file")
    if not f:
        return _err("Choose a file to upload.")
    if not f.name.lower().endswith(ALLOWED_EXT):
        return _err("Only PDF, TXT or MD files are supported.")
    if f.size > settings.MAX_UPLOAD_BYTES:
        return _err("File is larger than 5 MB.")
    if Document.objects.filter(session_key=sk).count() >= settings.MAX_DOCS_PER_SESSION:
        return _err(f"Demo limit: {settings.MAX_DOCS_PER_SESSION} documents. Delete one first.")

    try:
        text = services.extract_text(f)
    except Exception:
        return _err("Could not read this file.")
    chunks = services.chunk_text(text)
    if not chunks:
        return _err("No readable text found (scanned PDFs need OCR).")
    if len(chunks) > settings.MAX_CHUNKS_PER_DOC:
        return _err("Document is too long for this demo. Try a smaller one.")

    try:
        vectors = services.embed(chunks, "RETRIEVAL_DOCUMENT")
    except services.AIServiceError as exc:
        return _err(str(exc), 502)

    with transaction.atomic():
        doc = Document.objects.create(session_key=sk, title=f.name[:255], text=text)
        Chunk.objects.bulk_create(
            Chunk(document=doc, position=i, text=t, embedding=v)
            for i, (t, v) in enumerate(zip(chunks, vectors))
        )
    return JsonResponse({"id": doc.id, "title": doc.title, "chunks": len(chunks)}, status=201)


@require_http_methods(["DELETE"])
def document_detail(request, pk):
    sk = _session_key(request)
    deleted, _ = Document.objects.filter(pk=pk, session_key=sk).delete()
    return HttpResponse(status=204) if deleted else _err("Not found.", 404)


@require_http_methods(["POST"])
def ask(request):
    sk = _session_key(request)
    data = _json(request)
    question = str(data.get("question", "")).strip()
    if not question:
        return _err("Type a question.")
    if len(question) > 1000:
        return _err("Question is too long (max 1000 characters).")
    try:
        doc_id = int(data["document_id"]) if data.get("document_id") else None
    except (TypeError, ValueError):
        return _err("Invalid document.")
    if not Document.objects.filter(session_key=sk).exists():
        return _err("Upload a document first.")
    if doc_id and not Document.objects.filter(pk=doc_id, session_key=sk).exists():
        return _err("Document not found.", 404)
    try:
        hits = services.retrieve(question, sk, doc_id)
        reply = services.answer(question, hits)
    except services.AIServiceError as exc:
        return _err(str(exc), 502)
    return JsonResponse({
        "answer": reply,
        "sources": [{"document": c.document.title, "text": c.text[:300], "score": round(s, 3)} for s, c in hits],
    })


@require_http_methods(["POST"])
def summarize_view(request):
    sk = _session_key(request)
    try:
        doc = Document.objects.get(pk=int(_json(request).get("document_id")), session_key=sk)
    except (Document.DoesNotExist, TypeError, ValueError):
        return _err("Pick a document to summarize.", 404)
    try:
        return JsonResponse({"summary": services.summarize(doc)})
    except services.AIServiceError as exc:
        return _err(str(exc), 502)