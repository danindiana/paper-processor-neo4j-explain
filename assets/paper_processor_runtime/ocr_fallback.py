#!/usr/bin/env python3
"""
ocr_fallback.py — Local OCR fallback for scanned / image-only PDFs.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Shared by paper_processor.py and vram_resident_processor.py.

Zero cloud dependency: OCR runs locally through Tesseract, driven by
PyMuPDF's built-in OCR text-page (so no extra Python packages beyond the
`fitz` we already require — only the system `tesseract-ocr` binary + a
language data pack such as `tesseract-ocr-eng`).

Behaviour
  • Pages are extracted normally first (fast, lossless for born-digital PDFs).
  • A page whose stripped text is below `min_chars` is treated as image-only
    and re-read via OCR — but ONLY when OCR is available and the mode allows it.
  • If Tesseract is missing, the module degrades gracefully: it logs one
    warning and returns whatever native text exists, exactly like the old
    behaviour. Nothing crashes.
  • OCR output is cached to disk (keyed by paper hash + page index), so a
    re-run or `--reprocess` never pays the OCR cost twice.

Modes
  "auto"   — OCR only the pages that look empty/low-text   (default)
  "always" — OCR every page (use for known-bad scans with garbage text layers)
  "never"  — disable OCR entirely (original behaviour)
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import fitz  # pymupdf — already a hard dependency of the pipeline


# ── Tunable defaults (overridable via CLI / kwargs) ─────────────────────────
DEFAULT_MIN_CHARS = 100   # a page with fewer stripped chars is "probably scanned"
DEFAULT_DPI       = 300   # rasterisation DPI handed to Tesseract
DEFAULT_LANG      = "eng" # Tesseract language pack(s), e.g. "eng" or "eng+deu"


# Common Ubuntu / macOS tessdata locations, in priority order.
_TESSDATA_CANDIDATES = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/opt/homebrew/share/tessdata",
)


@dataclass
class OcrStats:
    """Per-document OCR accounting, surfaced to the caller for logging."""
    total_pages:   int  = 0
    ocr_pages:     int  = 0   # pages actually OCR'd this run
    cached_pages:  int  = 0   # pages served from the OCR cache
    native_pages:  int  = 0   # pages that had a usable native text layer
    skipped_blank: int  = 0   # pages still empty even after OCR (or OCR off)
    ocr_used:      bool = False
    notes:         List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ocr_used and self.ocr_pages == 0 and self.cached_pages == 0:
            return f"{self.native_pages}/{self.total_pages} pages had native text (no OCR needed)"
        bits = [f"{self.native_pages} native"]
        if self.ocr_pages:
            bits.append(f"{self.ocr_pages} OCR'd")
        if self.cached_pages:
            bits.append(f"{self.cached_pages} cached")
        if self.skipped_blank:
            bits.append(f"{self.skipped_blank} still-blank")
        return f"{self.total_pages} pages → " + ", ".join(bits)


# ── Availability detection ──────────────────────────────────────────────────
def _find_tessdata() -> Optional[str]:
    """Return a tessdata directory, honouring TESSDATA_PREFIX first."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env and os.path.isdir(env):
        return env
    for cand in _TESSDATA_CANDIDATES:
        if os.path.isdir(cand):
            return cand
    return None


def ocr_available(lang: str = DEFAULT_LANG) -> Tuple[bool, str]:
    """
    Check whether local OCR can run. Returns (ok, detail).
    Side effect: when OK, exports TESSDATA_PREFIX so fitz/Tesseract find the
    language packs even if the user never set it.
    """
    if shutil.which("tesseract") is None:
        return False, "tesseract binary not found in PATH (apt install tesseract-ocr)"

    tessdata = _find_tessdata()
    if not tessdata:
        return False, "tessdata directory not found (set TESSDATA_PREFIX)"

    # Verify each requested language pack is present.
    missing = [
        code for code in lang.split("+")
        if not os.path.isfile(os.path.join(tessdata, f"{code}.traineddata"))
    ]
    if missing:
        pkgs = " ".join(f"tesseract-ocr-{c}" for c in missing)
        return False, f"missing language data {missing} (apt install {pkgs})"

    os.environ["TESSDATA_PREFIX"] = tessdata
    return True, tessdata


# ── Core OCR primitives ───────────────────────────────────────────────────--
def page_needs_ocr(text: str, min_chars: int = DEFAULT_MIN_CHARS) -> bool:
    """A page looks scanned/image-only if its stripped text is below threshold."""
    return len(text.strip()) < min_chars


def ocr_page(page: "fitz.Page", dpi: int = DEFAULT_DPI, lang: str = DEFAULT_LANG) -> str:
    """
    OCR a single PyMuPDF page via its built-in Tesseract text page.
    `full=True` forces OCR of the whole page (correct for image-only scans).
    Returns "" on any failure rather than raising, so one bad page can't abort
    a whole corpus run.
    """
    try:
        tp = page.get_textpage_ocr(flags=0, language=lang, dpi=dpi, full=True)
        return page.get_text("text", textpage=tp).strip()
    except Exception:
        return ""


# ── Cache helpers ─────────────────────────────────────────────────────────--
def _cache_path(cache_dir: Path, paper_hash: str, idx: int) -> Path:
    return cache_dir / f"{paper_hash}_p{idx:04d}.txt"


# ── Main entry point ────────────────────────────────────────────────────────
def extract_pages_with_ocr(
    pdf_path: Path,
    *,
    mode: str = "auto",
    min_chars: int = DEFAULT_MIN_CHARS,
    dpi: int = DEFAULT_DPI,
    lang: str = DEFAULT_LANG,
    paper_hash: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    log=print,
) -> Tuple[List[str], OcrStats]:
    """
    Drop-in replacement for the old `extract_pages`, with OCR fallback.

    Returns (pages, stats) where `pages` is the list of non-empty page texts
    (same contract as before — blank pages are dropped) and `stats` is an
    OcrStats for the caller to log.

    Caching is enabled when BOTH `paper_hash` and `cache_dir` are supplied.
    """
    stats = OcrStats()
    if mode not in ("auto", "always", "never"):
        raise ValueError(f"invalid OCR mode {mode!r} (expected auto|always|never)")

    doc = fitz.open(str(pdf_path))
    stats.total_pages = doc.page_count

    # Resolve OCR availability once per document (only if the mode wants it).
    ocr_ok = False
    if mode != "never":
        ocr_ok, detail = ocr_available(lang)
        if not ocr_ok:
            stats.notes.append(f"OCR unavailable — {detail}")
            log(f"      ⚠️  OCR fallback disabled: {detail}")

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    out: List[str] = []
    for idx, page in enumerate(doc):
        native = page.get_text("text").strip()
        want_ocr = ocr_ok and (mode == "always" or page_needs_ocr(native, min_chars))

        if not want_ocr:
            if native:
                stats.native_pages += 1
                out.append(native)
            else:
                stats.skipped_blank += 1
            continue

        # --- OCR path (with optional disk cache) ---
        text = ""
        cached = False
        if cache_dir is not None and paper_hash:
            cp = _cache_path(cache_dir, paper_hash, idx)
            if cp.exists():
                text = cp.read_text(encoding="utf-8").strip()
                cached = True

        if not cached:
            text = ocr_page(page, dpi=dpi, lang=lang)
            if cache_dir is not None and paper_hash:
                _cache_path(cache_dir, paper_hash, idx).write_text(text, encoding="utf-8")

        stats.ocr_used = True
        if cached:
            stats.cached_pages += 1
        else:
            stats.ocr_pages += 1

        # OCR may still beat a genuinely-blank page; fall back to native if OCR empty.
        chosen = text or native
        if chosen:
            out.append(chosen)
        else:
            stats.skipped_blank += 1

    doc.close()
    return out, stats


# ── Manual smoke test:  python3 ocr_fallback.py some.pdf ────────────────────--
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        ok, detail = ocr_available()
        print(f"OCR available: {ok}  ({detail})")
        print("Usage: python3 ocr_fallback.py <file.pdf> [auto|always|never]")
        sys.exit(0)
    pages, st = extract_pages_with_ocr(
        Path(sys.argv[1]),
        mode=sys.argv[2] if len(sys.argv) > 2 else "auto",
    )
    print(st.summary())
    for i, p in enumerate(pages):
        print(f"\n──── page {i} ({len(p)} chars) ────\n{p[:400]}")
