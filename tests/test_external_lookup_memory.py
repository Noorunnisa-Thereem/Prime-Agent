"""Tests for external_lookup's long-term memory store (Box 04: Context &
memory -- memory_store.py).

This is a batch pipeline, so the only memory it needs is long-term: "have
we already looked this (source, term) up recently?" These tests cover, per
the task:

  - memory_store.py in isolation: a fresh entry recalls unchanged, an
    entry past the 30-day TTL is treated as a miss, a corrupted store file
    degrades to "nothing remembered" rather than raising, and a failed
    write is swallowed rather than propagated.
  - End-to-end through a real lookup module (pubmed_lookup, representative
    of all five, which all share the same memory_store.recall_or_compute
    wiring): first run is live and stores the finding; a second run within
    TTL recalls it -- with the ORIGINAL retrieval date, never a new one --
    without making any live call; a run after expiry (simulated by
    rewriting the stored timestamp, not by waiting 30 real days) refreshes
    live; a corrupted store file still degrades to a live call, never a
    crash.
  - report_html.py's LIVE/CACHED badge reflects the new "verification"
    field these envelopes now carry, with a fallback to the older
    "from_cache" flag for any envelope that predates this change.

Every test here uses an isolated tmp_path for the memory store -- never
the pipeline's real reports/external_lookup/memory/memory_store.json --
so test fixtures (fake PMIDs, fake titles) can never leak into what a real
patient report run would recall as an already-verified finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from patient_prime_agent import report_html
from patient_prime_agent.external_lookup import memory_store, pubmed_lookup


def _fake_fetch_json(idlist: list[str], title: str, *, calls: list[str], retrieved_at: str = "2026-01-01T00:00:00Z"):
    esearch_body = {"esearchresult": {"idlist": idlist}}
    esummary_body = {
        "result": {pmid: {"title": title, "source": "Journal", "pubdate": "2026", "lastauthor": "Someone"} for pmid in idlist}
    }

    def _fetch(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        calls.append(url)
        body = esearch_body if "esearch" in url else esummary_body
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": retrieved_at, "from_cache": False, "error": None}

    return _fetch


def _push_stored_at_back(store_path, days: int) -> None:
    """Rewrite every entry's stored_at to `days` in the past, simulating an
    old entry without actually waiting -- the TTL is measured wall-clock,
    so this is the only practical way to test expiry."""
    store = json.loads(store_path.read_text(encoding="utf-8"))
    old_stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    for entry in store.values():
        entry["stored_at"] = old_stamp
    store_path.write_text(json.dumps(store), encoding="utf-8")


# ---------------------------------------------------------------------------
# memory_store.py in isolation
# ---------------------------------------------------------------------------


def test_recall_returns_none_for_a_missing_store_file(tmp_path):
    assert memory_store.recall("SourceA", "termx", store_path=tmp_path / "missing.json") is None


def test_remember_then_recall_round_trips_a_fresh_entry(tmp_path):
    store_path = tmp_path / "memory.json"
    result = {"status": "ok", "retrieved_at": "2026-01-01T00:00:00Z", "records": [{"title": "hi"}]}

    memory_store.remember("SourceA", "TermX", result, store_path=store_path)
    recalled = memory_store.recall("SourceA", "termx", store_path=store_path)  # key is case-insensitive

    assert recalled == result


def test_recall_treats_an_entry_past_the_ttl_as_a_miss(tmp_path):
    store_path = tmp_path / "memory.json"
    memory_store.remember("SourceA", "termx", {"status": "ok", "retrieved_at": "2026-01-01T00:00:00Z", "records": []}, store_path=store_path)

    _push_stored_at_back(store_path, days=memory_store.TTL_DAYS + 1)

    assert memory_store.recall("SourceA", "termx", store_path=store_path) is None


def test_recall_degrades_to_a_miss_on_a_corrupted_store_file(tmp_path):
    store_path = tmp_path / "memory.json"
    store_path.write_text("{this is not valid json at all!!", encoding="utf-8")

    assert memory_store.recall("SourceA", "termx", store_path=store_path) is None  # must not raise


def test_remember_swallows_a_write_failure_instead_of_raising(tmp_path):
    # store_path's parent is a plain file, not a directory -- ensure_dir/
    # atomic_write_json cannot create it, so remember() must not propagate
    # that failure; the caller already has its live result regardless.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store_path = blocker / "memory.json"

    memory_store.remember("SourceA", "termx", {"status": "ok"}, store_path=store_path)  # must not raise


def test_recall_or_compute_never_remembers_a_transient_error_status(tmp_path):
    store_path = tmp_path / "memory.json"
    calls = {"n": 0}

    def _compute_error() -> dict:
        calls["n"] += 1
        return {"status": "rate_limited", "retrieved_at": "2026-01-01T00:00:00Z", "records": []}

    envelope = memory_store.recall_or_compute(
        "SourceA", "termx", _compute_error, store_path=store_path, remember_statuses={"ok", "no_results"}
    )
    assert envelope["status"] == "rate_limited"
    assert envelope["verification"] == "live"
    assert memory_store.recall("SourceA", "termx", store_path=store_path) is None, (
        "a transient failure must never be frozen into 30-day memory as if it were a durable finding"
    )

    memory_store.recall_or_compute(
        "SourceA", "termx", _compute_error, store_path=store_path, remember_statuses={"ok", "no_results"}
    )
    assert calls["n"] == 2, "nothing was remembered, so the second call must also compute live"


# ---------------------------------------------------------------------------
# End-to-end through a real lookup module (pubmed_lookup is representative --
# every module shares the same memory_store.recall_or_compute wiring)
# ---------------------------------------------------------------------------


def test_first_run_makes_a_live_call_and_stores_the_finding(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(["111"], "First Title", calls=calls))
    memory_path = tmp_path / "memory.json"

    envelope = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)

    assert calls, "a never-seen term must make a real (here, faked) live call"
    assert envelope["verification"] == "live"
    assert envelope["from_cache"] is False
    assert memory_path.exists(), "a genuine finding (status ok) must be persisted for next time"


def test_second_run_within_ttl_recalls_with_the_original_retrieval_date(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(["111"], "Original Title", calls=calls))
    memory_path = tmp_path / "memory.json"

    first = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)
    calls.clear()

    # Swap in a fake that would return different data, to prove the second run
    # never even reaches it.
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(["222"], "Different Title", calls=calls))
    second = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)

    assert calls == [], "a fresh (within-TTL) entry must be recalled without any live call"
    assert second["verification"] == "cached"
    assert second["from_cache"] is True
    assert second["retrieved_at"] == first["retrieved_at"], "recall must keep the ORIGINAL retrieval date"
    assert second["records"][0]["title"] == "Original Title", "recall must replay the original finding, not a new one"


def test_run_after_ttl_expiry_refreshes_live(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(["111"], "Original Title", calls=calls))
    memory_path = tmp_path / "memory.json"

    first = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)
    calls.clear()

    _push_stored_at_back(memory_path, days=memory_store.TTL_DAYS + 1)
    monkeypatch.setattr(
        pubmed_lookup, "fetch_json", _fake_fetch_json(["333"], "Refreshed Title", calls=calls, retrieved_at="2026-02-01T00:00:00Z")
    )
    second = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)

    assert calls, "an expired entry must trigger a fresh live call, not a stale recall"
    assert second["verification"] == "live"
    assert second["records"][0]["title"] == "Refreshed Title"
    assert second["retrieved_at"] != first["retrieved_at"]


def test_corrupted_memory_store_falls_back_to_a_live_call(monkeypatch, tmp_path):
    memory_path = tmp_path / "memory.json"
    memory_path.write_text("{this is not valid json", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(["444"], "Live Despite Corruption", calls=calls))

    envelope = pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)  # must not raise

    assert calls, "a corrupted store must degrade to a live call, not a crash"
    assert envelope["verification"] == "live"
    assert envelope["records"][0]["title"] == "Live Despite Corruption"


# ---------------------------------------------------------------------------
# report_html.py: LIVE/CACHED badge reflects "verification"
# ---------------------------------------------------------------------------


def test_retrieved_cell_shows_live_and_cached_badges_from_verification_field():
    live_cell = report_html._retrieved_cell([{"retrieved_at": "2026-01-01T00:00:00Z", "verification": "live", "from_cache": False}])
    cached_cell = report_html._retrieved_cell([{"retrieved_at": "2026-01-01T00:00:00Z", "verification": "cached", "from_cache": False}])

    assert "LIVE" in live_cell
    assert "CACHED" in cached_cell


def test_retrieved_cell_falls_back_to_from_cache_when_verification_is_absent():
    # Envelopes predating memory_store.py carry no "verification" field --
    # the badge must still work from the older "from_cache" flag.
    legacy_live = {"retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False}
    legacy_cached = {"retrieved_at": "2026-01-01T00:00:00Z", "from_cache": True}

    assert "LIVE" in report_html._retrieved_cell([legacy_live])
    assert "CACHED" in report_html._retrieved_cell([legacy_cached])
