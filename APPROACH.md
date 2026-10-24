
# Approach Document — CT-200 Manual API

## 1. Data Model

The application uses SQLite with SQLAlchemy to store documents, their versions, and extracted sections.

- `documents(name)` stores document information.
- `document_versions(version_no, title, parser warnings)` records each version of a document.
- `nodes` stores extracted sections. Each node has a version-specific ID and a `logical_id` that links the same section across versions. It also stores the section number, heading, hierarchy, body text, tables, content hash, change status, previous-node reference, warnings, and page number.
- `selections` and `selection_items(node_id)` record which sections were selected for test generation.

Each selection refers to specific node rows, so it remains associated with the version from which it was created. Ingestion adds new version records without overwriting earlier source data.

Generated results are stored in MongoDB when configured. If `MONGO_URI` is not set, the application uses a JSON file instead. Each generation record contains the selection ID, idempotency key, prompt version, model, status, attempts, warnings, raw output, test cases, and source snapshots.

A generation is stored as a complete nested record, making a document database suitable for this data structure. Source snapshots preserve the original text and content hashes so that changes can be detected later. The JSON fallback makes the application easier to run without additional infrastructure. Both storage implementations use the same `insert/get/find` interface in `app/store.py`.

The JSON implementation uses a process-local lock. It is suitable for a demonstration but would need a different approach for concurrent production use.

## 2. Parsing Approach

### PDF Text Extraction

The parser uses `pdfplumber` to extract text from the PDF's existing text layer instead of applying OCR. The CT-200 sample is a born-digital PDF with selectable text, font information, and character positions. These details help identify headings and tables. OCR could also introduce errors into important numerical values.

The assignment permits PDF parsing and/or OCR, so text-layer extraction is sufficient for the supplied document.

If the parser cannot find numbered sections, ingestion returns HTTP 422 without storing an empty or incorrect section tree. Scanned PDFs are not supported by the current parsing path. A future improvement would be to add OCR using a tool such as Tesseract behind the same parsing interface.

### Reconstructing the Section Hierarchy

A line is identified as a heading when it matches the numbered-section pattern `^\d+(\.\d+)*\.?\s+\S` and is entirely bold. The section number determines the hierarchy level.

The parser identifies a parent by looking for the nearest existing numeric prefix instead of assuming that the previous heading is the parent. This supports documents with skipped levels and sections that appear out of order.

Body lines are assigned to the current section. Tables are identified using page geometry, excluded from normal body text, and attached to the heading above them.

### Document Irregularities and Handling

| Case | Handling |
|---|---|
| The heading "Error Codes" appears in sections 4.2 and 7.1 | Section numbers distinguish the nodes, so both sections remain separate. |
| Section 2.1.1.1 appears without section 2.1.1 | The nearest existing ancestor, 2.1, is used and a warning is recorded. |
| Section 3.4 appears before 3.3 | Original document order is preserved and an out-of-order warning is recorded. |
| A heading uses the same 11 pt font size as body text | Heading detection uses bold formatting and the numbering pattern rather than font size alone. |
| Text contains references such as "see 2.1, 4.3" | Such references are not treated as headings unless they meet the heading rules. The parser test expects 27 nodes. |
| Text contains ligatures or non-breaking hyphens | NFKC normalisation and hyphen mapping are applied before matching and hashing. |
| Tables appear in sections 2.1 and 4.2, potentially across page breaks | Table data is stored in `tables_json`; nested fragments are filtered and continuation rows are merged with a warning. |
| Text moves between pages in different versions | Hashing uses normalised text, so a page-break-only shift does not count as a content change. |

These cases are covered by the parser's tests. The implementation uses formatting and numbering for heading detection, numeric ancestry for parent selection, geometry for table placement, and normalisation before hashing.

Figures and captions are not extracted. The supplied CT-200 sample does not contain them, so this limitation has not been exercised by the sample. A future improvement would be to detect images on each page and issue a warning when figures appear to have been missed.

## 3. Versioning and Section Matching

The application compares document sections in two passes.

1. **Match by section number:** Sections with the same number and heading similarity of at least 0.6 are treated as the same logical section.
2. **Match remaining sections:** Sections with the same parent logical section and heading similarity of at least 0.85 are matched. This helps identify renumbered sections, such as 5.3 becoming 5.4.
3. Unmatched new sections are marked `new`, while unmatched sections from the earlier version are marked `removed`.
4. For matched sections, equal content hashes mean `unchanged`; different hashes mean `changed`.

Section numbers help distinguish repeated headings. Heading similarity provides a fallback for renumbered sections, while normalised content hashes make comparisons less sensitive to page-break changes.

For the supplied sample versions, the reported changed sections are 2.1.1.1, 3.2, 4.2, and 4.3. Section 5.3 is new, and no sections are reported as removed.

There are some limitations to this strategy. Splitting one section into two or merging two sections can appear as a removal and a new section. A section that is both renamed and renumbered may also be classified as new and removed. Sections that exchange numbers can be matched incorrectly if their headings remain similar.

The first pass relies on section numbers, so a replacement section with a sufficiently similar heading may be matched to the earlier section and reported as changed. The content hash also treats a typo correction as a change. This is intentional because the system should not silently ignore changes to source text.

## 4. Browsing and Selection

The README contains the endpoint table. A selection stores the IDs of the chosen section rows, keeping its source text tied to a specific document version.

`GET /selections/{id}` returns the pinned source text together with the status of the corresponding logical sections in the latest version. This allows users to review the original content and see whether it has changed.

## 5. LLM Generation, Validation, and Failure Handling

### Prompt Construction

The prompt in `app/llm.py` frames the model as a QA engineer working with a medical device. Each supplied section includes its node ID, section number, and heading.

The prompt requests three to five concrete and repeatable test cases. It instructs the model to use only facts from the source, preserve exact numerical values, skip vague sections, cite source node IDs, treat document content as data rather than instructions, and return a JSON object in the required format.

The provider's JSON mode is requested when available, but the application still validates the response.

### Validation and Error Handling

Pydantic models (`LLMOutput` and `TestCase`) validate the generated response. At least three test cases are required. Each case must contain a non-empty title, steps, and expected result, along with a valid priority.

The application can remove code fences or surrounding text from a response. If validation still fails, it retries up to three attempts and sends the validation error back to the model. Provider failures also count as attempts.

If generation fails after the permitted attempts, the API returns HTTP 502 and records a failed generation with attempt details and raw output for debugging. Normal retrieval does not expose that raw failed output. Incomplete or invalid test cases are not saved as a successful result.

Limited automatic repairs are supported:

- If more than five test cases are returned, the first five are retained and a warning is added.
- Citations that do not belong to the selected nodes are removed.
- If a test case has no valid citation, it is associated with the entire selection and a warning is recorded.

A missing provider key returns HTTP 503 because retrying cannot resolve a configuration problem. Input longer than 12,000 characters is rejected with HTTP 413.

These checks validate the response structure, but they cannot guarantee that every generated fact is correct. For example, a response may be valid JSON while still inventing a threshold. The prompt discourages this, and stored source snapshots allow a reviewer to compare the generated case with the original text.

A useful next improvement would be a numeric-grounding check that verifies whether every number in a test case appears in its cited source text.

### Repeated Requests

Submitting the same selection again returns the existing successful generation with HTTP 200 and `reused: true`.

The idempotency key is based on the sorted node ID and content-hash pairs, the prompt version, and the model. Reusing a successful result avoids unnecessary cost and prevents repeated requests for identical inputs from silently producing different test cases.

Using `?force=true` requests an additional generation while preserving earlier records. A failed generation does not prevent a later retry.

## 6. Staleness and Impact Detection

Every generation stores snapshots of its source sections. When a result is retrieved, the application compares each snapshot with the corresponding logical section in the latest document version.

The source status is classified as follows:

- `fresh`: The content hash is unchanged.
- `possibly_stale`: The text changed, but the heuristic did not identify a stronger warning.
- `stale`: The section number changed, or a safety-related word such as "not", "must", "within", "before", or "cannot" was added or removed.
- `source_removed`: The corresponding section no longer exists.

A test case receives the most severe status among the sections it cites. The generation receives the most severe status among its test cases. Each source includes a reason and a word-level difference.

The system does not automatically clear stale results or regenerate test cases.

A one-word change is not automatically treated the same as a changed pressure threshold. However, every non-fresh result still needs human review. The classifier uses heuristics rather than fully understanding meaning. It may miss changes such as "two" becoming "three", or a condition being rephrased without using a recognised keyword.

The check also operates at section level. If one requirement changes inside a section, all test cases citing that section may be flagged, even when they cover different requirements. The system relies on source citations from the model, so an incorrect citation can produce an incorrect impact assessment. If no valid citation remains, the whole selection is used as a conservative fallback.

A change in another section that affects the meaning of a cited section is not detected by this comparison.

## 7. Retrieval

The endpoints `GET /selections/{id}/test-cases`, `GET /nodes/{id}/test-cases`, and `GET /generations/{gid}` calculate staleness when results are retrieved.

The node endpoint matches sections by logical ID. For example, requesting the v2 node for section 4.2 can return test cases generated from its v1 counterpart. This helps answer the question: "Which existing tests might be affected by this document change?"

## 8. Decision Log

### 1. What could silently produce a misleading result?

The main risk is the staleness classifier. It relies on keyword and number checks, so a meaningful change that does not match those rules could be labelled `possibly_stale` instead of `stale`.

To reduce this risk, I would test the classifier against labelled before-and-after edits and require human review for every result that is not fresh. Recording cases where reviewers upgrade a `possibly_stale` result would also help identify weaknesses in the rules.

Incorrect source citations from the model are another risk because the impact check depends on those citations.

### 2. Where did I choose a simpler design, and what did the runs show?

The staleness check works at section level instead of tracking individual requirements. In one observed run, all five test cases were marked stale even though only the E3 timing and the new E6 row changed in section 4.2.

This happened because the system flags test cases based on the section they cite. It does not know which individual requirement each test case exercises. Tracking requirements separately could reduce these unnecessary warnings, but it would require a more detailed data model and matching process.

The application also provides a JSON-file storage option and uses PDF-layout heuristics designed around the supplied manual. These choices keep the demo straightforward to set up, but the JSON store is not suitable for concurrent writers across multiple server processes, and the parser may need changes for a differently formatted manual.

For production, I would use a database configuration suitable for concurrent access and test the parser against a wider variety of documents.

### 3. What real failure did I observe, and how did the system respond?

During a generation run, the Groq model became unavailable and the provider request failed. The API returned HTTP 502, retained the attempt log for debugging, and did not save the failed response as a successful test-case result.

This is the intended failure behaviour: preserve the information needed to investigate the problem without presenting incomplete or unreliable test cases as a successful result.

The parser also rejects scanned or image-only PDFs with HTTP 422 rather than creating an empty section tree. Figures and captions are not extracted either. These are known input limitations, not failures I am claiming occurred during the sample run.

If broader PDF support were needed, I would add OCR and figure/caption validation.

## 9. Sample Run Observations

The observed runs demonstrated two important behaviours.

### Section Change and Staleness

In section 4.2, the E3 timing was changed and a new E6 row was added. All five test cases were marked stale because they cited the changed section.

This demonstrates the current section-level limitation: the system flags test cases by their cited section rather than identifying the precise requirement each test case covers.

### Provider Failure

When the Groq model call failed, the API returned HTTP 502, retained the attempt log, and did not save the unsuccessful response as a successful generation.

The available notes do not include a complete step-by-step transcript or the exact output from step 7. The observations above are therefore documented without inventing additional console output.

## 10. Future Improvements

With more time, I would prioritise the following improvements:

1. Track individual requirements using separate nodes and content hashes.
2. Add numeric grounding checks for generated test cases.
3. Use semantic comparison as a second opinion for results labelled `possibly_stale`.
4. Add OCR support for scanned PDFs.
5. Extract figures and captions and validate image counts per page.
6. Introduce database migrations using Alembic.
7. Move test generation into a background job.
8. Add MongoDB indexes for selection and logical-section lookups.
9. Test concurrent requests and storage writes.
