"""Reading a resume PDF as an image when its text layer is missing or garbled (a scan, a
design-tool export, a PDF whose characters map to nothing): each page is rendered and read with
Tesseract OCR, all locally. Optional: it needs the ``tesseract`` program and ``pypdfium2``
(``pip install 'jobhunt[ocr]'``); without them nothing changes.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DPI = 300
MAX_PAGES = 4  # a resume; a 40-page PDF isn't one
TIMEOUT_S = 90


# Degree abbreviations OCR misreads ("M.S." -> "N.5.", "Ph.D." -> "Ph.0."): fixed only in OCR text.
_FIXES = [
    (re.compile(r"\b[MN]\.\s?[5S]\.(?=\s)"), "M.S."),
    (re.compile(r"\bB\.\s?5\.(?=\s)"), "B.S."),
    (re.compile(r"\bPh\.\s?[0O]\.(?=\s|,|$)"), "Ph.D."),
    (re.compile(r"\bM\.\s?8\.\s?A\.?"), "M.B.A."),
]


def fix_ocr(text: str) -> str:
    for rx, rep in _FIXES:
        text = rx.sub(rep, text)
    return text


def unavailable() -> str | None:
    """Why OCR can't run here, or None."""
    if shutil.which("tesseract") is None:
        return "the tesseract program isn't installed (macOS: `brew install tesseract`)"
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return "pypdfium2 isn't installed (`pip install 'jobhunt[ocr]'`)"
    return None


def _pgm(width: int, height: int, stride: int, buf: bytes) -> bytes:
    """An 8-bit grayscale bitmap as a binary PGM (Tesseract reads it directly)."""
    rows = (
        buf
        if stride == width
        else b"".join(bytes(buf[r * stride : r * stride + width]) for r in range(height))
    )
    return f"P5\n{width} {height}\n255\n".encode() + bytes(rows)


def ocr_pdf(path: Path) -> str:
    """The PDF's text as Tesseract reads its rendered pages. Raises RuntimeError when OCR isn't
    available or fails."""
    if why := unavailable():
        raise RuntimeError(why)
    import pypdfium2 as pdfium

    exe = shutil.which("tesseract") or "tesseract"
    pdf = pdfium.PdfDocument(str(path))
    texts: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="jobhunt-ocr-") as tmp:
            for i in range(min(len(pdf), MAX_PAGES)):
                bm = pdf[i].render(scale=DPI / 72, grayscale=True)
                img = Path(tmp) / f"p{i}.pgm"
                img.write_bytes(_pgm(bm.width, bm.height, bm.stride, bytes(bm.buffer)))
                proc = subprocess.run(
                    [exe, str(img), "stdout", "--dpi", str(DPI)],
                    capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
                )  # fmt: skip
                if proc.returncode != 0:
                    raise RuntimeError(f"tesseract failed: {proc.stderr.strip()[:200]}")
                texts.append(proc.stdout)
    finally:
        pdf.close()
    return fix_ocr("\n".join(texts))
