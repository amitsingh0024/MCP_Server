# Parser feedback round 2 — hallucination on Sanskrit verse lists

Date: 2026-11-14
Service: the Cloudflare quick tunnel in use on this date (hostname omitted — this
repository is public and the service enforces no auth)
Model: `unlimited-ocr2-indic-lora-v1` (base `baidu/Unlimited-OCR-2`)
From: OpenPDFSpecs (MCP knowledgebase) integration

Thank you for adding `model_version` and `resolved_checkpoint` to `/healthz` — that
was request 4.4 from the previous round and it is already useful. We can now record
which checkpoint produced which text.

This round has one new finding that matters more than everything in the last report
combined, plus the files you need to reproduce our numbers.

---

## 1. The headline: the model hallucinates on dense Sanskrit

On page 21 of our test book, the model did not misread the verse. It invented a
different one, in a different language, on a loop.

**Ground truth** (a list of herbs — trikatu, coriander, chavya, chitraka, gaja pippali):

```
त्र्यूषणं धान्यकं चव्यं चित्रकं गजपिप्पली। अजमोदा त्वगेला च ग्रन्थिकं च पलांशकम्
```

**What the model returned:**

```
प्रत्याशन के लिए प्रत्याशन के लिए प्रत्याशन के लिए प्रतिनिधि के लिए प्रतिनिधि
```

Two things are wrong here, and the second is the serious one.

1. **It is Hindi, not Sanskrit.** `के लिए` ("for") is a Hindi postpositional
   construction that does not exist in classical Sanskrit. `प्रतिनिधि`
   ("representative") is not a word in this text's vocabulary at all.
2. **It is a degenerate repetition loop.** The same phrase repeats until the
   generation stops. This is the classic VLM failure mode where the decoder loses
   the image grounding and falls back on language-model priors.

Similarity to ground truth: **31.7%**, against a 93% average everywhere else.

### Why this is worse than a character error

Every other issue we have reported is a *visible* error. `ब्रणानां` for `व्रणानां`
is wrong, but it is obviously wrong — it is not a word, and a reader or a validator
catches it.

This failure produces **clean, well-formed, grammatical Devanagari that is entirely
invented**. Nothing about it looks wrong. In a clinical knowledgebase, a herbal
formulation silently replaced by fluent filler is the most dangerous output the
system can produce: a practitioner searching for a formula would be served
confident nonsense with no signal that anything failed.

We would rather have an explicit failure than a fluent guess. See the request in
section 4.

### What we know and do not know

- It reproduces. The model is deterministic, so the same image gives the same
  output every time.
- We **cannot** say whether it is a regression. Page 21 was never measured on the
  previous checkpoint, and that service is gone, so there is no before to compare
  with. We are not claiming a trend we cannot evidence.
- On verses we *can* compare like for like, the new checkpoint is flat to slightly
  worse, not collapsing:

| Page | Previous checkpoint | This checkpoint | Change |
|------|--------------------|-----------------|--------|
| 19 | 94.6% | 94.6% | unchanged |
| 20 | 94.1% | 94.1% | unchanged |
| 22 | 91.3% | 87.8% | −3.5 |

So this is a localised, severe failure rather than a general degradation.

### Full pages time out at the tunnel (HTTP 524)

We tried to confirm whether the same page hallucinates when sent as a **whole page**
rather than a cropped line, because the whole page is what our ingest actually sends.
We could not, because the request never completes:

```
full page 21, 200 dpi, 325 KB
attempt 1: HTTP 524 after 126s   (content-type: text/html - Cloudflare error page)
attempt 2: HTTP 524 after 126s
```

524 is Cloudflare's "origin did not respond in time". The quick tunnel cuts the
connection at roughly 120 seconds, so any page the model takes longer than that to
process is unreachable to us — we get an HTML error page instead of JSON.

Two consequences:

1. **It interacts badly with the hallucination.** Normal pages in our run completed
   in 70-90s, comfortably inside the limit. Page 21 — the page that sends the model
   into a repetition loop — exceeded 126s twice. A degenerate loop generates until it
   hits a stop condition, so the pages most likely to produce bad output are also the
   ones most likely to time out.
2. **The failure is silent and lands on the worst possible fallback.** Our client
   treats 5xx as retryable, retries, fails again, and falls back to the PDF's existing
   text layer — which on this corpus is the corrupted one we were using OCR to
   replace. So a timeout silently reverts that page to broken text.

This is a strong argument for both of the infrastructure requests in section 4.3: a
stable host without a 120s proxy limit, and an async job API where a slow page is
polled rather than held open on a connection.

### The pattern worth investigating

The verse that failed is a **list** — eleven herb names in sequence with no
connecting grammar. The verses that scored 94% are normal sentences. Our hypothesis
is that list-like input gives the decoder weak sequential grounding, so it slips
into repetition. Dense tabular or enumerated content would be the thing to probe
in training data.

---

## 2. Confusions from round 1 are unchanged

Every systematic substitution we reported is still present at the same rate:

| Got | Expected | Note |
|-----|----------|------|
| ब | व | still at every DPI; corrupts `व्रण` (wound), a disease name throughout this corpus |
| त | ल | most frequent substitution |
| र | न | |
| ि | ी | |
| ु | ृ | |
| : | ः | visarga emitted as ASCII colon — a post-processing fix, not a training one |

Resolution is still not the lever: 200 DPI scores 93.3%, 400 DPI scores 86.2%, and
400 lost on every verse tested. Please do not optimise for higher-resolution input.

---

## 3. Files in this folder

| File | What it is |
|------|------------|
| `ground_truth_yogratnakar.json` | 122 hand-verified Sanskrit verses with page numbers |
| `benchmark.py` | Reproducible harness — scores the service against that set |

The ground truth is the markdown the PDF was *rendered from*, so it is the actual
source text rather than another OCR pass. We verified the rendered page images by
eye: the glyphs are correct and complete, so every difference the harness reports
is the model's, not the document's.

Run it:

```
python benchmark.py --url https://your-service --pdf yogratnakar_slokas_part01.pdf
```

It writes `report.json` with, per verse: ground truth, model output, similarity,
the exact character substitutions, a repetition ratio, and a hallucination flag.
Add `--full-page` to test the production path (whole pages) rather than verse crops.

Because the model is deterministic, any change in these numbers is a real change in
the model, not sampling noise. That makes this a usable regression gate.

---

## 4. What we are asking for

Ordered by how much it blocks us.

### 4.1 Do not emit a fluent guess — fail loudly

This is now our top request, ahead of everything in round 1.

When the decoder loses grounding, we need the response to say so rather than
returning invented text. Any of these would work:

- a `confidence` field we can threshold on (this was request 4.3 last round and is
  now urgent rather than valuable)
- a repetition / degeneration flag computed server-side
- `success: false` with a reason, which our client already handles by keeping the
  existing text layer

We can detect obvious cases ourselves — `benchmark.py` flags Hindi constructions
and a high repetition ratio — but a client-side heuristic over fluent output is a
poor substitute for the model's own uncertainty.

### 4.2 A script or language constraint

A `script=devanagari-sanskrit` parameter would make this specific failure
impossible: `के लिए` and `प्रतिनिधि` are Hindi and could be excluded outright. It
would also remove the nukta characters (`ड़`) we reported last round, which never
occur in Sanskrit.

### 4.3 Carried over from round 1, still needed

- **Stable hostname.** The tunnel URL has now rotated twice. Each rotation silently
  degrades us to a much worse OCR engine until someone notices.
- **Async jobs with polling.** Our corpus is 5,384 pages, about 107 hours of OCR. A
  blocking POST per page over a tunnel is not viable at that duration.
- **Batch submission**, with per-item rather than per-request failure.

---

## 5. Reproducing the hallucination

```
pdf   : yogratnakar_slokas_part01.pdf   (sha256 24a73c7a33c8...)
page  : 21
dpi   : 200
mode  : fast_mode=false
```

Send the **first Devanagari text block** on that page, cropped — not the whole page.
`benchmark.py --limit 25` does exactly this and reaches it in a couple of minutes.

Use the cropped form to reproduce it: the whole-page request for this page does not
return at all through the tunnel, it 524s after 126s (section 1). Running it on your
own host without the proxy in front should show what the full-page path actually
produces, which is the one measurement we were unable to take.

Round-1 note that still holds: cropping to a single line scored ~3 points better than
sending the whole page (94.6% vs 91.6% on page 19), so the two paths do differ.

We are glad to re-run the full 122-verse set against any new checkpoint and send
back the report — it costs us a few minutes and gives you an exact before/after.
