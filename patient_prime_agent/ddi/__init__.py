"""Drug-drug interaction (DDI) and therapy-evidence engine.

Additive layer on top of the existing standalone-summary pipeline (see
``patient_prime_agent/ddi_summary.py``). Every module here reads only the
already-generated category summaries (clinical notes, genetics, CBC, EEG,
ECG) plus a small bundled static reference table of real, published
drug-metabolism facts -- it never fabricates a per-patient finding, a dose, a
drug that is not in the patient's actual regimen, or a "predicted" score with
no real source behind it.
"""
