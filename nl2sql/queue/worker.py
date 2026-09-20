"""
Redis cache helpers for NL2SQL query result caching.
"""
from __future__ import annotations

import hashlib
import re

import redis as _redis

CACHE_TTL = 3600  # ponytail: fixed 1hr, make configurable if stale results become an issue
CACHE_KEY = "cache:sql:{hash}"


def get_redis(redis_url: str) -> _redis.Redis:
    return _redis.from_url(redis_url, decode_responses=True)


def _cache_hash(question: str, schema: str) -> str:
    """Semantic hash: normalize question, use only table/column names from schema."""
    q = re.sub(r"[^\w\s]", "", question.lower()).strip()
    identifiers = " ".join(re.findall(r"\b[a-zA-Z_]\w*\b", schema))
    return hashlib.sha256(f"{q}||{identifiers}".encode()).hexdigest()[:32]
