"""Embedding provider.

Primary: OmniRoute /v1/embeddings (EMBEDDING_MODEL). Fallback: `local-hash-v1`, a deterministic
signed feature-hashing embedding over word unigrams/bigrams and character trigrams. It is a lexical
embedding (no semantics beyond token overlap) but needs no model and no network. Every stored vector is
tagged with the model that produced it and searches only compare vectors from the same model.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

from app.core.config import get_settings
from app.integrations.omniroute.client import ModelClient, ModelError, get_model_client

logger = logging.getLogger("atlas.embeddings")

HASH_MODEL = "local-hash-v1"
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the and or of to in on for with is are was were be been it this that at by from as "
                  "i you we they he she me my our your please can could would should will do does did".split())


def _stem(tok: str) -> str:
    for suf in ("ing", "ed", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def hash_embed(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    toks = [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]
    feats: list[tuple[str, float]] = [(t, 1.0) for t in toks]
    feats += [(f"{a}_{b}", 0.7) for a, b in zip(toks, toks[1:], strict=False)]
    for t in toks:
        if len(t) >= 5:
            feats += [(f"#{t[i:i + 3]}", 0.25) for i in range(len(t) - 2)]
    for feat, weight in feats:
        h = hashlib.blake2b(feat.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class Embedder:
    def __init__(self, client: ModelClient | None = None):
        self.client = client or get_model_client()
        self.dim = get_settings().embedding_dimensions

    @property
    def model_name(self) -> str:
        return self.client.settings.embedding_model if self.client.embeddings_configured else HASH_MODEL

    async def embed(self, texts: list[str]) -> tuple[str, list[list[float]]]:
        if self.client.embeddings_configured:
            try:
                vecs = await self.client.embed(texts)
                if all(len(v) == self.dim for v in vecs):
                    return self.client.settings.embedding_model, vecs
                logger.warning("embedding_dimension_mismatch; using local-hash-v1")
            except (ModelError, Exception) as exc:  # noqa: BLE001 - never fail memory writes on embedding errors
                logger.warning(f"embedding_failed; using local-hash-v1: {exc}")
        return HASH_MODEL, [hash_embed(t, self.dim) for t in texts]

    async def embed_one(self, text: str) -> tuple[str, list[float]]:
        model, vecs = await self.embed([text])
        return model, vecs[0]
