#!/usr/bin/env python
"""
Reproducible accuracy harness for the Indic OCR service.

Scores the model against 122 hand-verified Sanskrit verses from
yogratnakar_slokas_part01.pdf and writes a per-verse report.

    python benchmark.py --url https://your-service --pdf yogratnakar_slokas_part01.pdf

Ground truth comes from the markdown the PDF was rendered from, so it is the
actual source text, not another OCR pass. The rendered page images are correct
and complete -- we verified the glyphs by eye -- so every difference the harness
reports is the model's, not the PDF's.

The model is deterministic (identical input gives byte-identical output), so a
change in these numbers is a real change in the model.

Outputs report.json with, for every verse: the ground truth, what the model
returned, the similarity, and the specific character substitutions.
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from difflib import SequenceMatcher

try:
    import pymupdf
    import requests
except ImportError:
    sys.exit("pip install pymupdf requests")

BANNER = re.compile(r"^#{1,6}\s*(?:\U0001F4C4|📄)?\s*Page\s+\d+\s*$", re.MULTILINE)
DEVA = re.compile(r"[ऀ-ॿ]")
# Hindi-only constructions that cannot occur in classical Sanskrit. Their presence
# is a reliable hallucination signal, not a transcription error.
HINDI_TELL = re.compile(r"के लिए|के साथ|किया गया|प्रतिनिधि|नहीं है")


def norm(s):
    return re.sub(r"[\s।|*>:]+", "", s or "")


def repetition_ratio(text, n=4):
    """Fraction of n-gram slots taken by the single most repeated n-gram."""
    w = (text or "").split()
    if len(w) < n * 2:
        return 0.0
    grams = Counter(" ".join(w[i:i + n]) for i in range(len(w) - n + 1))
    return grams.most_common(1)[0][1] / max(1, len(w) - n + 1)


def parse_one(url, png, api_key=None, dpi_note="", timeout=300):
    headers = {"X-API-Key": api_key} if api_key else {}
    t0 = time.time()
    r = requests.post(f"{url}/v1/parse",
                      files={"file": ("page.png", png, "image/png")},
                      data={"pages": "1", "fast_mode": "false"},
                      headers=headers, timeout=timeout)
    el = time.time() - t0
    if r.status_code != 200:
        return None, el, f"HTTP {r.status_code}"
    j = r.json()
    if not j.get("success", True):
        return None, el, "success=false"
    return BANNER.sub("", j.get("markdown") or "").strip(), el, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--truth", default="ground_truth_yogratnakar.json")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--dpi", type=int, default=200,
                    help="200 is measured-optimal; 400 scored 7 points worse")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--full-page", action="store_true",
                    help="send whole pages (production path) instead of verse crops")
    ap.add_argument("--out", default="report.json")
    args = ap.parse_args()

    truth = json.load(open(args.truth, encoding="utf-8"))
    if args.limit:
        truth = truth[:args.limit]
    doc = pymupdf.open(args.pdf)

    # one request per page when --full-page, so cache by page
    page_cache = {}
    rows, scores = [], []
    for i, item in enumerate(truth, 1):
        pno = item["page"]
        page = doc[pno - 1]
        if args.full_page:
            if pno not in page_cache:
                png = page.get_pixmap(dpi=args.dpi).tobytes("png")
                page_cache[pno] = parse_one(args.url, png, args.api_key)
            out, el, err = page_cache[pno]
        else:
            blk = next((b for b in page.get_text("blocks")
                        if DEVA.search(b[4])), None)
            if blk is None:
                continue
            clip = pymupdf.Rect(blk[0] - 6, blk[1] - 6, blk[2] + 6, blk[3] + 6)
            png = page.get_pixmap(dpi=args.dpi, clip=clip).tobytes("png")
            out, el, err = parse_one(args.url, png, args.api_key)

        if err:
            rows.append({**item, "error": err})
            print(f"{i:4d}/{len(truth)}  page {pno:3d}  ERROR {err}")
            continue

        gt = item["ground_truth"]
        best, bestline = 0.0, ""
        for line in (out or "").split("\n"):
            if not DEVA.search(line):
                continue
            r = SequenceMatcher(None, norm(gt), norm(line)).ratio()
            if r > best:
                best, bestline = r, line.strip()

        sm = SequenceMatcher(None, gt, bestline)
        subs = [{"expected": gt[a:b], "got": bestline[c:d]}
                for tag, a, b, c, d in sm.get_opcodes() if tag != "equal"]
        rep = repetition_ratio(bestline)
        hallucinated = bool(HINDI_TELL.search(bestline)) or rep > 0.25

        rows.append({**item, "model_output": bestline, "similarity": round(best, 4),
                     "substitutions": subs, "repetition_ratio": round(rep, 3),
                     "hallucination_suspected": hallucinated,
                     "latency_sec": round(el, 1)})
        scores.append(best)
        flag = "  <-- HALLUCINATION" if hallucinated else ""
        print(f"{i:4d}/{len(truth)}  page {pno:3d}  {best:6.1%}  {el:5.1f}s{flag}")

    ok = [r for r in rows if "similarity" in r]
    hall = [r for r in ok if r["hallucination_suspected"]]
    summary = {
        "verses_scored": len(ok),
        "mean_similarity": round(sum(scores) / max(1, len(scores)), 4),
        "exact": sum(1 for s in scores if s == 1.0),
        "at_least_95pct": sum(1 for s in scores if s >= 0.95),
        "below_60pct": sum(1 for s in scores if s < 0.60),
        "hallucinations": len(hall),
        "hallucinating_pages": sorted({r["page"] for r in hall}),
        "mode": "full_page" if args.full_page else "verse_crop",
        "dpi": args.dpi,
    }
    json.dump({"summary": summary, "verses": rows},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k:22s} {v}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
