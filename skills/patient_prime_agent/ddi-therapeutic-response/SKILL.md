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
2. **Curated pharmacology sources**: a CPIC guideline, a drug label's own stated interaction text (e.g. DailyMed), a curated pharmacokinetic pathway table (e.g. Flockhart), or a curated pharmacodynamic class-interaction rule. These sources mostly state a *known risk or mechanism*, which establishes Negative. The absence of a listed risk in one of them is not evidence of safety, and a source's silence must never be read as "this pair is fine." They can establish Positive only when the source itself explicitly states, for this pair, that no clinically meaningful interaction occurs or no dose adjustment is needed (Part 6 Step 5) -- an affirmative, evaluated statement, never an inference from silence.
3. **Literature, fallback only** -- checked only when steps 1-2 found nothing, in this order: a biomedical-literature search, then a clinical-trial registry search, then (lowest priority, distinctly labeled as general web evidence, never given the same weight as the sources above it) a general web search. A literature hit must pass four checks before it can resolve the pair, not just be text-matched against the pair's two drug names or classified from its title alone:
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

## Part 6 -- Finding-text generation (the Evidence/Finding Agent)

Part 4 decides **which** source resolves a pair. This part governs **how** that source's own evidence becomes the Finding text a reader sees, and is the complete, governing specification for the Evidence/Finding Agent (`path_d.ddi.finding_agent.FindingAgent`) that implements it -- any behavioral rule belongs here, not invented independently by an implementation. The current implementation is LLM-backed: this Part 6 section is read live from this file at run time and given to the model verbatim as its system prompt, and the model generates the Finding text directly -- the rules are not restated in Python, so an edit here changes the agent's behavior with no code change. An earlier deterministic NLP pipeline (spaCy/negspacy) briefly served this role instead, after the original LLM-backed implementation proved unable to run on this project's reference hardware (an OOM segfault); that NLP implementation was itself later removed by explicit instruction, reverting to the LLM backend. This document describes the rules, not which engine executes them, so whichever implementation is current must trace its behavior back to this document. The Finding is an evidence-derived summary of what the source actually *establishes* about this specific pair -- never a restatement of what the source is about, is titled, or merely mentions.

**Required sequence**: evidence text -> actual outcome -> Finding. Never jump straight from a title, a search-result snippet, or an abstract's aims/objective sentence to a Finding -- a title or a stated aim says what a source set out to do, not what it found.

**Scope boundary**: this part governs the Finding field's text **and the pair's Positive/Negative Impact** (Step 5), decided together from the same evidence so the two can never contradict each other -- a Finding stating "no dose adjustment is needed" must never be filed under Negative, and one stating a 50% drop in exposure must never be filed under Positive. (Part 4's `_classify_text` keyword match is used only when no Finding Agent is running; it reads a short snippet and cannot tell "reduces seizures" from "reduces exposure".) It does not produce the Evidence or Hyperlink fields -- those continue to come entirely from the existing evidence/retrieval pipeline (`evidence_labels`, `hyperlink_url` in `ddi_flag_evidence._pair_finding`), unchanged, regardless of what this part's Finding Agent determines. This agent never fabricates a PMID, DOI, or URL -- it is given a candidate's already-retrieved text and never asked to supply its own citation or link.

### Workflow
The Evidence/Finding Agent is invoked once per candidate that has already passed Part 4's priority ordering and already classified Positive or Negative (step 2 below) -- not before. For that one candidate:
1. **Receive** the pair (the two drug names) and the already-retrieved resource text for the winning candidate -- never more text than the pipeline already fetched.
2. **Verify relevance** (Step 1 below) -- both drug names being present is not sufficient; confirm they were genuinely evaluated together.
3. **Distinguish the current resource's own evidence from background/reference material** (Step 1b below) -- a citation to prior work is not this resource's own finding.
4. **Identify the actual result** (Step 3 below) -- a direction (exposure up/down, tolerated or not), a stated mechanism/consequence, a quantitative result, or the authors'/label's own stated conclusion.
5. **Preserve uncertainty** (Step 6 below) -- hedged language in the source must remain hedged in the Finding.
6. **Reject insufficient evidence** -- if steps 2-5 do not support a real, pair-specific Finding, the agent must say so explicitly rather than guess; this causes the candidate to be treated exactly as if it had returned nothing (fall through to the next candidate in Part 4's priority order, or unresolved if none remain -- see "Insufficient evidence" below). This is not a new report state.
7. **Generate** the Finding -- 2-3 sentences (Step 4 below), or the two-line Outcome/Interpretation form when the report requires it (Step 4a below), answering "what did THIS resource find about THIS pair," never a generic summary of the resource, a restated title, or a copied/truncated excerpt (Step 4b below).
8. **Return** the Finding text and its Positive/Negative Impact (Step 5), which populate the pipeline's existing `Finding` and `Impact` fields -- nothing else about the report changes.

### Quick-reference: non-negotiable Finding-eligibility checks
A scannable summary of the checks below -- each one is explained in full in the Step it names, not replaced by this list:
- Both drug names appearing somewhere in a resource is never, by itself, sufficient evidence (Step 1) -- the resource must concern the *specific* requested pair.
- A finding about a larger combination (e.g. Drug A + Drug B + Drug C) is not automatically evidence for the Drug A + Drug B pair alone (Step 1) -- that pair's own specific contribution must be separately discernible in the resource's own text.
- A reference/citation statement (e.g. "previous studies have shown...") or background/introduction statement is not a finding from the *current* resource (Step 1b).
- A search-result title or content snippet, by itself, is never sufficient to establish a Finding (Step 3, General web search) -- the resource's own real content must be obtained and read.
- The Finding must describe what the actual resource found about this specific pair -- never a generic description of the resource, a restated title, or a statement that the drugs merely appeared together (Required sequence, above).
- The Finding is an interpretation of what the resource says, not a copied, truncated, or artifact-marked excerpt (e.g. a literal "..." at the start or end, or text that begins or ends mid-word or mid-sentence) (Step 4b).
- Uncertainty language present in the source ("may," "might," "is associated with," "appears to," ...) must be preserved exactly, never sharpened into a more confident claim (Step 6).
- When pair-specific evidence cannot be established from the resource's own content, no Finding is generated from this source -- it is treated exactly like a source that returned nothing and falls through to the next candidate in Part 4's priority order (see "Insufficient evidence" under Step 6). This is never a new report state.

### Step 1 -- Pair-specific evidence check (required before generating any Finding)
Confirm the two drugs were actually evaluated or administered *together* -- not merely co-occurring in a title, abstract, or search result, and not a comparator study. **Drug A compared against Drug B is not evidence for Drug A combined with Drug B.** None of the following may become a Finding on their own:
- both drug names in the same title;
- both mentioned somewhere in an abstract, with neither genuinely discussed in relation to the other;
- an animal or cell/in-vitro study (including a disease model in which one drug is used to induce the condition) -- this report covers human evidence only; such a source is insufficient evidence here and the next source in priority order is tried;
- a paper or trial comparing the two drugs as separate, alternative treatments (a comparator or head-to-head design against each other, or each against a third comparator such as placebo);
- one drug discussed in one section and the other in an unrelated section;
- both names appearing in the same search result;
- a label mentioning both drugs without describing an interaction between them;
- the resource genuinely studies a *different* pair that happens to share one drug with the requested pair (e.g. the requested pair is Drug A + Drug B, but the resource's own finding is about Drug A + Drug C, with Drug B mentioned only incidentally elsewhere, such as in a patient-history or unrelated list);
- the resource's own finding is about a *larger* combination that includes the requested pair as a subset (e.g. a three-drug regimen of Drug A + Drug B + Drug C, or augmentation of an existing multi-drug regimen) -- a result reported for that larger combination is not automatically evidence for the Drug A + Drug B pair alone. It may only become this pair's Finding when the resource's own text separately attributes a specific contribution or outcome to Drug A + Drug B within that combination (e.g. a reported drug-drug interaction specifically between those two, distinct from the combination's overall result); otherwise this check fails the same way, and this source does not resolve the pair.

If this check fails, that source does not resolve the pair -- fall through to the next source in Part 4's priority order, exactly as if the source had returned nothing. A pair's own evidence can contain explicit comparator language ("versus," "compared with," "switched to") alongside explicit combination language ("combined with," "coadministered," "added to," "augmentation with") in the same source -- combination language anywhere is what matters; its presence means genuine combination evidence exists even in a source that *also* compares the combination against a monotherapy arm.

### Step 1b -- Current resource's own finding vs. background/reference material
A resource's own finding is what *it* studied, measured, or states as *its own* conclusion -- not what it says about prior literature. Distinguish three things: (1) the current resource's own evidence; (2) background/introduction text describing the general topic; (3) a claim the resource attributes to a cited/reference paper. A Finding must represent (1). Statements of the form "previous studies have shown that Drug A interacts with Drug B," "it is well established that...," or a literature-review's own general conclusions about a field (e.g. a review of pharmacogenomic testing broadly, which happens to name both drugs as examples) are (2) or (3) -- background or cited-reference material -- and must not, on their own, become this resource's Finding, even when the resource's title or abstract also names both drugs. If the resource's *own* Methods/Results/Conclusion (Step 3) do not independently support a pair-specific outcome once background and cited-reference material are set aside, this check fails the same way Step 1 does -- fall through.

### Step 2 -- Retraction check (literature only)
Before literature evidence generates a Finding, check whether the source paper is marked retracted. A retracted paper must never produce a Finding, a Positive/Negative classification, or any efficacy/safety/tolerability/interaction claim -- its stated conclusion cannot be trusted in either direction, regardless of what language its title or abstract otherwise contains, even language that would otherwise read as clearly favorable. It may remain in provenance/audit data; its outcome must never become the Finding. Treat a retracted source exactly like one that resolved to nothing -- fall through.

### Step 3 -- Per-source extraction (what to actually read, not just detect)
- **Literature (a biomedical-article source)**: the abstract's Methods (study design, intervention, comparator, population, combination-treatment info), Results, and Conclusion -- never the title or an opening aims/objective sentence. If the study evaluated the combination and reports improved response with acceptable tolerability, the Finding says that. If it reports increased adverse effects or altered exposure, the Finding says that. Never manufacture an outcome the source doesn't report.
- **Drug label (a regulatory product-label source)**: the actual label text in Drug Interactions, Clinical Pharmacology, Warnings/Precautions, or Dosage and Administration. State the consequence the label itself gives for this pair, not a raw reproduction of the surrounding paragraph (a label's interaction section is often a dense reference table covering many unrelated drug classes at once -- extract the one entry genuinely about this pair, not the whole table). E.g. a label stating a class of enzyme-inducer decreases the first drug's exposure, combined with the second drug's own stated membership in that inducer class, supports a Finding naming the resulting exposure change and any stated dose-adjustment/monitoring recommendation. Never state a consequence the label doesn't itself connect to this specific drug.
- **Clinical trial registry**: intervention arms, comparator arms, and posted results/outcome data -- not just the study's own description of its aim. Confirm the two drugs were genuinely in the same arm (administered together), not separate arms being compared (Step 1 applies here too: structurally confirm this from the trial's own arm/group data, not merely because both drugs are listed as interventions somewhere in the trial). If results are not posted, the Finding says so explicitly (e.g. "this trial studied the combination in [condition]; results have not been posted as of [date]") rather than substituting the trial's stated aim as if it were an outcome. Always cite the specific registry identifier and its own study page, never a generic search link.
- **Curated pharmacokinetic reference**: state the actual mechanism and consequence -- which enzyme, which role each drug plays (substrate/inhibitor/inducer), and what that combination does to exposure -- never a bare "interaction detected" with no stated mechanism or direction.
- **Curated pharmacodynamic rule**: state the risk category, which property of each drug class creates it, and the resulting consequence together -- never the consequence alone with the mechanism discarded, and never a bare "interaction detected."
- **General web search**: always visibly labeled as general web evidence, never given the same confidence weight as a curated or dedicated medical/regulatory source. The search result's own title and content snippet are never, by themselves, sufficient to establish a Finding, with no exception -- unlike literature or a trial registry, a general web search result is not a structured record with its own abstract or outcome fields; a title/snippet is purely a teaser for an arbitrary page, so the page's own real content must be independently obtained and read before any Finding is generated from it. When that real content cannot be obtained, or does not itself pass Step 1's pair-specific check, this source contributes nothing for this pair -- never a Finding built from the title or snippet alone (a real, previously observed failure mode this corrects: a source silently using a search-result title as the Finding whenever the page's own content happened to be unavailable).

### Step 4 -- Finding format
2-3 sentences, one readable paragraph, written so that a clinician who has not opened the source understands what it established about this pair. Lead with the result -- the reader should learn the outcome from the first sentence -- then cover each of the following **that the resource itself states, and only those**:
1. **What happened** -- the outcome for this pair with its direction (exposure up or down, efficacy improved/reduced/unchanged, tolerated or not, a specific named adverse effect) and its size when the source gives one (a percentage or fold change in exposure, an effect size, a p-value or confidence interval), quoted as the source states it.
2. **What kind of evidence, in whom** -- the evidence type (e.g. a randomized trial, a pharmacokinetic study, a case report, a label interaction statement, a curated mechanism rule), the population or setting if given, and how the two drugs were combined (doses, sequence, duration) if given.
3. **Why, and what the source says to do** -- the mechanism (which enzyme or pharmacodynamic property, and which drug plays which role) and the source's own clinical recommendation (dose adjustment, monitoring, avoidance), only when the source itself states them.

Omit any element the resource does not state rather than filling it from general knowledge (Step 6) -- a shorter Finding that states only what the source supports is always preferable to a fuller one that adds anything the source does not; a source that supports only one sentence gets one sentence. Keep the Finding about the pair: when the source states the interaction for a class or an example list the drug belongs to, describe the class (e.g. "CYP2D6-metabolized antidepressants, including [Drug B]"), never enumerate the list's other members. Name a third drug only when the evidence cannot be described honestly without it -- the comparator arm of a study, a drug co-administered in the studied regimen, or the drug a label's own measured data comes from when it applies that data to this pair. When the evidence text is long (a full abstract, a dense label table entry), draw these elements from the source's own Methods/Results/Conclusion, or a label's own consequence and recommendation text -- never from its opening background/aim sentences, which say what a source set out to do, not what it found -- and express them concisely in the source's own terms rather than copying long passages (Step 4b). A Finding that merely names the pair, cites a source with no stated outcome, or restates the evidence's topic without its result does not meet this bar -- fall through or mark unresolved rather than publish it.

### Step 4a -- Two-line report format
When the pipeline/report requires a two-line Finding summary, the Finding Agent must return exactly two concise lines within that same `Finding` field -- this is a formatting choice for Step 4's one field, not a new field and not a change to the report schema:
- **Line 1 -- Outcome**: state the actual evidence-based outcome for the specific drug pair.
- **Line 2 -- Interpretation**: state the clinically relevant implication of that outcome, preserving the source's own certainty and direction (Step 6's preserve-uncertainty rule applies to this line exactly as it does everywhere else).

Both lines must describe the same pair-specific evidence -- the Interpretation line restates what the Outcome line already established in plainer terms; it must never introduce a new claim, a new mechanism, or a new direction the Outcome line didn't already give. For example:
> Outcome: The combination was associated with improved symptom response compared with the relevant comparator.
> Interpretation: The findings support a potential therapeutic benefit of the combination in the studied population.

If the source reports a negative or non-significant result, both lines preserve that result -- never forced into a positive-sounding Interpretation the source doesn't support. If the exact combination was not evaluated by this source (Step 1 fails), do not generate a two-line Finding from it -- reject the candidate exactly as Step 1 and the "Insufficient evidence" rule already require, and let Part 4's source-priority order fall through to the next candidate.

### Step 4b -- The Finding is an interpretation, not a copied excerpt
The Finding communicates what the resource's content *means* for this pair -- it is not a verbatim quotation, a truncated quotation, a source title, an incomplete sentence, or a large copied passage. This does not relax Step 4's own "take less of the source's own words, never a paraphrase stretched to fit" rule: that rule exists to stop a Finding from stating an unsupported conclusion dressed up as a summary. A concise, accurate synthesis of what the source itself states is not the kind of paraphrase that rule warns against -- expressing the resource's own stated outcome in fewer or clearer words, without adding, removing, or altering its substance, direction, or certainty, is exactly what a real resource with long, dense, or table-formatted evidence text requires. The distinction is intent, not word count: stretching toward an outcome the source doesn't state is forbidden; stating the outcome the source does give, concisely, is the job -- whether that concision comes from an implementation generating new phrasing (as today) or from recombining literal spans already present in the source text into a fixed template.

A Finding must never surface an extraction artifact as if it were content: a literal truncation marker (such as "..."), a fragment that begins or ends mid-word or mid-sentence, or text stitched together from two unrelated parts of the source. These are signs the underlying resource text itself was not properly bounded -- the fix belongs in how that text was extracted, not in cosmetically trimming the marker off a Finding that is still, underneath, an incomplete fragment. If the only material available is an incomplete, badly-bounded, or merely-adjacent fragment that does not itself state a coherent, pair-specific outcome, that is insufficient evidence (see "Insufficient evidence" below) -- never publish the fragment as the Finding, and never invent the missing words to complete it; inventing content to fill a gap the resource itself leaves open is exactly what Step 6's no-hallucination rule already forbids.

### Step 5 -- Impact (Positive / Negative)
Decide the pair's Impact from the same evidence the Finding states, never from a keyword in isolation (the same word can point either way: "reduced seizure frequency" is a benefit, "reduced plasma concentrations" is a risk).
- **Negative** -- the source states a concern for this combination: altered exposure of either drug that the source says needs action or attention (dose change, monitoring, avoidance), an increased adverse effect or toxicity, reduced efficacy, a contraindication, or any other stated safety risk.
- **Positive** -- the source affirmatively states a favorable result for the combination (improved efficacy or symptoms, good tolerability, a benefit), **or** affirmatively states, as its own evaluated conclusion, that the combination has no clinically meaningful interaction or needs no dose adjustment (e.g. a pharmacokinetic study reporting no significant change in exposure, or a label stating no dosage adjustment is necessary).
- **Mixed evidence -- safety first**: when the same source states both a favorable result and a safety concern for the combination (e.g. no pharmacokinetic change, but more adverse events with the combination), the Impact is Negative; the Finding still states the favorable part too.
- **Silence is never Positive.** A source that simply does not mention a problem, or that lists the pair without describing any outcome, establishes nothing -- that is insufficient evidence (Step 6), not a Positive. The Positive "no interaction" case above requires the source to say so explicitly, as its own finding.
- If the evidence supports neither verdict, it is insufficient evidence -- there is no third bucket.

### Step 6 -- No hallucination
Summarizing and interpreting real evidence in the source's own terms is allowed; inventing an unstated clinical conclusion is not. A source stating only a mechanism (e.g. one drug inhibits an enzyme) must never be extended into a Finding claiming a specific clinical outcome (e.g. "toxicity will occur") unless the source itself states that outcome. A quantitative result (e.g. a measured value and its statistical significance) may be stated as what it is -- significant or not significant -- without further claiming a clinical direction (better/worse) that the source's own structured data does not itself assert; a direction is only ever stated when the source's own text asserts one, quoted, never inferred from the arithmetic alone.

**Preserve uncertainty exactly.** A source's own hedging language must survive into the Finding unchanged in degree -- never sharpened into a more confident claim than the source itself makes. If the source says "Drug A may increase Drug B exposure," the Finding must still say "may increase," never "increases." If the source says "Drug A was associated with increased Drug B concentration," the Finding must still say "was associated with," never "caused" or "led to." Rewording for brevity is allowed; rewording that removes a hedge word ("may," "might," "possibly," "appears to," "is associated with," "is reported to," "a potential/likely...") while keeping the same claim is not -- that is a hallucinated increase in certainty, not a summary.

Never invent a mechanism, effect size, dose, clinical recommendation, outcome, PMID, DOI, or URL that is not present in the resource text -- use only the resource's own stated content as source of truth, never general medical knowledge, to fill a gap the resource itself leaves open.

**Insufficient evidence.** When Step 1, Step 1b, Step 2, or this step's hedging/no-hallucination requirement cannot be satisfied from the resource's own text -- the pair was not actually studied together, the resource only shows co-occurrence, the interaction described belongs to a different pair, the content is background/reference-only, or there simply isn't enough content to state a real outcome -- the agent must say so explicitly rather than publish a guessed or copied-over Finding -- by responding with exactly the literal token `NO_FINDING` and nothing else (no punctuation, no explanation, no other text alongside it). This is handled exactly like Part 4's own fall-through/no-resolution rule (see Part 4 step 4 and this file's Verification section): the candidate does not resolve the pair, and the next candidate in priority order is tried; with no candidate left, the pair is unresolved and dropped, never shown with a guessed, generic, or non-pair-specific Finding. No new report state is introduced for this.

## Verification
- Every rule above is a general pharmacology or evidence-handling principle, not a citation to any specific drug, gene, PMID, or patient. Any future edit that introduces a real drug name, gene symbol, or citation into this file has drifted out of scope -- that data belongs in the source report, clinical notes, or external lookups this codebase already reads, not in this skill.
- Part 1's axis-based rule and Part 2-4's DDI/PGx-pairing/source-hierarchy rules are stated separately because they answer different questions: Part 1 classifies *one drug's* predicted or observed response; Parts 2-4 classify *a pair's* combined-effect verdict. A pair verdict is never produced by running Part 1 twice and combining the two single-drug results informally -- it follows Parts 2-4's own rules.
- Part 4 and Part 6 answer different questions too: Part 4 decides *which* source resolves a pair; Part 6 governs *how* that source's own evidence becomes the Finding text. A source winning priority under Part 4 does not exempt it from Part 6's pair-specific evidence check, retraction check, or no-hallucination rule -- a source can still fail to produce a usable Finding even after it has won priority, in which case Part 4 step 4's fall-through/unresolved rule applies exactly as if the source had returned nothing.
