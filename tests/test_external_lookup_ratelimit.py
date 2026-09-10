"""Real, non-mocked rate-limit test for external_lookup.

Every other test in this package (test_external_lookup.py,
test_external_lookup_guardrails.py, test_external_lookup_sanitization.py)
monkeypatches http_client.fetch_json, so it proves the parsing/guardrail
logic but never actually exercises what happens when NCBI itself pushes
back with a real HTTP 429. This file does that: it fires a burst of real,
concurrent requests at NCBI E-utils (PubMed's host, shared with ClinVar)
with http_client's own polite per-host throttle disabled and refresh=True
(bypassing this package's on-disk cache), so every call in the burst is a
genuine live request with no artificial delay between them.

During development, a *sequential* burst of even 60 distinct rapid calls
was not enough to provoke a 429 from this network -- NCBI's real-world
tolerance for a rapid-but-one-at-a-time sequence turned out to be more
generous than its published per-second limit suggests. Firing the same
kind of burst *concurrently* (ThreadPoolExecutor, all requests in flight
at once) reliably did trigger real 429s (37 of 40 concurrent requests in
one observed run) -- so this test uses concurrency, not just a tight loop,
to have a realistic chance of exercising the real code path.

WHY THIS IS EXCLUDED FROM THE FAST SUITE
-----------------------------------------
This test depends on live network conditions and a third party's rate
limiter actually tripping. It is not guaranteed to trigger deterministically
(NCBI's limits, this machine's network path, and shared-IP conditions can
all vary run to run), and it makes 20+ real concurrent outbound HTTP calls
and takes real wall-clock time -- none of which belong in a suite meant to
run on every commit.

It is registered under the custom "slow" pytest marker (see pyproject.toml,
which also excludes `-m slow` from the default `addopts` so a plain
`pytest` run skips it automatically). Run it explicitly, manually or on a
nightly schedule, with:

    pytest tests/test_external_lookup_ratelimit.py -m slow
"""

# Calibrated against NCBI's live throttling behavior as of 2026-09-10.
# This test may need adjustment if NCBI changes its rate-limit behavior.

from __future__ import annotations

import concurrent.futures

import pytest

from patient_prime_agent.external_lookup import http_client, pubmed_lookup
from patient_prime_agent.external_lookup.http_client import ERROR_STATUSES

# Real, harmless, distinct query terms -- distinct so NCBI's per-query result
# cache can't mask the burst as a single repeated request, and so this
# doesn't look like a scripted attack against one specific record. Plain
# antiseizure-drug names, all real and already used elsewhere in this
# project's own test fixtures.
_BURST_TERMS = [
    "lamotrigine", "levetiracetam", "carbamazepine", "phenytoin", "valproate",
    "topiramate", "gabapentin", "pregabalin", "oxcarbazepine", "zonisamide",
    "clobazam", "perampanel", "lacosamide", "vigabatrin", "tiagabine",
    "felbamate", "rufinamide", "ethosuximide", "primidone", "phenobarbital",
]


def _run_concurrent_burst(monkeypatch, tmp_path, retmax: int = 1, max_workers: int = 20) -> list[dict]:
    """Fire every term in _BURST_TERMS at pubmed_lookup.search_pubmed concurrently,
    with http_client's own polite per-host throttle disabled, so the burst actually
    lands on NCBI all at once, giving it a realistic chance of provoking a real 429
    (see module docstring: sequential bursts did not reliably reproduce this).
    refresh=True bypasses both this package's on-disk HTTP cache and its long-term
    memory store (memory_store.py), so every call is a genuine live request.
    memory_path is pinned to an isolated tmp_path file so this real, concurrent
    burst against real drug names never writes into the pipeline's actual
    long-term memory store."""
    monkeypatch.setattr(http_client, "_throttle", lambda host: None)
    memory_path = tmp_path / "memory.json"

    def _one(term: str) -> dict:
        return pubmed_lookup.search_pubmed(term, retmax=retmax, refresh=True, memory_path=memory_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_one, _BURST_TERMS))


@pytest.mark.slow
def test_rapid_real_requests_degrade_cleanly_when_ncbi_pushes_back(monkeypatch, tmp_path):
    """Fire 20 real, concurrent, throttle-disabled requests at NCBI E-utils. This
    does not assert that a 429 WILL happen -- that's outside this test's control,
    which is exactly why it's excluded from the fast/required suite. It asserts
    that IF NCBI pushes back, every one of the three concrete behaviors this task
    cares about holds:

      1. The lookup never raises -- a rate limit degrades to a clean envelope, not
         a crash that would take down the rest of external_evidence_summary's run.
      2. A rate-limited envelope never carries a fabricated/guessed value and is
         never mislabeled as a trustworthy live hit: its "from_cache" flag is
         False (a rate-limited call is never written to, or served from, the
         on-disk cache of genuinely successful responses -- see
         http_client.fetch_json, which only caches an error-free fetch), and any
         record it does carry (the bare PMIDs from an esearch that succeeded
         before esummary got rate-limited -- see pubmed_lookup.py's partial-
         esummary path) has title=None rather than a guessed title.
      3. The failure reason is distinguishable: status is "rate_limited"
         specifically for an HTTP 429 (one of http_client.ERROR_STATUSES, never
         collapsed into one generic "error"/"failed" state), and the envelope's
         "note" says "Not retrieved in this session" -- the exact phrase this
         pipeline uses everywhere else for "we don't have this, and we are not
         pretending otherwise."
    """
    envelopes = _run_concurrent_burst(monkeypatch, tmp_path)

    # Requirement 1: no exception escaped the burst -- reaching this line already
    # proves every one of the calls returned a plain dict rather than raising.
    assert len(envelopes) == len(_BURST_TERMS)
    for envelope in envelopes:
        assert isinstance(envelope, dict)
        assert "status" in envelope

    rate_limited = [e for e in envelopes if e.get("status") == "rate_limited"]
    other_errors = [e for e in envelopes if e.get("status") in (ERROR_STATUSES - {"rate_limited"})]
    successes = [e for e in envelopes if e.get("status") in ("ok", "no_results")]

    # Every envelope must land in exactly one recognized, distinguishable bucket --
    # none of the three statuses observed here may collapse into each other.
    assert len(rate_limited) + len(other_errors) + len(successes) == len(envelopes)

    if not rate_limited:
        pytest.skip(
            "NCBI did not rate-limit this burst (allowed it all through) -- nothing "
            "to verify this run; this is expected/tolerated live-network variance, "
            "not a test failure. Re-run, or widen _BURST_TERMS/max_workers, to try "
            "to reproduce a 429."
        )

    for envelope in rate_limited:
        # Requirement 3: the specific reason is recoverable from the envelope, not
        # a generic "error" -- and it uses the pipeline's real "not retrieved"
        # vocabulary, not a made-up one just for this test.
        assert envelope["status"] == "rate_limited"
        assert envelope["status"] in ERROR_STATUSES
        assert envelope.get("http_status") == 429
        assert envelope.get("error"), "the raw HTTP error text must be preserved, not discarded"
        note = envelope.get("note") or ""
        assert "Not retrieved in this session" in note
        assert "rate-limited" in note.lower() or "rate limit" in note.lower()

        # Requirement 2: no fabricated data, and never mislabeled as a live hit.
        assert envelope.get("from_cache") is False, "a rate-limited call must never be reported as served from the trusted cache"
        for record in envelope.get("records", []):
            # The only records a rate-limited PubMed envelope can legitimately carry
            # are bare PMIDs from an esearch that succeeded before esummary got
            # rate-limited (see pubmed_lookup.py) -- title must be None, never guessed.
            assert record.get("title") is None, "a rate-limited lookup must not report a guessed/fabricated title"


def test_error_status_vocabulary_is_registered_and_distinguishable():
    """Fast, offline sanity check (always runs, not network-gated) that the
    distinguishable-failure-reason contract the live test above relies on
    actually exists in http_client, independent of whether NCBI cooperates
    on any given run."""
    assert "rate_limited" in ERROR_STATUSES
    assert "network_error" in ERROR_STATUSES
    assert len(ERROR_STATUSES) >= 3, "collapsing every failure into one or two buckets defeats the point of this contract"
