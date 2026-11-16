#!/usr/bin/env python
"""
Build an OCR training/eval dataset by pairing each PDF with the markdown it was
rendered from, aligned down to the page.

    python scripts/build_ocr_dataset.py --pdfs <dir> --markdown <dir> --out <dir>

Why alignment is needed: the markdown is document-level, but OCR training wants
text tied to a specific page image. Both files come from the same row source and
keep the same record order, and each record's `... — Shloka N.N` header is ASCII
English that survives the PDFs' broken font encoding. So the headers are a
reliable join key: extract them per page from the PDF, extract them in order from
the markdown, and align the two sequences.

Anything not confidently aligned is dropped rather than guessed. A wrong
page/text pair is worse than a missing one for training.
"""
import argparse
import difflib
import json
import os
import re
from collections import defaultdict

import pymupdf

HDR_PDF = re.compile(r"^(.*?—\s*Shloka\s*[\d.]+)\s*$", re.MULTILINE)
HDR_MD = re.compile(r"^###\s*(.+?—\s*Shloka\s*[\d.]+)\s*$", re.MULTILINE)
DEVA = re.compile(r"[ऀ-ॿ]")


def pdf_is_readable(path, sample=12):
    """
    (ok, reason). False when the pages carry no Devanagari glyphs to read.

    Sampling the text layer is enough to tell: a PDF whose Sanskrit renders as
    .notdef boxes has no Devanagari codepoints either, because the font that would
    carry them was never embedded.
    """
    doc = pymupdf.open(path)
    n = len(doc)
    step = max(1, n // sample)
    deva = 0
    unembedded = set()
    for i in range(0, n, step):
        deva += len(DEVA.findall(doc[i].get_text()))
        for f in doc[i].get_fonts():
            if f[1] == "n/a":
                unembedded.add(f[3].split("+")[-1])
    doc.close()
    if deva == 0:
        detail = (f"; fonts not embedded: {', '.join(sorted(unembedded))}"
                  if unembedded else "")
        return False, ("no Devanagari glyphs on the page — renders as .notdef boxes"
                       + detail)
    return True, ""


def pdf_headers(path):
    """[(title, page_number)] in document order."""
    out = []
    doc = pymupdf.open(path)
    for i in range(len(doc)):
        for m in HDR_PDF.finditer(doc[i].get_text()):
            out.append((m.group(1).strip(), i + 1))
    n_pages = len(doc)
    doc.close()
    return out, n_pages


def md_records(path):
    """[{title, sanskrit, meaning, description, specification}] in document order."""
    text = open(path, encoding="utf-8").read()
    hits = list(HDR_MD.finditer(text))
    recs = []
    for i, m in enumerate(hits):
        body = text[m.end():hits[i + 1].start() if i + 1 < len(hits) else len(text)]

        def field(label):
            fm = re.search(rf"\*\*{label}:\*\*\s*\n?>?\s*(.+?)(?=\n\s*\n|\n\*\*|\Z)",
                           body, re.S)
            return fm.group(1).strip() if fm else None

        sanskrit = field("Sanskrit")
        recs.append({
            "title": m.group(1).strip(),
            "sanskrit": sanskrit,
            "meaning": field("Meaning"),
            "description": field("Description"),
            "specification": field("Specification"),
        })
    return recs


def align(pdf_hdrs, recs):
    """Map markdown record index -> pdf page, by aligning the two title sequences."""
    a = [t for t, _ in pdf_hdrs]
    b = [r["title"] for r in recs]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    page_of = {}
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            page_of[j + k] = pdf_hdrs[i + k][1]
    return page_of, sm.ratio()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", required=True)
    ap.add_argument("--markdown", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    summary = []

    pdfs = sorted(f for f in os.listdir(args.pdfs) if f.lower().endswith(".pdf"))
    for fn in pdfs:
        stem = os.path.splitext(fn)[0]
        # yogratnakar_slokas_part01.pdf <-> yogratnakar_part01.md
        cands = [f"{stem}.md", f"{stem.replace('_slokas', '')}.md"]
        md_path = next((os.path.join(args.markdown, c) for c in cands
                        if os.path.exists(os.path.join(args.markdown, c))), None)
        if not md_path:
            print(f"{fn:34s} NO MARKDOWN -- skipped")
            summary.append({"pdf": fn, "status": "no_markdown"})
            continue

        # A PDF whose pages carry no Devanagari glyphs at all must be excluded, not
        # merely scored low. charak_part02 was generated with non-embedded Helvetica,
        # which has no Devanagari coverage, so every Sanskrit glyph fell back to
        # .notdef and the page renders as empty boxes. Pairing those images with
        # correct Sanskrit would train the model to produce text from nothing --
        # teaching exactly the hallucination we are trying to remove.
        readable, reason = pdf_is_readable(os.path.join(args.pdfs, fn))
        if not readable:
            print(f"{fn:34s} EXCLUDED -- {reason}")
            summary.append({"pdf": fn, "status": "excluded", "reason": reason})
            continue

        hdrs, n_pages = pdf_headers(os.path.join(args.pdfs, fn))
        recs = md_records(md_path)
        page_of, ratio = align(hdrs, recs)

        verses, by_page = [], defaultdict(list)
        for idx, r in enumerate(recs):
            page = page_of.get(idx)
            if page is None or not r["sanskrit"] or not DEVA.search(r["sanskrit"]):
                continue
            verses.append({"id": len(verses) + 1, "page": page, **r})
            by_page[page].append(r["sanskrit"])

        json.dump({
            "pdf": fn,
            "markdown": os.path.basename(md_path),
            "pdf_pages": n_pages,
            "markdown_records": len(recs),
            "pdf_headers": len(hdrs),
            "aligned_verses": len(verses),
            "sequence_similarity": round(ratio, 4),
            "verses": verses,
            "pages": {str(p): v for p, v in sorted(by_page.items())},
        }, open(os.path.join(args.out, f"{stem}.json"), "w", encoding="utf-8"),
            ensure_ascii=False, indent=1)

        pct = len(verses) / max(1, len(recs))
        print(f"{fn:34s} pages={n_pages:4d}  records={len(recs):5d}  "
              f"aligned={len(verses):5d} ({pct:5.1%})")
        summary.append({"pdf": fn, "markdown": os.path.basename(md_path),
                        "pdf_pages": n_pages, "markdown_records": len(recs),
                        "aligned_verses": len(verses),
                        "aligned_pct": round(pct, 4),
                        "sequence_similarity": round(ratio, 4),
                        "status": "ok"})

    tot_r = sum(s.get("markdown_records", 0) for s in summary)
    tot_a = sum(s.get("aligned_verses", 0) for s in summary)
    tot_p = sum(s.get("pdf_pages", 0) for s in summary)
    json.dump({"books": len(summary), "pdf_pages": tot_p,
               "markdown_records": tot_r, "aligned_verses": tot_a,
               "aligned_pct": round(tot_a / max(1, tot_r), 4),
               "per_book": summary},
              open(os.path.join(args.out, "_summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n{'TOTAL':34s} pages={tot_p:4d}  records={tot_r:5d}  "
          f"aligned={tot_a:5d} ({tot_a/max(1,tot_r):5.1%})")


if __name__ == "__main__":
    main()
