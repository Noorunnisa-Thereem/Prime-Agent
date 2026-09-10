"""Long-term memory for external_lookup (Box 04: Context & memory).

This is a batch pipeline, not a conversational agent -- there is no
conversation history to retain, no vector store of documents to retrieve
similar passages from, and no knowledge graph of entities/relations to
traverse. The only kind of memory this pipeline needs is long-term: "have
we already looked this exact (source, term) up recently, and if so, what
did we find and when?" That is what this module provides.

Storage is a single JSON file (``reports/external_lookup/memory/
memory_store.json``), keyed by ``"<source>::<normalized term>"``. Each
entry holds the finding's full result envelope plus ``stored_at`` (when
this pipeline captured it) and ``retrieval_date`` (an explicit copy of the
envelope's own ``retrieved_at``, for readability without reaching into the
nested result). An entry older than ``TTL_DAYS`` is treated exactly like a
missing entry -- the caller must re-verify live rather than trust a
30-day-old "not available" or a stale finding as if it were fresh.

Every read goes through ``utils.read_json``, which already swallows a
corrupted/unreadable file and returns the given default -- so a corrupted
store degrades to "nothing remembered" (i.e. a live call happens) rather
than crashing the pipeline. Every write is wrapped separately so a failure
to persist (e.g. a locked file) never prevents returning a result the
caller already has.

This module never invents a finding: ``recall`` only ever returns a result
this same pipeline previously received from a live call and stored
verbatim; it does not guess, extrapolate, or fabricate a plausible-looking
substitute when nothing fresh is on record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..core.utils import atomic_write_json, ensure_dir, read_json, utc_now_iso

DEFAULT_STORE_PATH = Path("reports") / "external_lookup" / "memory" / "memory_store.json"

# How long a stored finding is considered fresh enough to recall without
# re-verifying live. 30 days matches this task's requirement -- long enough
# that a batch pipeline re-run the same day/week doesn't re-issue identical
# live calls, short enough that a finding is never trusted as current for
# an unbounded amount of time.
TTL_DAYS = 30


def _make_key(source: str, term: str) -> str:
    return f"{source}::{term.strip().lower()}"


def _load_store(store_path: Path) -> dict[str, Any]:
    """Read the store file. Returns {} for a missing file AND for a
    corrupted/unreadable one -- utils.read_json already catches any
    exception from a malformed JSON body and falls back to the given
    default, which is exactly the "degrade to live call, never crash"
    behavior this module requires."""
    data = read_json(store_path, default={})
    return data if isinstance(data, dict) else {}


def _is_fresh(stored_at: object, ttl_days: int) -> bool:
    if not isinstance(stored_at, str):
        return False
    try:
        stored_dt = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stored_dt.tzinfo is None:
        stored_dt = stored_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stored_dt <= timedelta(days=ttl_days)


def recall(
    source: str,
    term: str,
    *,
    store_path: Path = DEFAULT_STORE_PATH,
    ttl_days: int = TTL_DAYS,
) -> dict[str, Any] | None:
    """Return the previously-stored result for ``(source, term)`` if an
    entry exists and is within ``ttl_days`` of when it was stored.

    Returns ``None`` -- a cache miss, meaning the caller must make a live
    call -- when there is no entry, the entry has expired, or the store
    file itself is missing or corrupted. The returned dict is the exact
    result previously stored, including its original ``retrieved_at``;
    this function never alters it.
    """
    store = _load_store(store_path)
    entry = store.get(_make_key(source, term))
    if not isinstance(entry, dict):
        return None
    if not _is_fresh(entry.get("stored_at"), ttl_days):
        return None
    result = entry.get("result")
    return result if isinstance(result, dict) else None


def remember(
    source: str,
    term: str,
    result: dict[str, Any],
    *,
    store_path: Path = DEFAULT_STORE_PATH,
) -> None:
    """Persist ``result`` for ``(source, term)`` with the current time as
    ``stored_at``. Never raises: if the store can't be read or written (a
    locked file, a permissions error, ...), the write is silently skipped
    -- the caller already has the live result in hand and must not fail
    the lookup just because remembering it for next time didn't work."""
    try:
        store = _load_store(store_path)
        store[_make_key(source, term)] = {
            "result": result,
            "stored_at": utc_now_iso(),
            "retrieval_date": result.get("retrieved_at"),
        }
        ensure_dir(store_path.parent)
        atomic_write_json(store_path, store)
    except Exception:
        return


def recall_or_compute(
    source: str,
    term: str,
    compute: Callable[[], dict[str, Any]],
    *,
    store_path: Path = DEFAULT_STORE_PATH,
    ttl_days: int = TTL_DAYS,
    refresh: bool = False,
    remember_statuses: frozenset[str] | None = None,
) -> dict[str, Any]:
    """The memory-check step every external_lookup module runs before
    making a live call.

    - Fresh entry within ``ttl_days`` (and ``refresh`` not requested):
      return it unchanged except for ``from_cache``/``verification``,
      which are overlaid to mark it as recalled -- the original
      ``retrieved_at`` is preserved so a caller always sees when the
      finding was actually retrieved, not when it was replayed.
    - Expired, missing, or ``refresh=True``: call ``compute()`` for a live
      result, mark it ``verification="live"``, and -- only when its
      ``status`` is one of ``remember_statuses`` (a caller passes e.g.
      ``{"ok", "no_results"}``) -- store it for next time. A transient
      failure (rate-limited, network error, ...) is deliberately never
      remembered: freezing "the API was unreachable a moment ago" into
      30-day memory would misreport a temporary problem as a durable
      finding.
    """
    if not refresh:
        cached = recall(source, term, store_path=store_path, ttl_days=ttl_days)
        if cached is not None:
            envelope = dict(cached)
            envelope["from_cache"] = True
            envelope["verification"] = "cached"
            return envelope

    result = compute()
    if not isinstance(result, dict):
        return result

    envelope = dict(result)
    envelope["verification"] = "live"
    if remember_statuses is None or envelope.get("status") in remember_statuses:
        remember(source, term, envelope, store_path=store_path)
    return envelope
