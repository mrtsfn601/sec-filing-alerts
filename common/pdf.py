"""PDF -> text via poppler's pdftotext. House and OGE PTRs are PDFs."""

import os
import subprocess
import tempfile


def pdf_to_text(raw, timeout=60):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        out = subprocess.run(["pdftotext", "-raw", path, "-"],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout
    finally:
        os.unlink(path)
