"""Daily kids nursery-rhyme video agent: Claude writes -> TTS sings -> images -> ffmpeg -> YouTube."""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "out" / date.today().isoformat()
HISTORY = ROOT / "history.json"
MUSIC_DIR = ROOT / "assets" / "music"

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
VOICE = os.getenv("TTS_VOICE", "en-US-AnaNeural")  # child-like voice
PRIVACY = os.getenv("YT_PRIVACY", "private")  # private | unlisted | public
W, H = 1920, 1080

STYLE = ("bright cheerful children's picture-book illustration, soft rounded shapes, "
         "vivid pastel colors, friendly cartoon characters, no text, no scary elements")


def load_history() -> list[str]:
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else []


def write_rhyme(past: list[str]) -> dict:
    client = anthropic.Anthropic()
    prompt = f"""Write an ORIGINAL nursery rhyme for toddlers (ages 2-5). Do not copy any existing
copyrighted song or rhyme. Simple words, strong rhythm, a repeating chorus-like line, gentle positive
message (counting, colors, animals, kindness, bedtime, etc.).

Already made (pick a different topic): {past[-60:]}

Reply with ONLY JSON:
{{"topic": "...", "title": "YouTube title under 70 chars, kid-friendly, ends with '| Nursery Rhyme for Kids'",
 "description": "2-3 sentence parent-friendly description with the full lyrics below it",
 "tags": ["10 tags"],
 "character": "one-sentence consistent visual description of the main character(s) reused in every scene",
 "verses": [{{"lyrics": "2-4 short lines", "scene": "what the illustration shows"}}]}}
Use 5 to 6 verses."""
    msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=8000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = text[text.index("{"): text.rindex("}") + 1]
    return json.loads(text)


def make_image(prompt: str, path: Path):
    for attempt in range(1, 8):
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={"model": os.getenv("IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
                  "size": "1536x1024", "n": 1,
                  "quality": os.getenv("IMAGE_QUALITY", "medium")},
            timeout=300,
        )
        if r.status_code not in (429, 500, 502, 503) or "insufficient_quota" in r.text or attempt == 7:
            break
        wait = 20 * attempt
        print(f"image API {r.status_code}, retrying in {wait}s: {r.text[:200]}")
        time.sleep(wait)
    if not r.ok:
        print("image API error body:", r.text[:500])
    r.raise_for_status()
    import base64
    raw = base64.b64decode(r.json()["data"][0]["b64_json"])
    path.with_suffix(".raw.png").write_bytes(raw)
    img = Image.open(path.with_suffix(".raw.png")).convert("RGB")
    scale = max(W / img.width, H / img.height)  # cover-crop to 16:9
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - W) // 2, (img.height - H) // 2
    img.crop((left, top, left + W, top + H)).save(path)


def font(size: int):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def add_lyrics(path: Path, lyrics: str):
    img = Image.open(path).convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font(64)
    lines = lyrics.split("\n")
    band_h = 90 * len(lines) + 40
    d.rectangle((0, H - band_h, W, H), fill=(0, 0, 0, 150))
    for i, line in enumerate(lines):
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, H - band_h + 25 + i * 90), line, font=f, fill="white")
    Image.alpha_composite(img, layer).convert("RGB").save(path)


async def make_audio(text: str, path: Path):
    flat = text.replace("\n", " ... ")
    await edge_tts.Communicate(flat, VOICE, rate="-15%", pitch="+8Hz").save(str(path))


VERSE_MS = int(os.getenv("VERSE_MS", "14000"))


def make_song(meta: dict, path: Path) -> bool:
    """Generate a sung version with ElevenLabs Music. Returns False if unavailable/failed."""
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return False
    styles = ["children's nursery rhyme", "cheerful sung female vocal", "simple catchy melody",
              "gentle ukulele, glockenspiel and soft percussion", "about 100 bpm"]
    chunks = [{"text": f"[Verse {i + 1}]\n{v['lyrics']}", "duration_ms": VERSE_MS,
               "positive_styles": styles, "negative_styles": ["dark", "aggressive", "spoken word"]}
              for i, v in enumerate(meta["verses"])]
    try:
        r = requests.post("https://api.elevenlabs.io/v1/music",
                          params={"output_format": "mp3_44100_128"},
                          headers={"xi-api-key": key},
                          json={"composition_plan": {"chunks": chunks},
                                "model_id": os.getenv("ELEVENLABS_MODEL", "music_v2_5")},
                          timeout=600)
        r.raise_for_status()
        path.write_bytes(r.content)
        return True
    except Exception as e:
        resp = getattr(e, "response", None)
        body = resp.text[:500] if resp is not None else ""
        print("ElevenLabs song failed, falling back to TTS:", e, body)
        return False


def build_sung_video(images: list[Path], song: Path, out: Path) -> Path:
    clips = []
    for i, img in enumerate(images):
        clip = OUT / f"clip{i}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(img),
            "-vf", f"zoompan=z='min(zoom+0.0005,1.12)':d={VERSE_MS * 25 // 1000}:s={W}x{H}:fps=25,format=yuv420p",
            "-t", f"{VERSE_MS / 1000:.2f}", "-an", "-c:v", "libx264", str(clip)],
            check=True, capture_output=True)
        clips.append(clip)
    listing = OUT / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips))
    joined = OUT / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", str(joined)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(joined), "-i", str(song), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)],
                   check=True, capture_output=True)
    return out


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def build_video(scenes: list[tuple[Path, Path]], out: Path) -> Path:
    clips = []
    for i, (img, aud) in enumerate(scenes):
        dur = duration(aud) + 1.0
        clip = OUT / f"clip{i}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(aud),
            "-vf", f"zoompan=z='min(zoom+0.0005,1.12)':d={int(dur*25)}:s={W}x{H}:fps=25,format=yuv420p",
            "-af", "apad", "-t", f"{dur:.2f}", "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            "-ac", "2", str(clip)], check=True, capture_output=True)
        clips.append(clip)
    listing = OUT / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips))
    joined = OUT / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", str(joined)], check=True, capture_output=True)
    tracks = sorted(MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        joined.rename(out)
        return out
    track = tracks[date.today().toordinal() % len(tracks)]  # rotate background music
    subprocess.run([
        "ffmpeg", "-y", "-i", str(joined), "-stream_loop", "-1", "-i", str(track),
        "-filter_complex", "[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True)
    return out


def upload(video: Path, thumb: Path, meta: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload",
                                "https://www.googleapis.com/auth/youtube"])
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": meta["title"][:100], "description": meta["description"],
                    "tags": meta["tags"], "categoryId": "27"},  # Education
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": True,
                   "containsSyntheticMedia": True},  # disclose AI-generated content
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
    except Exception as e:  # needs a phone-verified channel
        print("thumbnail skipped:", e)
    return vid


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    history = load_history()
    meta = write_rhyme(history)
    print("Topic:", meta["topic"], "|", meta["title"])
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))

    scenes = []
    for i, v in enumerate(meta["verses"]):
        img, aud = OUT / f"s{i}.png", OUT / f"s{i}.mp3"
        make_image(f"{STYLE}. Main character: {meta['character']}. Scene: {v['scene']}", img)
        add_lyrics(img, v["lyrics"])
        scenes.append((img, aud))

    song = OUT / "song.mp3"
    sung = make_song(meta, song)  # after images so a failed image step doesn't waste credits
    if not sung:
        for v, (_, aud) in zip(meta["verses"], scenes):
            asyncio.run(make_audio(v["lyrics"], aud))

    if sung:
        video = build_sung_video([s[0] for s in scenes], song, OUT / "final.mp4")
    else:
        video = build_video(scenes, OUT / "final.mp4")
    if "--no-upload" in sys.argv:
        print("Built", video)
        return
    vid = upload(video, scenes[0][0], meta)
    print("Uploaded: https://youtu.be/" + vid)
    HISTORY.write_text(json.dumps(history + [meta["topic"]], indent=2))


if __name__ == "__main__":
    main()
