-- CPIC merged-dataset schema, as proposed and approved from the source-file
-- inspection of data/Final_merged_sheet.xlsx. See scripts/load_data.py for
-- the loader that populates these tables.
--
-- Design notes (why TEXT everywhere, why three tables):
--  * Gene Studied, Drug Name, Drug Class, Disease Studied, Genotype, and
--    Response Category all have real casing/synonym/typo variance in the
--    source sheet, so they are stored as TEXT on the fact table rather than
--    forced into lookup-table foreign keys that would either fail to load
--    or silently merge distinct source strings.
--  * Total Participants, Cases/Control, No. of SNP Selected, Frequency of
--    Genetic Variants (%), and Page(s) are free text with embedded numbers
--    (e.g. "More than 170,000", "3 for SLCO1B1, 3 for ABCB1"), not safe to
--    cast to INTEGER/NUMERIC -- stored as TEXT.
--  * cpic_source_documents captures the real one-to-many relationship
--    between a source PDF and the findings extracted from it.
--  * cpic_finding_variants captures the real one-finding/many-variant-alias
--    relationship discovered during inspection (276 groups of rows that
--    were identical in every column except the variant name/RsID).

CREATE TABLE IF NOT EXISTS cpic_source_documents (
    source_document_id  BIGSERIAL PRIMARY KEY,
    pdf_name             TEXT NOT NULL UNIQUE,   -- raw, e.g. '21716271_4.pdf'
    source_pubmed_id     BIGINT,                 -- derived: numeric prefix before '_'
    extract_seq          INT,                    -- derived: numeric suffix before '.pdf'
    loaded_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cpic_findings (
    finding_id                          BIGSERIAL PRIMARY KEY,
    source_document_id                  BIGINT REFERENCES cpic_source_documents(source_document_id),
    page_ref                            TEXT,   -- raw 'Page(s)', e.g. '6, 709'
    gene_studied                        TEXT,
    genotype                            TEXT,
    disease_studied                     TEXT,
    drug_name                           TEXT,
    dosage_form                         TEXT,
    drug_class                          TEXT,
    route_of_medication                 TEXT,
    duration_of_medication              TEXT,
    dosage_adjustments                  TEXT,
    combination_therapies               TEXT,
    response_category                   TEXT,
    therapeutic_interaction             TEXT,
    total_participants                  TEXT,
    cases_control                       TEXT,
    no_of_snp_selected                  TEXT,
    measure_of_response                 TEXT,
    dosage_effect_relationship          TEXT,
    frequency_of_genetic_variants_pct   TEXT,
    association_stats                   TEXT,
    core_finding                        TEXT,
    alert_point                         TEXT,
    ethnicity                           TEXT,
    row_hash                            TEXT,   -- sha256 of the normalized finding row
    loaded_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cpic_finding_variants (
    finding_variant_id  BIGSERIAL PRIMARY KEY,
    finding_id          BIGINT NOT NULL REFERENCES cpic_findings(finding_id) ON DELETE CASCADE,
    variant_raw          TEXT NOT NULL   -- raw 'Genetic Variant(s) & RsID' text/alias
);

CREATE INDEX IF NOT EXISTS idx_findings_gene       ON cpic_findings (gene_studied);
CREATE INDEX IF NOT EXISTS idx_findings_drug       ON cpic_findings (drug_name);
CREATE INDEX IF NOT EXISTS idx_findings_disease    ON cpic_findings (disease_studied);
CREATE INDEX IF NOT EXISTS idx_findings_response   ON cpic_findings (response_category);
CREATE INDEX IF NOT EXISTS idx_findings_source_doc ON cpic_findings (source_document_id);
CREATE INDEX IF NOT EXISTS idx_variants_finding    ON cpic_finding_variants (finding_id);
