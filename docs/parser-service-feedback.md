# Feedback for the Parser team — Indic OCR service

Date: 2026-11-05
From: OpenPDFSpecs (MCP knowledgebase) integration
Service tested: `POST /v1/parse`, Baidu Unlimited-OCR-2 LoRA, RTX 4050 6GB

This has two parts: what we measured about output quality, and what the service
needs to expose before we can integrate it properly.

Headline: the model is doing well on layout and structure, and its remaining text
errors are systematic character confusions rather than resolution problems. Do not
spend effort on higher-resolution input. The confusion table in section 3 is the
thing worth acting on.

---

## 1. How we measured

We found an authoritative source for the test book, so every number below is against
real ground truth rather than against another OCR pass.

- Document: `yogratnakar_part01.pdf`, 64 pages, Ayurvedic text, mixed Devanagari and
  English with scholarly IAST transliteration.
- Ground truth: the markdown the PDF was generated from, 122 Sanskrit verses.
- Scoring: character-level sequence similarity per verse, whitespace and dandas
  normalised away.

One thing to know about this PDF, because it explains why we were OCRing it at all:
its embedded text layer is damaged. The fonts are `Identity-H` TrueType and the
`ToUnicode` map is wrong in places, so 510 characters (5.6% of the Devanagari)
extract as Arabic or arrow codepoints. **The rendered page is perfect** — we verified
the glyphs by eye at 400 DPI. So the model was always being shown a clean, complete
image. None of the errors below are caused by the source being degraded.

---

## 2. Resolution: 200 DPI is optimal, higher is worse

We swept DPI on isolated verse crops, then repeated on three verses at the two
resolutions that mattered.

Single-verse sweep:

| DPI | Accuracy |
|-----|----------|
| 150 | 90.4% |
| 200 | 94.6% |
| 300 | 94.6% |
| 400 | 87.7% |
| 600 | 89.2% |

Three verses, two repeats each:

| DPI | Mean accuracy | Range | Samples |
|-----|---------------|-------|---------|
| 200 | 93.3% | 91.3 - 94.6% | 6 |
| 400 | 86.2% | 83.1 - 87.8% | 6 |

400 DPI lost ground on all three verses and never won once. New errors appear at the
higher resolutions that are not present at 200 (`फ` read as `क`, a spurious `नि`
inserted), which looks like the input being resized away from what the model was
trained on.

**The model is fully deterministic.** Every repeat was byte-identical to its pair,
six times out of six. That means these gaps are real signal, not sampling noise, and
it also means you can measure training improvements precisely — a fixed input gives
a fixed output.

Action for us: we are staying at 200 DPI. We will not ask for higher.

### 2b. Isolated lines score better than full pages

Same verse, same model, same DPI, differing only in how much of the page was sent:

| Input | Accuracy |
|-------|----------|
| Full page (production path) | 91.6% |
| Cropped to the single verse | 94.6% |

Three points from cropping alone. If that holds generally it suggests the model is
losing effective resolution on dense pages, and it is the motivation for the region
endpoint requested in section 4.6.

---

## 3. The actual errors: systematic character confusions

Across all runs, every single-character substitution we saw:

| Read as | Should be | Count |
|---------|-----------|-------|
| त | ल | 8 |
| ् | ू | 4 |
| : | ः | 4 |
| र | न | 4 |
| ि | ी | 4 |
| ि | ् | 2 |
| क | फ | 2 |
| त | न | 2 |
| ु | ृ | 2 |

Plus one that appears at **every** resolution including 600 DPI:

| Read as | Should be | Note |
|---------|-----------|------|
| ब | व | Present at 150, 200, 300, 400 and 600 DPI |

These are minimal pairs — characters separated by one stroke or by the position of a
vowel mark. `ल`/`त`, `न`/`र`, `ी`/`ि`, `ृ`/`ु`. That is a discrimination problem in
the model, not an input quality problem, and it is what we would suggest oversampling
in the next training round.

The `व` to `ब` confusion is worth singling out. In our corpus `व्रण` (vraṇa, "wound")
is a disease name that appears throughout, and the model consistently renders it
`ब्रण`, which is not a word. A systematic error on a domain term is more damaging
than its frequency suggests, because it corrupts exactly the words users search for.

Two of these are probably not model problems at all:

- **`ः` emitted as ASCII colon `:`** — visarga is being normalised to a colon on
  output. The model is arguably reading it correctly. A post-processing fix on your
  side (or a documented guarantee that it will not happen) is cheaper than training.
- **`ि` / `्` / `ू` confusions** — these are matra placement rather than glyph
  identity, and may be a reordering issue in how the output is serialised rather than
  a recognition failure.

### 3b. Two smaller issues

**Nukta characters in Sanskrit.** We see `ड़` and other nukta forms (9 occurrences).
Nukta characters are Hindi and Urdu; they essentially never occur in Sanskrit. If the
model can be told the expected language, that whole class of error disappears. See
the `script` hint in section 4.7.

**Latin ligatures are being dropped.** In the English portions, `ffi`, `ff` and `fl`
ligatures lose a letter:

| Output | Should be | Count |
|--------|-----------|-------|
| Dificulty | Difficulty | 26 |
| Swel / Swelling | Swell / Swelling | 12 |
| Stifness | Stiffness | 4 |
| Refection | Reflection | 2 |
| Afect, Efect, Infamm | Affect, Effect, Inflamm | 3 |

47 tokens in one book. This matters more than it looks: these are silent failures. A
user searching "difficulty swallowing" gets nothing, because the index contains
"Dificulty". Unlike a garbled Sanskrit verse, nothing about the output looks wrong.

**IAST diacritics**: 72% retained (286 of 397). `Jṛmbhāṅga-mardō` comes back as
`Jrmbhânga-mardō` — `ṛ` to `r`, `ā` to `â`, `ṅ` to `n`.

---

## 4. What the service needs before we can integrate it properly

Current contract, as we use it today:

```
GET  /healthz
     -> {status, model_loaded, gpu_name, vram_used_gb, vram_total_gb,
         active_queue_size, total_jobs_tracked, timestamp}

POST /v1/parse        multipart: file=<png|pdf>
                      form:      pages, fast_mode, checkpoint
     -> {success, request_id, filename, pages_parsed, selected_pages,
         latency_sec, tokens, tokens_per_sec, markdown, raw_layout,
         annotated_image_url, annotated_image_urls}

Auth: X-API-Key header, not enforced when API_SECRET_KEY is unset server-side
```

What already works well and should not change: `model_loaded` in health (it lets us
fail fast at startup instead of failing every page), tables emitted as HTML markup,
`request_id` on every response, and `raw_layout` being available alongside the
markdown.

Requests below are ordered by how much they block us.

### 4.1 A stable base URL — blocking

The service is currently on a Cloudflare quick tunnel
(`*.trycloudflare.com`) whose hostname **changes every restart**. We cannot put a
rotating hostname in deployment config. Every rotation silently degrades us: our
provider health-probes at startup, fails, and falls back to Tesseract, which is far
worse for this script.

Needed: a named tunnel or a DNS name that survives restarts.

### 4.2 Async jobs with polling — blocking for production runs

Today every page is a blocking POST with a 300 second timeout. Our full corpus is 28
books, roughly 1,800 pages, about five hours of wall clock. Over that window a
blocking HTTP call per page is fragile: a tunnel drop loses in-flight work with no
way to recover the result, and we cannot restart our side without re-running pages.

Needed:

```
POST /v1/jobs           -> 202 {job_id}
GET  /v1/jobs/{job_id}  -> {status: queued|running|done|failed,
                            progress: {pages_done, pages_total},
                            result?, error?}
```

This lets us survive client restarts and tunnel drops, and show real progress.

### 4.3 Per-region confidence scores — highest value for quality

This is the single most useful thing you could add.

Right now parsing is all or nothing: we either keep the page's existing text layer or
replace it wholesale with model output. That cost us real accuracy — we replaced a
94%-correct text layer with 85%-correct OCR, because we had no way to see which
*parts* the model was unsure about.

With per-line (ideally per-token) confidence in `raw_layout`, we can merge: take the
model's output where it is confident and keep the original text where it is not. On
this corpus that alone would likely beat both sources.

Needed: a `confidence` field per region in `raw_layout`, 0-1, and a note on whether
it is calibrated.

### 4.4 Model version reporting

We record which embedding model produced every vector, so we can re-embed when the
model changes. We need the same for OCR and cannot currently get it.

Needed: `model_version` and the resolved `checkpoint` in both `/healthz` and the
`/v1/parse` response. Then we can store which checkpoint produced which text, and
re-run only the pages that predate an improvement.

Also: please document what values `checkpoint` accepts and what `latest` resolves to,
since pinning it is the only way we can get reproducible output.

### 4.5 Batch submission

One page per request at about 11.5 tokens/sec, with a round trip each time, leaves
the GPU idle between pages. A batch endpoint taking N images and returning N results
would improve throughput on a 1,800-page run.

If batching is added, please make the failure mode per-item rather than per-request.
We hit exactly this on another provider: a whole batch failing because of one bad
item cost us two complete runs.

### 4.6 Region parsing

Given section 2b, an endpoint that accepts bounding boxes and returns text per box
would help twice: better accuracy on dense pages, and it lets us keep the layout
analysis we already have from PyMuPDF rather than having the model redo it.

```
POST /v1/parse/regions   file + [{x0,y0,x1,y1}, ...]
     -> [{box, markdown, confidence}, ...]
```

### 4.7 A script or language hint

A parameter such as `script=devanagari-sanskrit` versus `devanagari-hindi` would let
the model exclude characters that cannot occur in the target language. For Sanskrit
that removes all nukta forms immediately, and probably helps the `व`/`ब` case too,
since relative frequency differs sharply between the two languages.

### 4.8 Documented limits and error semantics

We currently infer these by experiment. We treat 4xx as fatal and 5xx as retryable;
please confirm that is right.

Needed in writing: max file size, max image dimensions, supported concurrency, and
whether there is a rate limit. If there is, a 429 with `Retry-After` rather than a
500 would let us back off correctly instead of guessing.

### 4.9 Idempotency keys

When a request times out on our side we cannot tell whether the work completed. An
`Idempotency-Key` header, where a repeat returns the original result instead of
re-running, would let us retry safely without paying 77 seconds again.

---

## 5. Summary

What is working: layout, table extraction as HTML, structural fidelity (all 122
entries and every field came through), determinism, and a health endpoint that fails
fast.

What to fix in the model, in priority order:

1. `व` / `ब` discrimination — systematic, resolution-independent, hits a domain term
2. The minimal-pair confusions in section 3, led by `ल` / `त`
3. Latin `ffi` / `ff` / `fl` ligature loss — silent search failures
4. IAST diacritic retention, currently 72%
5. Visarga emitted as ASCII colon — likely post-processing, not training

What to fix in the service, in priority order:

1. Stable hostname (4.1)
2. Async jobs (4.2)
3. Per-region confidence (4.3)
4. Model version reporting (4.4)

What not to spend time on: higher input resolution. 200 DPI is already optimal and
400 is measurably worse.

Happy to re-run any of these measurements against a new checkpoint. We have the
122-verse ground truth set and the scoring harness, and since the model is
deterministic the comparison will be exact.
