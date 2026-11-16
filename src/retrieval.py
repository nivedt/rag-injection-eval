"""Minimal dense retrieval over the document corpus."""
import re
from collections import Counter

import numpy as np


class MiniLMEmbedder:
    """Local sentence-transformers model (downloads about 90 MB on first use)."""

    def __init__(self, name="sentence-transformers/all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(name)

    def embed(self, texts):
        return np.asarray(self.model.encode(list(texts), normalize_embeddings=True))


class TfidfEmbedder:
    """Dependency-free fallback, useful for offline smoke tests."""

    def __init__(self, corpus):
        tokenized = [self._tokens(t) for t in corpus]
        self.vocab = {w: i for i, w in enumerate(sorted({w for toks in tokenized for w in toks}))}
        df = Counter(w for toks in tokenized for w in set(toks))
        n = len(corpus)
        self.idf = np.zeros(len(self.vocab))
        for w, i in self.vocab.items():
            self.idf[i] = np.log((1 + n) / (1 + df[w])) + 1

    @staticmethod
    def _tokens(text):
        return re.findall(r"[a-z0-9]+", text.lower())

    def embed(self, texts):
        out = np.zeros((len(texts), len(self.vocab)))
        for row, text in enumerate(texts):
            for w, c in Counter(self._tokens(text)).items():
                if w in self.vocab:
                    out[row, self.vocab[w]] = c * self.idf[self.vocab[w]]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


class Retriever:
    def __init__(self, docs, embedder="minilm"):
        self.docs = dict(docs)
        self.ids = list(self.docs)
        texts = [self.docs[i] for i in self.ids]
        self.embedder = MiniLMEmbedder() if embedder == "minilm" else TfidfEmbedder(texts)
        self.matrix = self.embedder.embed(texts)
        self._query_cache = {}
        self._doc_cache = {}

    def _embed_query(self, question):
        if question not in self._query_cache:
            self._query_cache[question] = self.embedder.embed([question])[0]
        return self._query_cache[question]

    def _embed_doc(self, text):
        if text not in self._doc_cache:
            self._doc_cache[text] = self.embedder.embed([text])[0]
        return self._doc_cache[text]

    def top_k(self, question, k=3, override=None):
        """Return [(doc_id, text)] for the k most similar documents.

        override: optional (doc_id, new_text) that replaces one document,
        used to swap in the attacked version of the target document.
        """
        matrix, texts = self.matrix, self.docs
        if override:
            doc_id, new_text = override
            idx = self.ids.index(doc_id)
            matrix = matrix.copy()
            matrix[idx] = self._embed_doc(new_text)
            texts = {**self.docs, doc_id: new_text}
        scores = matrix @ self._embed_query(question)
        order = np.argsort(-scores)[:k]
        return [(self.ids[i], texts[self.ids[i]]) for i in order]
