#!/usr/bin/env python3
"""
ingest_docs.py — Ingestion des PDFs et TXT de cours dans la knowledge base RAG.

Usage :
    /opt/openclaw/venv/bin/python /opt/openclaw/scripts/ingest_docs.py

Lit tous les PDFs et TXT de /opt/openclaw/docs/
→ découpe en chunks de 500 mots (overlap 50)
→ sauvegarde dans /opt/openclaw/memory/knowledge_base.json
"""

import json
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    raise SystemExit("pdfplumber manquant — installez-le : pip install pdfplumber")

DOCS_DIR   = Path("/opt/openclaw/docs")
KB_FILE    = Path("/opt/openclaw/memory/knowledge_base.json")
CHUNK_SIZE = 500   # mots
OVERLAP    = 50    # mots


def extract_text(file_path: Path) -> str:
    """Extrait tout le texte d'un PDF ou TXT."""
    if file_path.suffix.lower() == ".pdf":
        try:
            text_parts = []
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n".join(text_parts)
        except Exception:
            pass  # PDF invalide — on tente en texte brut ci-dessous
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return file_path.read_text(encoding=enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return ""


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



def load_existing_sources() -> set[str]:
    """Retourne les noms de sources déjà présentes dans la KB."""
    if not KB_FILE.exists():
        return set()
    try:
        with open(KB_FILE, encoding="utf-8") as f:
            kb = json.load(f)
        if isinstance(kb, list):
            return {c["source"] for c in kb}
    except Exception:
        pass
    return set()


def main():
    docs = sorted(
        f for f in DOCS_DIR.rglob("*")
        if f.suffix.lower() in (".pdf", ".txt")
    )
    if not docs:
        print(f"Aucun PDF/TXT trouvé dans {DOCS_DIR} (ni ses sous-dossiers)")
        return

    existing_sources = load_existing_sources()

    # Charger les chunks déjà en base
    existing_chunks: list[dict] = []
    if KB_FILE.exists():
        try:
            with open(KB_FILE, encoding="utf-8") as f:
                existing_chunks = json.load(f)
            if not isinstance(existing_chunks, list):
                existing_chunks = []
        except Exception:
            existing_chunks = []

    new_chunks: list[dict] = []
    skipped = 0
    for doc in docs:
        source = doc.stem
        if source in existing_sources:
            skipped += 1
            continue
        print(f"  Nouveau : {doc.name}")
        try:
            text = extract_text(doc)
        except Exception as e:
            print(f"    ✗ Erreur lecture : {e}")
            continue
        if not text.strip():
            print(f"    ✗ Aucun texte extrait")
            continue
        chunks = make_chunks(text, source)
        print(f"    → {len(chunks)} chunks")
        new_chunks.extend(chunks)

    if skipped:
        print(f"  (ignorés — déjà ingérés : {skipped} fichier(s))")

    if not new_chunks:
        print(f"\n✅ Rien de nouveau à ingérer ({len(existing_chunks)} chunks existants).")
        return

    all_chunks = existing_chunks + new_chunks

    KB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KB_FILE, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {len(new_chunks)} nouveaux chunks ajoutés — total : {len(all_chunks)} chunks")
    print(f"   → {KB_FILE}")


if __name__ == "__main__":
    main()
