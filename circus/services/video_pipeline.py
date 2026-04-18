"""
Free Video Pipeline Service
Stack: Pexels (free images) + gTTS (Google TTS) + FFmpeg (assembly)
Cost: $0.00

Usage:
    from circus.services.video_pipeline import build_video
    result = build_video(
        title="Lot 42 — BMW 3-Series",
        description="Excellent condition. Bidding starts at R85,000.",
        keywords="BMW car sedan",
        output_path="/tmp/lot42.mp4",
        pexels_api_key="your-free-key"
    )
    print(result)  # {"ok": True, "path": "/tmp/lot42.mp4", "duration": 12.0}
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# Stage 1: Fetch free images from Pexels
# ──────────────────────────────────────────────

def fetch_pexels_images(
    query: str,
    count: int = 4,
    api_key: str = "",
    dest_dir: str = "/tmp",
) -> list[str]:
    """
    Download free stock images from Pexels API.
    Free tier: 200 req/hour, 20,000 req/month — no cost.
    Get your key at https://www.pexels.com/api/
    """
    try:
        import requests
    except ImportError:
        raise RuntimeError("pip install requests")

    if not api_key:
        api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        raise ValueError("PEXELS_API_KEY not set. Get free key at pexels.com/api")

    headers = {"Authorization": api_key}
    params = {"query": query, "per_page": count, "orientation": "landscape"}
    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers=headers,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    photos = resp.json().get("photos", [])
    if not photos:
        raise RuntimeError(f"No Pexels images found for query: {query!r}")

    paths = []
    for i, photo in enumerate(photos[:count]):
        url = photo["src"]["large"]
        img_path = os.path.join(dest_dir, f"img_{i:02d}.jpg")
        img_resp = requests.get(url, timeout=30)
        img_resp.raise_for_status()
        with open(img_path, "wb") as f:
            f.write(img_resp.content)
        paths.append(img_path)

    return paths


# ──────────────────────────────────────────────
# Stage 2: Generate voiceover with gTTS (free)
# ──────────────────────────────────────────────

def generate_voiceover(
    text: str,
    output_path: str = "/tmp/narration.mp3",
    lang: str = "en",
    slow: bool = False,
) -> str:
    """
    Convert text to MP3 using Google TTS (gTTS).
    Free — no API key required. Uses Google Translate TTS endpoint.
    pip install gtts
    """
    try:
        from gtts import gTTS
    except ImportError:
        raise RuntimeError("pip install gtts")

    tts = gTTS(text=text, lang=lang, slow=slow)
    tts.save(output_path)
    return output_path


# ──────────────────────────────────────────────
# Stage 3: Assemble slideshow with FFmpeg (free)
# ──────────────────────────────────────────────

def build_slideshow(
    image_paths: list[str],
    audio_path: str,
    output_path: str = "/tmp/output.mp4",
    image_duration: float = 3.5,
    fps: int = 25,
    resolution: str = "1280x720",
) -> dict:
    """
    Assemble images + audio into MP4 using FFmpeg.
    Uses zoompan (Ken Burns effect) for visual interest.
    """
    if not image_paths:
        raise ValueError("No images provided")

    w, h = resolution.split("x")

    # Write concat list
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        for img in image_paths:
            f.write(f"file '{img}'\n")
            f.write(f"duration {image_duration}\n")
        # FFmpeg concat needs last image repeated
        f.write(f"file '{image_paths[-1]}'\n")
        concat_file = f.name

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-i", audio_path,
        "-vf", (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"zoompan=z='min(zoom+0.0015,1.5)':d={fps}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}"
        ),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(concat_file)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-1000:]}")

    # Get duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", output_path],
        capture_output=True, text=True,
    )
    duration = 0.0
    if probe.returncode == 0:
        import json
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 0))

    return {
        "ok": True,
        "path": output_path,
        "duration": round(duration, 1),
        "size_kb": round(Path(output_path).stat().st_size / 1024, 1),
    }


# ──────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────

def build_video(
    title: str,
    description: str,
    keywords: str,
    output_path: str = "/tmp/output.mp4",
    pexels_api_key: str = "",
    image_count: int = 4,
    lang: str = "en",
    tmpdir: str = "/tmp",
) -> dict:
    """
    Full free pipeline: keywords → Pexels images + gTTS audio → FFmpeg MP4.

    Args:
        title:           Spoken first (e.g. "Lot 42 — BMW 3-Series")
        description:     Narration body (1-3 sentences)
        keywords:        Pexels image search query
        output_path:     Where to save the MP4
        pexels_api_key:  Free Pexels API key (or set PEXELS_API_KEY env var)
        image_count:     Number of images to use (default 4)
        lang:            TTS language code (default "en")
        tmpdir:          Temp dir for intermediate files

    Returns:
        {"ok": True, "path": str, "duration": float, "size_kb": float}
    """
    narration = f"{title}. {description}"

    # Stage 1: Images
    images = fetch_pexels_images(
        query=keywords,
        count=image_count,
        api_key=pexels_api_key,
        dest_dir=tmpdir,
    )

    # Stage 2: Voiceover
    audio_path = os.path.join(tmpdir, "narration.mp3")
    generate_voiceover(text=narration, output_path=audio_path, lang=lang)

    # Stage 3: Assemble
    result = build_slideshow(
        image_paths=images,
        audio_path=audio_path,
        output_path=output_path,
    )

    # Cleanup temp images + audio
    for img in images:
        try:
            os.unlink(img)
        except OSError:
            pass
    try:
        os.unlink(audio_path)
    except OSError:
        pass

    return result


# ──────────────────────────────────────────────
# CLI entry
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Free video pipeline")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--output", default="/tmp/output.mp4")
    parser.add_argument("--key", default="", help="Pexels API key")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--count", type=int, default=4)
    args = parser.parse_args()

    print("🎬 Building video...")
    res = build_video(
        title=args.title,
        description=args.description,
        keywords=args.keywords,
        output_path=args.output,
        pexels_api_key=args.key,
        image_count=args.count,
        lang=args.lang,
    )
    print(f"✅ Done: {res['path']} ({res['duration']}s, {res['size_kb']}KB)")
