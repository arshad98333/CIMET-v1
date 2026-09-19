from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def pcm_to_mp3(
    data: bytes,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    codec: str = "s16le",
) -> bytes | None:
    if not data:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "input.raw"
        mp3_path = Path(tmp) / "output.mp3"
        raw_path.write_bytes(data)
        command = [
            "ffmpeg",
            "-y",
            "-f",
            codec,
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-i",
            str(raw_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(mp3_path),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return None
        return mp3_path.read_bytes() if mp3_path.exists() else None
