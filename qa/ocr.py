"""OCR for scanned PDFs and images (tesseract). Optional: returns '' if tesseract is unavailable.

Setup (Colab): apt-get install tesseract-ocr tesseract-ocr-fra poppler-utils; pip install pytesseract pypdfium2
"""
from __future__ import annotations

from pathlib import Path

LANGS = "eng+fra"  # falls back to eng if a language pack is missing


def available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def _langs() -> str:
    import pytesseract

    have = set(pytesseract.get_languages())
    use = [l for l in LANGS.split("+") if l in have]
    return "+".join(use) or "eng"


def ocr_image(path: Path) -> str:
    import pytesseract
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("L")
        if max(im.size) < 1600:  # upscale small images; tesseract likes ~300 dpi
            f = 1600 / max(im.size)
            im = im.resize((int(im.width * f), int(im.height * f)))
        return pytesseract.image_to_string(im, lang=_langs()).strip()


def ocr_pdf(path: Path, max_pages: int = 5, scale: float = 2.5) -> str:
    import pypdfium2 as pdfium
    import pytesseract

    pdf = pdfium.PdfDocument(str(path))
    out = []
    for i in range(min(len(pdf), max_pages)):
        img = pdf[i].render(scale=scale).to_pil().convert("L")
        out.append(f"[page {i + 1}]\n" + pytesseract.image_to_string(img, lang=_langs()).strip())
    return "\n".join(out)


def ocr_file(path: Path, max_pages: int = 5) -> str:
    """OCR text for a .jpg/.png/.pdf, or '' if OCR is unavailable or fails."""
    if not available():
        return ""
    try:
        if path.suffix.lower() == ".pdf":
            return ocr_pdf(path, max_pages)
        return ocr_image(path)
    except Exception as e:  # noqa: BLE001
        return f"[ocr failed: {type(e).__name__}: {str(e)[:100]}]"
