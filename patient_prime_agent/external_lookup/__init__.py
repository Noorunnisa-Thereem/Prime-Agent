"""Live external-evidence lookups against public biomedical APIs.

Every module in this package makes a real HTTP GET request to a public API
(NCBI E-utils, DailyMed, CPIC, ClinicalTrials.gov) for a drug/gene name that
was already extracted from the patient's own data elsewhere in the pipeline
(Path B's ``genetics_summary.py`` / ``clinical_notes_summary.py`` outputs).
No module here hardcodes a patient, a drug, a gene, or a result -- if the
API returns nothing, times out, or errors, the caller gets an explicit
``status`` of ``"no_results"`` or ``"error"`` with the reason, never a
fabricated record. See ``http_client.py`` for the shared fetch/cache/
rate-limit helper and the response envelope shape every lookup returns.
"""

from __future__ import annotations
