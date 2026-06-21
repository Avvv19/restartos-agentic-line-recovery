"""
restartos.rag
============
Real retrieval over OEM manuals — markdown AND arbitrary PDFs. Default is a
pure-Python BM25 index (zero deps, fully offline, deterministic). If embeddings
are available it adds a dense lane and fuses scores:

  * sentence-transformers (local)  -> used if installed
  * OpenAI embeddings (text-embedding-3-small) -> used if OPENAI_API_KEY set

The retriever powers two things:
  1. ManualAgent grounding on natural-language alarms over real docs.
  2. The verifier's groundedness check on free-form citations: a claimed passage
     must retrieve with score above a floor or it is treated as unresolved.
"""
from __future__ import annotations

import glob
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DocChunk:
    doc_id: str
    section: str
    page: Optional[int]
    text: str

    @property
    def citation(self) -> str:
        p = f"@p.{self.page}" if self.page else ""
        return f"MANUAL:{self.doc_id}#{self.section}{p}"


_WORD = re.compile(r"[a-z0-9]+")


def _tok(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# --------------------------------------------------------------------------- #
# Loaders                                                                      #
# --------------------------------------------------------------------------- #
def load_markdown(path: str) -> list[DocChunk]:
    doc_id = os.path.basename(path)
    text = open(path, encoding="utf-8", errors="ignore").read()
    chunks = []
    # split on "## §X  (p.N)" headers, keep section + page
    parts = re.split(r"\n## ", text)
    for part in parts:
        m = re.match(r"§?([\w.]+)\s*(?:\(p\.(\d+)\))?\s*\n(.*)", part, re.S)
        if m:
            sec, page, body = m.group(1), m.group(2), m.group(3).strip()
            if body:
                chunks.append(DocChunk(doc_id, sec, int(page) if page else None, body))
    return chunks


def load_pdf(path: str) -> list[DocChunk]:
    try:
        from pypdf import PdfReader
    except Exception:
        return []
    doc_id = os.path.basename(path)
    out = []
    try:
        reader = PdfReader(path)
        for i, page in enumerate(reader.pages, 1):
            txt = (page.extract_text() or "").strip()
            if txt:
                sec = re.search(r"§\s*([\w.]+)", txt)
                out.append(DocChunk(doc_id, sec.group(1) if sec else f"page{i}", i, txt))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# BM25 (pure python)                                                           #
# --------------------------------------------------------------------------- #
class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.chunks: list[DocChunk] = []
        self.docs: list[list[str]] = []
        self.df: Counter = Counter()
        self.avgdl = 0.0
        self.N = 0

    def add(self, chunks: list[DocChunk]) -> None:
        for c in chunks:
            toks = _tok(c.text)
            self.chunks.append(c)
            self.docs.append(toks)
            for t in set(toks):
                self.df[t] += 1
        self.N = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / self.N if self.N else 0.0

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def search(self, query: str, k: int = 3) -> list[tuple[DocChunk, float]]:
        q = _tok(query)
        scored = []
        for i, doc in enumerate(self.docs):
            tf = Counter(doc)
            dl = len(doc)
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = self._idf(term)
                num = tf[term] * (self.k1 + 1)
                den = tf[term] + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * num / den
            if s > 0:
                scored.append((self.chunks[i], round(s, 4)))
        return sorted(scored, key=lambda x: -x[1])[:k]


# --------------------------------------------------------------------------- #
# Optional dense lane                                                          #
# --------------------------------------------------------------------------- #
class _Embedder:
    def __init__(self) -> None:
        self.mode = None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._m = SentenceTransformer("all-MiniLM-L6-v2")
            self.mode = "sentence-transformers"
        except Exception:
            if os.getenv("OPENAI_API_KEY"):
                self.mode = "openai"

    def available(self) -> bool:
        return self.mode is not None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.mode == "sentence-transformers":
            return [list(map(float, v)) for v in self._m.encode(texts)]
        if self.mode == "openai":
            from openai import OpenAI
            r = OpenAI().embeddings.create(model="text-embedding-3-small", input=texts)
            return [d.embedding for d in r.data]
        return []


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# Qdrant semantic lane — persistent vector store                               #
# --------------------------------------------------------------------------- #
class QdrantSemanticIndex:
    """Persistent dense lane backed by a running Qdrant container.

    Activated when QDRANT_URL is set (default http://localhost:6333) AND the
    embedder is available. Vectors persist across runs so we only pay the
    embedding cost on first ingest of each manual chunk.
    """

    COLLECTION = "restartos_manuals"

    def __init__(self, embedder, url: Optional[str] = None) -> None:
        self.mode = None
        self.client = None
        self.embedder = embedder
        self.url = url or os.getenv("QDRANT_URL", "http://localhost:6333")
        if not embedder or not embedder.available():
            return
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as qm
            self._qm = qm
            self.client = QdrantClient(url=self.url, timeout=5.0)
            self.client.get_collections()  # ping
            self.mode = "qdrant"
        except Exception:
            self.client = None
            self.mode = None

    def available(self) -> bool:
        return self.client is not None

    def _vector_size(self) -> int:
        # Probe by embedding a tiny token
        v = self.embedder.embed(["probe"])[0]
        return len(v)

    def ingest(self, chunks: list[DocChunk]) -> int:
        if not self.available() or not chunks:
            return 0
        qm = self._qm
        size = self._vector_size()
        existing = {c.name for c in self.client.get_collections().collections}
        if self.COLLECTION not in existing:
            self.client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=qm.VectorParams(size=size, distance=qm.Distance.COSINE),
            )
        # Idempotent ingest: id = stable hash of (doc_id, section, page, prefix)
        import hashlib
        def _id(c: DocChunk) -> int:
            h = hashlib.sha1(f"{c.doc_id}|{c.section}|{c.page}|{c.text[:64]}".encode()).hexdigest()
            return int(h[:15], 16)  # fits in 64-bit positive int

        ids = [_id(c) for c in chunks]
        # Skip chunks already present (cheap upsert by id is fine, but skip embedding work)
        present = set()
        try:
            res = self.client.retrieve(collection_name=self.COLLECTION, ids=ids, with_payload=False, with_vectors=False)
            present = {r.id for r in res}
        except Exception:
            present = set()
        to_embed = [(i, c) for i, c in zip(ids, chunks) if i not in present]
        if not to_embed:
            return 0
        vectors = self.embedder.embed([c.text for _, c in to_embed])
        points = [qm.PointStruct(
            id=i,
            vector=v,
            payload={"doc_id": c.doc_id, "section": c.section,
                     "page": c.page, "text": c.text, "citation": c.citation},
        ) for (i, c), v in zip(to_embed, vectors)]
        self.client.upsert(collection_name=self.COLLECTION, points=points)
        return len(points)

    def search(self, query: str, k: int = 5) -> list[tuple[DocChunk, float]]:
        if not self.available():
            return []
        qv = self.embedder.embed([query])[0]
        hits = self.client.query_points(
            collection_name=self.COLLECTION, query=qv, limit=k, with_payload=True
        ).points
        out = []
        for h in hits:
            p = h.payload or {}
            c = DocChunk(doc_id=p.get("doc_id", "?"), section=p.get("section", "?"),
                         page=p.get("page"), text=p.get("text", ""))
            out.append((c, float(h.score)))
        return out


# --------------------------------------------------------------------------- #
# The RAG facade                                                               #
# --------------------------------------------------------------------------- #
class ManualRAG:
    def __init__(self, manuals_dir: str, use_embeddings: bool = True) -> None:
        self.bm25 = BM25Index()
        self.emb = _Embedder() if use_embeddings else None
        self._vecs: list[list[float]] = []
        self.qdrant: Optional[QdrantSemanticIndex] = None
        chunks: list[DocChunk] = []
        for fp in sorted(glob.glob(os.path.join(manuals_dir, "*.md"))):
            chunks += load_markdown(fp)
        for fp in sorted(glob.glob(os.path.join(manuals_dir, "*.pdf"))):
            chunks += load_pdf(fp)
        self.bm25.add(chunks)
        self.chunks = chunks
        # Try Qdrant first; fall back to in-memory cosine if unavailable.
        if self.emb and self.emb.available() and chunks:
            q = QdrantSemanticIndex(self.emb)
            if q.available():
                q.ingest(chunks)
                self.qdrant = q
            else:
                self._vecs = self.emb.embed([c.text for c in chunks])

    def stats(self) -> dict:
        return {"chunks": len(self.chunks),
                "docs": sorted({c.doc_id for c in self.chunks}),
                "dense_lane": (self.qdrant.mode if self.qdrant else
                               (self.emb.mode if (self.emb and self.emb.available()) else None))}

    def search(self, query: str, k: int = 3) -> list[dict]:
        bm = self.bm25.search(query, k=max(k, 5))
        # Key by stable citation (Qdrant chunks are reconstructed, so id() differs).
        def _key(c: DocChunk) -> str:
            return f"{c.doc_id}#{c.section}@{c.page}"

        fused: dict[str, list] = {}
        for c, s in bm:
            fused[_key(c)] = [c, s, 0.0]
        # Dense lane: Qdrant if available, else in-memory vectors
        dense: list[tuple[DocChunk, float]] = []
        if self.qdrant and self.qdrant.available():
            dense = self.qdrant.search(query, k=max(k, 5))
        elif self._vecs and self.emb:
            qv = self.emb.embed([query])[0]
            dense = sorted(((self.chunks[i], _cos(qv, v)) for i, v in enumerate(self._vecs)),
                           key=lambda x: -x[1])[:max(k, 5)]
        for c, s in dense:
            entry = fused.setdefault(_key(c), [c, 0.0, 0.0])
            entry[2] = max(entry[2], s)
        # normalize + fuse
        max_b = max((v[1] for v in fused.values()), default=1.0) or 1.0
        ranked = sorted(fused.values(),
                        key=lambda v: -(0.6 * v[1] / max_b + 0.4 * v[2]))[:k]
        return [{"citation": c.citation, "section": c.section, "page": c.page,
                 "score": round(0.6 * b / max_b + 0.4 * d, 4),
                 "excerpt": c.text[:200]} for c, b, d in ranked]

    def grounds(self, query: str, floor: float = 0.15) -> bool:
        """Does this claim retrieve a supporting passage above the floor?"""
        hits = self.search(query, k=1)
        return bool(hits) and hits[0]["score"] >= floor
