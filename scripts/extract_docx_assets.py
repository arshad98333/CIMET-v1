from __future__ import annotations

import re
import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python scripts/extract_docx_assets.py <docx> <out-dir>")
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with ZipFile(src) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", xml)
        text = re.sub(r"\s+", " ", text).strip()
        (out / "doc3-text.txt").write_text(text, encoding="utf-8")
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
        for index, name in enumerate(media, 1):
            suffix = Path(name).suffix.lower() or ".bin"
            (out / f"doc3-diagram-{index:02d}{suffix}").write_bytes(archive.read(name))
    print(f"Extracted {len(media)} media file(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
