# Task C: Short written answers

### C1. Notes to structure

1. **Redact first.** Before notes go anywhere, strip PHI (names, phone numbers, addresses) with regex + NER (e.g. Presidio) inside Canadian infrastructure. The sample notes already contain these ("brother Kevin 905-555-…").
2. **Discover the fields.** Review a sample of notes with staff and list the fields worth extracting: meeting attendance, passes, incidents, mood, risk signals. Mark each as categorical (e.g. `incident_type`: substance / violence / rules / vape) or non-categorical (e.g. meeting count, pass dates).
3. **Build an internal vocabulary and glossary.** Map shorthand to canonical terms ("AMA" → self-discharge, "UA" → urine test, "d/c" → discharge, "NA 3x/wk" → NA, 3/week).
4. **Regex for structured patterns.** Fixed shapes ("NA 3x/wk", "positive UA x2", "left AMA") are extracted by rules: cheap, exact, explainable.
5. **An LLM for the rest.** Structured output into the same schema (enum values only, "unknown" allowed), self-hosted or in a Canadian region, on redacted notes only.
6. **Evidence spans.** Every extracted field, from rules or the LLM, stores the exact text it came from (note id + character offsets), so any value can be traced back and checked.
7. **Validate before saving.** An LLM judge (a different model) checks each extraction against its span. Confidence routing: confident and judge-approved fields are saved (marked AI-extracted); low-confidence or disputed fields, and **every risk flag**, go to a staff review queue.

**Why:** rules give precision where notes are formulaic, the LLM gives coverage where they aren't, and the glossary keeps both consistent. Redaction keeps PHI contained, spans make every field auditable, and humans stay accountable for anything safety-related.

### C2. How I'd know it's accurate

**Evaluation set.** A hand-labelled golden set of 50–100 redacted notes, chosen to cover each field, including rare but important cases (incidents, risk signals). It grows over time from reviewed corrections.

**Labelling.** Two staff label each note independently against the glossary from C1; disagreements are resolved by a senior clinician. Fields where humans can't agree get their definition fixed first.

**Metrics.** Precision, recall and F1 per field, to see which fields are reliable and which aren't. For risk flags, recall matters most (missing one is worse than a false alarm).

**Human-in-the-loop.** Extractions are shown to staff in the product with a quick ✓/✗ (and a fix). Low-confidence fields and risk flags always go through this review. Every correction becomes a new labelled example.

**Drift.** Watch for it weekly: the ✗ rate, the "unknown" rate, and new terms that aren't in the glossary. When it moves, update the vocabulary, glossary and prompts, then re-run the golden set to confirm the change actually improves things.

**CI.** All of this runs in an eval harness in CI: no prompt, model or glossary change ships if golden-set scores drop.

### C3. Predictive idea: early self-discharge risk

**Use case.** Flag residents at elevated risk of leaving against advice in the next 2–4 weeks, so staff can check in early. In this data, self-discharge is the largest early-exit reason, and early exits happen at a median of ~47 days, so there's a window to act.

**Approach.** Start simple and interpretable: logistic regression as a baseline, then gradient-boosted trees (XGBoost). Features are rebuilt weekly per resident: days since admission, phase, recent incidents, meeting attendance, passes, mood flags (from C1), fee waived. Data is split **by time** (train on earlier cohorts, test on later ones), not randomly, to avoid leakage. If enough longitudinal note data builds up, a sequence model (e.g. 1D-conv/GRU over weekly features) can be tried, but only if it beats the baseline.

**Data prerequisites.**
- Reliable discharge reasons and dates.
- C1's structured note fields, validated (C2).
- Enough events: a single house has ~30 self-discharges, far too few, so pool across houses (with consent and agreements).
- Features computed only from information available *at prediction time*.

**Metrics.** Accuracy is misleading with imbalanced outcomes, so: AUC-ROC and PR-AUC, recall at a fixed alert budget (e.g. top 10% flagged), precision, F1, and calibration (does "30% risk" mean ~30%?). Everything is reported per subgroup.

**Main risks.**
- **Bias:** e.g. funder-sponsored or justice-referred residents flagged more often for structural reasons. Audit error rates by subgroup.
- **Small samples:** unstable models and overfitting. Use confidence intervals, simple models, pooled data.
- **Misuse:** risk scores used to deny admission, cut support or punish. The score may only be used to **offer more support**, never to restrict.
- **Feedback loops:** interventions change outcomes, so log the actions taken alongside the predictions.

**Before shipping.**
- It beats a simple rule (e.g. "first 60 days + a recent incident").
- Subgroup error rates are reviewed with clinicians and acceptable.
- It runs in **shadow mode** for one quarter (predictions hidden, compared to outcomes).
- Staff see the top reasons behind each flag, not just a score.
- Clear policy on permitted use, a PHIPA privacy impact assessment, and continuous monitoring with retraining on new cohorts.
