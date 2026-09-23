---
name: ddi-therapeutic-response
description: Use when classifying a patient's response to a single drug into one of the 4 categories used by the NeuroPrecisionDx PGx report (Good Response / Average Response / Toxicity / Poor Response), or when classifying a drug-drug pair as Positive Impact / Negative Impact -- e.g. `path_d/ddi/aggregation.py`'s `build_therapy_assessment()`, `ddi_flag_evidence.py`'s per-pair classification, or explaining what these categories mean and how a pair verdict may be formed from two drugs' individual PGx findings. This skill states rules and mechanisms only -- it names no specific drug, gene, or PMID; those live in the patient's own report/data files, not here.
---

# DDI Therapeutic Response & Drug-Drug Interaction Classification

## Purpose
Define, as reusable rules independent of any specific drug, gene, or citation:
1. What the 4-category single-drug response system (Good Response / Average Response / Toxicity / Poor Response) means and how to apply it to a Clinical Impact sentence, genotype-predicted or observed.
2. How a drug-drug interaction (DDI) mechanistically happens, in general pharmacology terms.
3. When two drugs' individual PGx findings may be combined into one pair-level verdict.
4. The priority order across evidence sources for classifying a pair as Positive Impact / Negative Impact, and what happens when no source resolves it.

Nothing in this file should ever be read as a statement about a specific drug, gene, or patient -- that data lives in the source PGx report / clinical notes / external lookups, not in this skill. If a future edit adds a real drug name, gene symbol, or PMID here, it no longer belongs in this file.

## Part 1 -- Single-drug response classification (4 categories)

### The categories
| Symbol | Label |
|---|---|
| Green filled circle | Good response |
| Orange/yellow filled circle | Average response |
| Red filled circle | Toxicity |
| Black/dark filled circle | Poor Response |
| Open/hollow circle | "No interaction detected. Use as per recommendation" -- a distinct, separate state, not a 5th tier |

The 5th state means the panel found nothing relevant for that drug. Never fold it into one of the 4 categories.

A source PGx report typically states no numeric threshold, lab value cutoff, or scoring formula for any of the 4 categories -- each row's category is a judgment already made by the report's authors, not a formula the reader can reproduce independently.

### Classification rule (axis-based)
Every Clinical Impact sentence (or, for observed evidence, an evidence item's `statement`) sits on one of two axes. Classify by axis first, then by direction within that axis:

1. **Toxicity (red)** -- the sentence is on the *safety/adverse-event axis*: risk language ("increased risk of...", "reduced risk of...", "...risk-involved response...") about a side effect, adverse reaction, or toxicity. **This holds regardless of whether the specific risk is reported as increased or decreased** -- the category names the axis being reported (safety), not which direction that one finding points.
2. **Good Response (green)** -- the sentence is on the *efficacy axis* and states a better/improved response, with no risk/safety language in the same sentence.
3. **Poor Response (black/dark)** -- efficacy axis, states a reduced/inadequate response, no risk/safety language.
4. **Average Response (orange/yellow)** -- efficacy axis, states a moderate/partial response.

**Requires clinician judgment** (do not guess when):
- A sentence mixes efficacy and safety language together in a way the source itself does not resolve.
- A quantitative threshold would be needed to separate "moderate" from "reduced" response, and the source states none. The instrument used varies (rating scale, plasma concentration, weight/BMI change, an enzyme activity assay, a cognitive test, etc.) and carries no stated cutoff.
- Multiple findings for the *same drug* disagree (e.g. one finding implies better response, another implies increased risk, for the same drug) and no cross-finding precedence rule is stated. This codebase's own evidence-precedence rules (e.g. "patient-specific observed evidence outranks predicted evidence," in `path_d/ddi/aggregation.py`) may be the right place to resolve this in code -- that is this codebase's own rule, not something any source PGx report states.
- Whether an "increased risk" of a mild, expected, or already-monitored side effect should be weighted the same as an "increased risk" of a severe one. Apply "Toxicity" uniformly to both severities by default, but flag that a clinician may reasonably want to distinguish them further downstream.

### A separate, unrelated annotation axis -- do not conflate
A source PGx report may carry two other marker systems on the same rows; neither is part of the 4-category Therapeutic Response system and both must stay out of this classification:
- **Metabolizer Phenotype** (Ultrarapid / Rapid / Normal / Intermediate / Poor Metabolizer) and **Drug Transporter Type** (Increased / Normal / Decreased / No / Unknown function) markers -- these encode transporter/metabolizer function, a different axis entirely, even when they appear alongside a response marker on the same row.
- **FDA/CPIC guideline markers** -- indicate only that a drug has an FDA or CPIC guideline on file, unrelated to response classification.

### Applying this to observed evidence (this codebase's own extension, not a source-PGx-report rule)
A PGx report classifies a *predicted* response from genotype, independent of whether the drug has actually been taken. This codebase's own observed clinical/lab/EEG/side-effect evidence for a current-regimen drug is a different kind of evidence -- the mapping below is this codebase's bridge for that purpose, held to lower confidence than Part 1 above:

| PGx report input | Observed-evidence equivalent |
|---|---|
| A gene's Clinical Impact sentence | One evidence item's `statement` (in a therapy's `supporting_evidence` / `counter_evidence`) |
| Genotype-predicted risk of a side effect | A real clinical-notes / lab / EEG finding, or a reported side effect, described as an adverse or safety concern |
| Genotype-predicted better/moderate/reduced efficacy | A real observed treatment-response outcome (symptom or seizure control, a lab-confirmed effect, a clinician's efficacy impression) |

Classification order for a therapy's existing evidence items:
1. Any evidence item's `statement` describes an adverse effect, toxicity, or safety risk for this drug (regardless of whether stated as present or resolved) -> **Toxicity**.
2. Else, the strongest efficacy-relevant evidence states a favorable/improved response -> **Good Response**.
3. Else, the strongest efficacy-relevant evidence states a reduced/inadequate response -> **Poor Response**.
4. Else, efficacy evidence is mixed or partial (some supporting, some counter, no adverse-event item) -> **Average Response**.
5. No efficacy- or safety-relevant evidence for this drug at all -> **requires clinician judgment** (never default to one of the 4 categories; state the gap explicitly -- unresolved is never collapsed into "no evidence" / "no concern").

## Part 2 -- How a drug-drug interaction (DDI) mechanistically happens

Two drugs can interact through any of the following general mechanisms. These are pharmacology concepts, not statements about any specific drug pair -- a real pair's actual mechanism (if any) comes from a curated reference (Flockhart, CPIC, DailyMed) or literature, never guessed from this list alone.

1. **Pharmacokinetic -- metabolic (enzyme) interaction.** One drug inhibits or induces a cytochrome P450 (or other metabolic) enzyme that the other drug is a substrate of. Inhibition raises the substrate drug's effective exposure (risk of toxicity); induction lowers it (risk of therapeutic failure). This is the mechanism a curated CYP-pathway overlap table (e.g. Flockhart-style) captures.
2. **Pharmacokinetic -- transporter-mediated interaction.** Competition for the same uptake or efflux transporter (e.g. a P-glycoprotein-type transporter) alters how much of one drug reaches its target tissue when the other is co-administered.
3. **Pharmacokinetic -- protein-binding displacement.** A highly protein-bound drug can displace another from plasma protein binding, transiently raising the free (active) concentration of the displaced drug.
4. **Pharmacodynamic interaction.** Two drugs act on the same or a physiologically related target/pathway, producing a combined effect that is additive, synergistic, or antagonistic -- independent of any change in either drug's blood concentration. Examples of the *kind* of combined effect (not tied to any specific drug pair): additive central-nervous-system depression, additive QT-interval prolongation, additive serotonergic activity, opposing effects at a shared receptor.
5. **Absorption/formulation interaction.** One substance alters gastrointestinal absorption of another (e.g. via chelation, pH change, or altered gut motility) without a metabolic or receptor-level mechanism.

A pair may have more than one real mechanism at once, or none at all (most drug pairs, in fact, have no clinically meaningful interaction).

## Part 3 -- Pharmacogenomic (PGx) pair-combination rule

A patient's PGx panel reports *single-drug* findings: one gene, one drug, one predicted response. Combining two drugs' individual PGx rows into a single pair-level verdict is only valid when the two findings are genuinely mechanistically linked -- never merely because both drugs happen to carry *some* PGx row.

**Combine two PGx rows into one pair verdict only when at least one of these holds:**
- **Shared gene**: both drugs' PGx rows name the exact same gene symbol (e.g. the same metabolic enzyme gene governs both drugs' clearance) -- this is direct evidence the same genetic variant plausibly affects both drugs together.
- **Confirmed enzyme relationship**: one drug's PGx row names a gene encoding a metabolic enzyme or transporter, and a curated pharmacokinetic reference (not this skill, not a guess) independently states that the *other* drug is a substrate, inhibitor, or inducer of that same enzyme/transporter.

**Never combine two PGx rows into a pair verdict when:**
- The two rows name different, mechanistically unrelated genes, even if both rows individually look clinically significant for their own drug. Two real single-drug findings do not add up to a real pair finding unless a genuine shared mechanism connects them -- treating them as if they did manufactures a pair conclusion the genetics do not actually support.
- Only one of the two drugs has any PGx row at all. A single-drug finding is evidence about that one drug, not about the pair.

## Part 4 -- Evidence source hierarchy for pair-level classification

Classify a drug pair as **Positive Impact** / **Negative Impact** by checking sources in this order. Never skip ahead to a later source once an earlier one resolves the pair, and never let a later source overturn what an earlier, higher-priority source already established. There is no third "in-between" bucket -- a source either resolves the pair as Positive or Negative by the rules below, or it does not resolve the pair at all (see step 4).

**How a source is checked -- retrieval before classification.** For each tier below, the pair is handed to that tier's own dedicated search/lookup tool (the PGx panel lookup, the curated-reference lookup, the literature-search tool) and only what that tool actually returns is evaluated against the rules for that tier. A pair is never classified from its two drug names alone, and a tier that returns nothing real (no matching PGx row, no curated entry, no literature hit) simply does not resolve the pair -- it is not evidence of safety, and it is not skipped past silently either; the next tier is checked in its place, in order.

1. **Patient's own PGx findings** (Part 3's shared-mechanism check). The most specific evidence available -- this patient's actual genotype, for a pair where the mechanism is genuinely shared. Classify by the axis-based rule in Part 1 (safety-axis language -> Negative/Toxicity-analog; efficacy-axis language -> Positive/Good-analog). Mixed or unclear language does not resolve the pair from this source -- fall through to the next source instead of forcing a verdict.
2. **Curated pharmacology sources**: a CPIC guideline, a drug label's own stated interaction text (e.g. DailyMed), a curated pharmacokinetic pathway table (e.g. Flockhart), or a curated pharmacodynamic class-interaction rule. **None of these four sources alone can establish a Positive verdict.** Each of them, by construction, only ever states a *known risk or mechanism* when it says anything at all about a pair -- the absence of a listed risk in one of these sources is not evidence of safety, and a source's silence must never be read as "this pair is fine." These sources can establish Negative (a stated risk/mechanism); a genuine Positive verdict needs affirmative tolerability/efficacy language, which these four source types do not carry, so they never resolve a pair as Positive on their own.
3. **Literature (PubMed), fallback only** -- checked only when steps 1-2 found nothing. A literature hit must pass four checks before it can resolve the pair, not just be text-matched against the pair's two drug names or classified from its title alone:
   - **Section-aware**: read the paper's own Abstract, Methods, Results, and Conclusion -- a title, or an abstract's opening Background/Introduction sentence, is never enough on its own. Each section answers a different question and none substitutes for another:
     - *Methods* -- confirms the pair was actually studied together (population, dosing, design), not merely both named somewhere in a paper about something else. This is what makes the co-occurrence check below genuine rather than coincidental.
     - *Results* -- the paper's own observed/measured finding, in the authors' own words -- the most direct evidence the paper contains.
     - *Conclusion* -- the authors' own interpretation of the Results. Weight it alongside Results, not instead of it: a conclusion can overstate, understate, or generalize beyond what the Results actually show, so a conclusion that is not backed by a matching Results statement is weaker evidence than one that is.
     - *Abstract* -- read as the summary of the other three, useful when the full paper is not accessible, but never treated as a 5th independent source of truth on top of them.
   - **Negation-aware**: "no significant interaction was observed" and "a significant interaction was observed" must classify oppositely, even though they share most of the same words -- a keyword match that ignores the negating clause misclassifies the pair in the wrong direction, which is worse than not classifying it at all. Apply this check within every section read above, not only the conclusion.
   - **Conclusion-weighted, not conclusion-only**: prefer the Results/Conclusion pairing over an incidental keyword appearing only in Background/Introduction text (which typically describes prior literature, not this paper's own finding) -- but per the Section-aware check above, a Conclusion is only trusted when Results supports it, not read in isolation.
   - **Co-occurrence-checked**: both drugs must genuinely co-occur in the same clinically relevant context within the source, confirmed by Methods -- not merely both appear somewhere in a paper that is not actually about their combination (e.g. a multi-arm trial comparing several unrelated drugs, or a review that lists both among many unrelated agents).
4. **No source resolves the pair.** Exclude the pair from the report entirely rather than presenting a fabricated or default verdict.

**Open tradeoff, flagged rather than decided here:** step 4's "silent exclusion" (the pair simply does not appear in the report) is easy to read as "nothing to worry about," when the true state is "nothing was found." A labeled "Unresolved" row that still appears in the report is more honest about that distinction but changes the report's shape (every screened pair would appear, not only the ones with evidence) -- and it is still not a 3rd classification bucket alongside Positive/Negative, only a marker that neither applies. Which behavior is correct is a product decision, not a rule this skill states -- confirm with whoever owns the report's intended reading before this ships against real patient data.

## Part 5 -- Evidence status tags

Every classified finding (single-drug or pair-level) should be tagged with where it came from, so a reader can see how strong the underlying evidence is without re-deriving it:

- **curated** -- from a hand-maintained pharmacology reference (a CPIC guideline, a curated CYP-pathway table, a curated pharmacodynamic-class rule). Reviewed and maintained independent of this patient.
- **predicted** -- from this patient's own genotype (a PGx finding), describing an expected response that has not necessarily been clinically observed yet.
- **observed** -- from this patient's actual real-world clinical notes, labs, EEG, or reported side effects -- what actually happened, not what genotype predicts.
- **inferred** -- derived from an automated literature search (e.g. PubMed) via the negation-aware/conclusion-targeted/co-occurrence-checked process in Part 4, rather than from a curated reference or this patient's own data.

`predicted` and `observed` can disagree for the same drug (genotype predicts one thing, real experience shows another) -- when they do, this codebase's own evidence-precedence rules decide which wins for a therapy's overall assessment; this skill only defines what each tag means, not how conflicts between tags resolve.

## Verification
- Every rule above is a general pharmacology or evidence-handling principle, not a citation to any specific drug, gene, PMID, or patient. Any future edit that introduces a real drug name, gene symbol, or citation into this file has drifted out of scope -- that data belongs in the source report, clinical notes, or external lookups this codebase already reads, not in this skill.
- Part 1's axis-based rule and Part 2-4's DDI/PGx-pairing/source-hierarchy rules are stated separately because they answer different questions: Part 1 classifies *one drug's* predicted or observed response; Parts 2-4 classify *a pair's* combined-effect verdict. A pair verdict is never produced by running Part 1 twice and combining the two single-drug results informally -- it follows Parts 2-4's own rules.
