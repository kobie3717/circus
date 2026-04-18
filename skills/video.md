---
name: video
description: >
  Free video creation skill. Builds voiced MP4 slideshows from text using
  Pexels (free images) + gTTS (Google TTS) + FFmpeg. Zero API cost.
  Use for: WhatsAuction item previews, product clips, announcement videos.
---

# /video — Free Video Creation Skill

## What it does

Text + keywords → voiced MP4 slideshow. No paid APIs. No Kling. No ElevenLabs.

**Stack (100% free):**
| Tool | Role | Cost |
|------|------|------|
| [Pexels API](https://www.pexels.com/api/) | Stock images | Free (20K req/mo) |
| gTTS | Google TTS voiceover | Free (no key) |
| FFmpeg | Video assembly + Ken Burns | Free |

---

## Setup (one-time)

```bash
pip install gtts requests pillow
# Get free Pexels key at https://www.pexels.com/api/
export PEXELS_API_KEY="your-key-here"
```

---

## Usage

### Python API
```python
from circus.services.video_pipeline import build_video

result = build_video(
    title="Lot 42 — 2019 BMW 3-Series",
    description="Excellent condition, 45,000 km. Bidding starts at R85,000. Auction closes Friday 8 PM.",
    keywords="BMW luxury car sedan",
    output_path="/tmp/lot42-preview.mp4",
    pexels_api_key="your-key",  # or set PEXELS_API_KEY env var
)
# {"ok": True, "path": "/tmp/lot42-preview.mp4", "duration": 15.2, "size_kb": 2840.0}
```

### CLI
```bash
python3 -m circus.services.video_pipeline \
  --title "Lot 42 — BMW 3-Series" \
  --description "Excellent condition, 45,000 km. Bidding starts at R85,000." \
  --keywords "BMW car sedan" \
  --output /tmp/lot42.mp4 \
  --key "$PEXELS_API_KEY"
```

---

## WhatsAuction Integration

Generate item preview videos automatically when a lot is listed:

```python
from circus.services.video_pipeline import build_video

def generate_auction_preview(lot: dict) -> str:
    """Create MP4 preview for a WhatsAuction lot."""
    result = build_video(
        title=f"Lot {lot['number']} — {lot['title']}",
        description=lot["description"],
        keywords=lot["category"],
        output_path=f"/tmp/lot-{lot['number']}.mp4",
    )
    return result["path"]
```

Then send the video via Baileys/WhatsApp:
```js
await sock.sendMessage(jid, {
  video: fs.readFileSync('/tmp/lot42.mp4'),
  caption: '🔨 *Lot 42* — BMW 3-Series\nBidding opens NOW. Reply with your bid!'
})
```

---

## Pipeline stages

```
keywords ──► Pexels API ──► 4 JPG images
                                │
text ──────► gTTS ─────────► MP3 narration
                                │
              FFmpeg ◄──────────┘
                │  (Ken Burns zoom + concat)
                ▼
           output.mp4 (HD 1280×720)
```

---

## Limits & notes

- Pexels free: 200 req/hour, 20,000 req/month — more than enough
- gTTS requires internet (calls Google Translate TTS endpoint)
- No gTTS API key needed — completely free
- Videos are ~12–20s depending on description length
- Output: H.264 MP4, 1280×720, AAC audio — WhatsApp compatible
- FFmpeg zoompan is slow on first run — ~10–30s generation time

---

## Language support (gTTS)

```python
# Afrikaans for SA market
build_video(..., lang="af")

# English (default)
build_video(..., lang="en")
```

---

## Future phases (not free)

| Phase | Addition | Cost |
|-------|----------|------|
| 2 | FLUX stills (AI generated) | ~$0.03/image |
| 3 | Kling motion clips | ~$1.33/60s |
| 4 | ElevenLabs voice | ~$0.15/min |

Start free. Upgrade per use case when revenue justifies it.
