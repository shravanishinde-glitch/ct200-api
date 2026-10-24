# Approach document – CT-200 Manual API

> Sections marked **[CONFIRM]** describe your own debugging history. Check them against what actually happened and
> reword them in your own voice before submitting: the company will ask you to defend every line.

## 1. Data model

SQLite (SQLAlchemy):
- `documents(name)` -> `document_versions(version_no, title, parser warnings)` -> `nodes`.
- `nodes`: `id` (unique **per version row**), `logical_id` (stable identity across versions), `number`, `heading`, `level`,
  `parent_id`, `order_index`, `body`, `tables_json`, `content_hash` (sha256 of normalized body + tables),
  `change_status` (new/unchanged/changed vs previous version), `prev_node_id`, `warnings_json`, `page`.
- `selections` -> `selection_items(node_id)`. A selection points at node **rows**, and a row belongs to exactly one
  version, so a selection is version-pinned by construction. Re-ingesting only adds rows; nothing is updated or deleted.

Generated output (MongoDB, or JSON file when `MONGO_URI` is unset): one document per generation containing
`selection_id`, `idempotency_key`, `prompt_version`, `model`, `status`, `attempts`, `warnings`, `raw_output`,
`test_cases[]` (each with `source_node_ids`) and `sources[]` – a **snapshot** of every source section
(node id, logical id, version, content hash and the full text at generation time).
Why NoSQL: a generation is a nested blob, written once and read whole, never joined. Why a snapshot: staleness
must be computable even if the relational rows change shape later, and a test case must always be traceable to the exact
text it came from. Why a JSON-file fallback: the reviewer can run everything with zero infrastructure; both backends
implement the same `insert/get/find` interface (`app/store.py`). The JSON store is not safe for multiple server
processes (single process lock only) – fine for a demo, not for production.

## 2. Parsing approach

**Tool: pdfplumber on the PDF text layer, not OCR.** The CT-200 PDF is born-digital (selectable text, font names, bounding
boxes). Font names and geometry are *information OCR throws away*; OCR would add character errors in pressure
values, which is the worst possible error in this domain. The assignment update explicitly allows "PDF parsing and/or OCR".
Fallback behaviour: if no numbered sections are found (e.g. a scanned PDF), ingestion fails with 422 and stores nothing,
rather than producing an empty or wrong tree. A production version would add an OCR path (e.g. page rasterisation +
Tesseract) behind the same interface for scanned input.

**Hierarchy reconstruction.** A line is a heading iff it matches `^\d+(\.\d+)*\.?\s+\S` **and** is entirely bold.
Level = number of dots + 1. **Parent = longest existing numeric prefix**, not "the previous heading". Body lines go to the
current heading; tables are detected geometrically, excluded from body text and attached to the heading *above* them.

**Irregularities found and how each is handled** (each has a test in `tests/test_parser.py`):
| Irregularity | Handling |
|---|---|
| Duplicate heading "Error Codes" (4.2 and 7.1) | Identity is the section **number**, never the title; two nodes, parents 4 and 7. |
| Skipped level: 2.1.1.1 under 2.1 (no 2.1.1) | Parent = nearest existing ancestor (2.1); a parser warning is recorded on the node and the version. |
| Out-of-order sections (3.4 appears before 3.3) | Order is kept as in the document; parent still correct; "out of order" warning recorded. |
| Some headings are 11 pt, same size as body | Detection uses **bold + number pattern**, not font size. |
| Cross-reference text like "see 2.1, 4.3" | Not a heading: not at line start / not bold. Test asserts exactly 27 nodes. |
| Ligatures (ﬁ ﬂ) and non-breaking hyphens (CT‑200) | NFKC normalization + hyphen mapping before hashing/matching. |
| Tables (2.1, 4.2), possibly continued over a page break | Cell grid kept in `tables_json`; nested fragment detections dropped; continuation rows merged with a warning. |
| Page breaks moving text between pages | Hash is computed over normalized text, so a page-break shift in v2 is **not** a change (tested). |

**What the first implementation got wrong, how it was found, what changed [CONFIRM].** Things the code comments and
tests show were fixed: (1) headings detected by font size missed 11 pt bold headings -> switched to bold + number;
(2) "previous heading" parenting broke on the skipped level and out-of-order cases -> numeric-prefix parenting;
(3) a table was attached to the last heading on its page instead of the one above it -> bbox ordering;
(4) table fragments inside larger tables were double counted -> bbox containment filter; (5) table text leaked into
body text -> lines inside table boxes are skipped; (6) ligatures made v1/v2 hashes differ for identical text -> NFKC.
Discovery method: manual comparison of `pdftotext -layout` output with the parsed tree, the node-count assertion (27),
v1 vs v2 hash comparisons, and unit tests per irregularity. **Not handled:** figures/images and captions – the CT-200
sample has none; if they appeared they would be silently ignored, which is exactly the failure the brief warns about, so
the safe next step is a validation that compares image count per page with captured figures and raises a warning.

## 3. Versioning / matching strategy
Pass A: same section **number** and heading similarity >= 0.6 -> same logical node. Pass B: for what is left, same *parent
logical node* and heading similarity >= 0.85 -> same logical node (handles renumbering, e.g. 5.3 -> 5.4). Otherwise **new**;
unclaimed old nodes are **removed**. Same logical node: equal hash = unchanged, else changed.
Why: numbers are unique even when titles repeat, headings catch renumbering, hashes make "changed" objective and immune to
page-break shifts. Result on the samples: changed = 2.1.1.1, 3.2, 4.2, 4.3; new = 5.3; removed = none.

**Where it breaks:** (a) a section split in two or two merged -> seen as removed + new, staleness says `source_removed`
(safe, but noisy); (b) a heading rewritten *and* renumbered at once -> new + removed; (c) two sections swap numbers and
keep similar titles; (d) pass A trusts the number: if section 5.3 is deleted and a different 5.3 inserted with a
heading >= 0.6 similar, it is called the same node (content differs, so it shows as `changed`, which is at least visible);
(e) the hash covers all body text, so a typo fix is "changed" (by design – safer than hiding it).

## 4. Browse / selection
See README endpoint table. Selections store node row ids (version-pinned). `GET /selections/{id}` shows the pinned text
plus the status of the same logical section in the latest version.

## 5. LLM generation: prompt, validation, failure policy
- **Prompt (`app/llm.py`)**: role = QA engineer for a medical device; sections supplied with `[NODE id] number heading`
  headers; rules: 3–5 concrete, repeatable cases, **use only facts in the text and reuse exact numbers**, skip vague
  sections, cite source node ids, treat section text as data (prompt-injection guard), reply with one JSON object of a
  stated shape. Provider JSON mode is requested too, but never trusted.
- **Validation**: Pydantic (`LLMOutput`/`TestCase`): at least 3 cases, non-blank title/steps/expected result, priority enum.
- **Policy when the LLM misbehaves**: tolerate fences/chatter around the JSON; otherwise retry up to 3 attempts, sending the
  validation error back to the model; provider errors count as attempts; after that -> **HTTP 502** and a record with
  `status: failed` (attempts + raw output kept for debugging, hidden from normal retrieval). We never save half-valid output as
  a result, because in QA a plausible-looking but malformed test case is worse than no test case. Safe auto-repairs only:
  >5 cases -> keep first 5 (warning); cited ids not in the selection -> dropped; a case citing nothing valid -> attributed
  to the whole selection (warning; conservative for staleness). Missing key -> 503 (retrying cannot help). Input over
  12,000 chars -> 413.
- **What validation cannot catch:** a well-formed test case containing an invented threshold. Mitigation is only the prompt
  rule plus the stored source snapshot so a human can compare; a numeric-grounding check (every number in a test case must
  appear in its cited sources) is the first thing I would add.
- **Same selection submitted twice (policy):** return the existing successful generation (200, `reused: true`). The key is
  sha256 of the sorted `(node id, content hash)` pairs + prompt version + model. Reasons: LLM output is non-deterministic,
  so silently returning different test cases for the same input would break traceability; it also saves cost. `?force=true`
  creates an additional generation; old ones are kept. A failed run never blocks a retry.

## 6. Staleness / impact detection
Every generation stores snapshots of its source sections. At **retrieval** time, each snapshot is compared with the same
*logical* section in the *latest* version (`latest_counterpart`):
`fresh` (hash equal) · `possibly_stale` (text changed, wording only) · `stale` (any number changed, or a safety-relevant word
such as not/must/within/before/cannot was added or removed) · `source_removed`.
A test case = worst status over the sections it cites; a generation = worst over its test cases. Each source carries a reason
and a word-level diff. Nothing is auto-cleared or regenerated (out of scope).
**Does a one-word change equal a changed pressure threshold? No** – graded. But also not "safe": both are non-fresh and need
a human look; the grade only helps prioritise. **Limits:** the classifier is a token heuristic, not semantics. It misses
meaning flips not covered by the word list or the digit check (e.g. "two" -> "three", "before" -> a rephrased condition spelled
differently), and it is section-granular: if a section has 10 requirements and one changes, every test case citing that
section is flagged even if it only exercised the other nine. Citations come from the LLM, so a wrong citation means wrong
impact; the fallback "whole selection" errs on the side of over-flagging. A change in a *different* section that alters the
meaning of a cited one (e.g. a definition) is not detected.

## 7. Retrieval
`GET /selections/{id}/test-cases`, `GET /nodes/{id}/test-cases`, `GET /generations/{gid}` all return staleness computed live.
The node route matches by **logical id**, so asking about the v2 node of 4.2 returns the test cases generated from v1 – this is
the "what does this document change affect?" question.

## 8. Decision log (drafts – rewrite in your own words)
1. **Most likely silent wrong result:** the staleness classifier. A meaning-changing edit that contains no digit and no keyword
   from my list is reported `possibly_stale` (looks harmless). Catch it: keep a labelled set of real v1->v2 edits and assert
   the classifier's grade for each (the test suite has a few); in production, route every non-fresh case to review and
   track how often reviewers upgrade `possibly_stale` to critical. Second candidate: LLM source citations.
2. **Simplicity over correctness:** section-level (not requirement-level) granularity for staleness and hash; heuristic word list;
   JSON-file store with a process-local lock; pdfplumber heuristics tailored to this one PDF. First to break in production:
   concurrent writers on the JSON store (use Mongo) and the parser on the next differently formatted manual.
3. **Input not handled:** a scanned/image-only PDF (ingestion rejects it with 422, nothing stored); also figures/captions are
   not extracted. For the LLM: a selection whose text is vague gives vague tests – the system does not detect that, it only
   validates shape. (Pick the one you can explain best.)

## 9. With more time
Requirement-level (sentence/clause) nodes and hashes; numeric grounding check for generated test cases; semantic-diff
(LLM-as-judge or embedding) as a *second opinion* on `possibly_stale`; OCR fallback path; figure/caption extraction with
a per-page image-count validation; Alembic migrations; background job for generation; Mongo indexes on `selection_id` /
`logical_ids`; real concurrency tests.
