#!/usr/bin/env python3
"""
ingest_docs.py — Ingestion des PDFs de cours dans la knowledge base RAG.

Usage :
    /opt/openclaw/venv/bin/python /opt/openclaw/scripts/ingest_docs.py

Lit tous les PDFs de /opt/openclaw/docs/
→ découpe en chunks de 500 mots (overlap 50)
→ sauvegarde dans /opt/openclaw/memory/knowledge_base.json
"""

import json, re
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    raise SystemExit("pdfplumber manquant — installez-le : pip install pdfplumber")

DOCS_DIR   = Path("/opt/openclaw/docs")
KB_FILE    = Path("/opt/openclaw/memory/knowledge_base.json")
CHUNK_SIZE = 500   # mots
OVERLAP    = 50    # mots


def extract_text(pdf_path: Path) -> str:
    """Extrait tout le texte d'un PDF."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


def make_chunks(text: str, source: str) -> list[dict]:
    """Découpe le texte en chunks de CHUNK_SIZE mots avec overlap."""
    words = text.split()
    chunks = []
    start = 0
    idx   = 0
    while start < len(words):
        end   = start + CHUNK_SIZE
        chunk = " ".join(words[start:end])
        chunks.append({
            "id":      f"{source}_{idx}",
            "source":  source,
            "content": chunk,
            "words":   min(CHUNK_SIZE, len(words) - start),
        })
        idx   += 1
        start  = end - OVERLAP   # overlap
    return chunks


def build_keyword_index(chunks: list[dict]) -> dict[str, list[str]]:
    """Index mot → liste de chunk ids (pour recherche rapide)."""
    index: dict[str, list[str]] = {}
    for chunk in chunks:
        words = set(re.findall(r'\b\w{3,}\b', chunk["content"].lower()))
        for w in words:
            index.setdefault(w, []).append(chunk["id"])
    return index


def main():
    pdfs = sorted(DOCS_DIR.rglob("*.pdf"))
    if not pdfs:
        print(f"Aucun PDF trouvé dans {DOCS_DIR} (ni ses sous-dossiers)")
        return

    all_chunks: list[dict] = []
    for pdf in pdfs:
        print(f"  Lecture : {pdf.name}")
        try:
            text = extract_text(pdf)
        except Exception as e:
            print(f"    ✗ Erreur lecture : {e}")
            continue
        chunks = make_chunks(text, pdf.stem)
        print(f"    → {len(chunks)} chunks")
        all_chunks.extend(chunks)

    index = build_keyword_index(all_chunks)

    kb = {
        "chunks":  all_chunks,
        "index":   index,
        "sources": [p.stem for p in pdfs],
        "total":   len(all_chunks),
    }
    KB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KB_FILE, "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Knowledge base sauvegardée : {len(all_chunks)} chunks depuis {len(pdfs)} PDF(s)")
    print(f"   → {KB_FILE}")


if __name__ == "__main__":
    main()
