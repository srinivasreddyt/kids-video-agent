"""Daily kids nursery-rhyme video agent.

Claude writes + reviews a rhyme -> images (vision-checked) -> sung song (ElevenLabs, TTS fallback)
-> animated ffmpeg video with intro/outro -> thumbnail -> YouTube upload + playlist.
Flags: --dry (placeholder images, free TTS voice, no paid APIs except Claude), --no-upload.
"""
import asyncio
import base64
import io
import json
import os
import random
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFilter

from animate import H, W, build_animated_video, font, lyric_layer

ROOT = Path(__file__).parent
OUT = ROOT / "out" / date.today().isoformat()
HISTORY = ROOT / "history.json"
CHARACTERS = json.loads((ROOT / "characters.json").read_text(encoding="utf-8"))
DRY = "--dry" in sys.argv

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
VOICE = os.getenv("TTS_VOICE", "en-US-AnaNeural")  # child-like voice (TTS fallback)
PRIVACY = os.getenv("YT_PRIVACY", "private")  # private | unlisted | public
CHANNEL = os.getenv("CHANNEL_NAME", "Kids Rhyme Time")
INTRO_S, OUTRO_S = float(os.getenv("INTRO_S", "5")), float(os.getenv("OUTRO_S", "6"))
VERSE_S, CHORUS_S = float(os.getenv("VERSE_S", "16")), float(os.getenv("CHORUS_S", "14"))
THEMES = ["colors", "counting", "animals", "kindness", "bedtime", "feelings", "shapes",
          "healthy habits", "nature", "alphabet"]

STYLE = ("wide landscape composition, main subject centered, "
         "bright cheerful children's picture-book illustration, flat colorful vector storybook style, "
         "soft rounded shapes, thick soft outlines, vivid pastel colors, no text, no letters, "
         "no scary elements")

_client = None


def claude(content, max_tokens: int = 8000) -> dict:
    """One Claude call that must answer with a single JSON object."""
    global _client
    _client = _client or anthropic.Anthropic()
    msg = _client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                  messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def load_history() -> list[str]:
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else []


# ---------- 1. writing + review ----------

def write_rhyme(past: list[str], ch: dict, feedback: str) -> dict:
    fb = f"\nA reviewer REJECTED your previous attempt because: {feedback}\nFix every one of those problems.\n" if feedback else ""
    return claude(f"""Write an ORIGINAL nursery rhyme song for toddlers (ages 2-5) starring {ch['name']}
({ch['look']}). Do not copy or closely imitate any existing song or rhyme. Simple words, strong bouncy
rhythm, clean rhymes, a gentle positive message. Pick ONE theme from: {THEMES}.
Already made (choose a different topic): {past[-60:]}
{fb}
Structure: exactly 4 verses, each 3-4 short lines, plus a CHORUS of 2-3 very catchy short lines that
repeats after every verse (the chorus must stay the same each time).
Every "scene" is a concrete picture description: what {ch['name']} is doing, where, and which objects are
visible. No text or letters in pictures. Keep the character's look exactly as described.

Reply with ONLY JSON:
{{"topic": "short topic", "theme": "one of the themes",
 "title": "YouTube title under 70 chars: {ch['name']} + topic, ending ' | Nursery Rhyme for Kids'",
 "thumb_text": "2-4 words, ALL CAPS, catchy",
 "description": "2-3 sentence parent-friendly description",
 "tags": ["12 relevant tags incl. kids songs, nursery rhymes, toddler songs"],
 "chorus": {{"lyrics": "line1\\nline2", "scene": "..."}},
 "verses": [{{"lyrics": "line1\\nline2\\nline3", "scene": "..."}}]}}""")


def get_rhyme(history: list[str], ch: dict) -> dict:
    feedback = ""
    for attempt in range(3):
        meta = write_rhyme(history, ch, feedback)
        if len(meta.get("verses", [])) != 4 or "chorus" not in meta:
            feedback = "it must have exactly 4 verses and a chorus"
            continue
        review = claude(f"""You are a strict children's-content reviewer. Review this nursery rhyme for
toddlers (ages 2-5). Reject ONLY for real problems: anything unsafe, scary, violent, or inappropriate;
lines that clearly copy a well-known song or rhyme; broken rhyme/rhythm; vocabulary too hard for toddlers;
factual errors (e.g. wrong counting or colors); scenes that need text or letters to make sense; or scene
descriptions that contradict the character ({ch['look']}).
Reply with ONLY JSON: {{"approved": true|false, "problems": ["..."]}}

{json.dumps(meta, indent=1)}""", 3000)
        if review.get("approved"):
            return meta
        feedback = "; ".join(review.get("problems", [])) or "unspecified quality problems"
        print(f"rhyme rejected (attempt {attempt + 1}): {feedback}")
    raise RuntimeError("no rhyme passed content review; nothing uploaded")


# ---------- 2. images ----------

def _post_with_retry(url: str, **kw) -> requests.Response:
    for attempt in range(1, 6):
        r = requests.post(url, timeout=300, **kw)
        if r.status_code not in (429, 500, 502, 503) or "insufficient_quota" in r.text or attempt == 5:
            break
        wait = 15 * attempt
        print(f"image API {r.status_code}, retrying in {wait}s: {r.text[:200]}")
        time.sleep(wait)
    if not r.ok:
        print("image API error body:", r.text[:500])
    r.raise_for_status()
    return r


def _cloudflare(model: str, payload: dict) -> bytes:
    acct, token = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
    r = _post_with_retry(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}",
        headers={"Authorization": f"Bearer {token}"}, json=payload)
    if "image" in r.headers.get("content-type", ""):  # some models return raw image bytes
        return r.content
    body = r.json()
    return base64.b64decode((body.get("result") or body)["image"])


def image_lucid(prompt: str) -> bytes:
    """True 16:9 at 1344x768 (no crop, no stretch). Billed per 512px tile, so mostly beyond the free tier."""
    return _cloudflare("@cf/leonardo/lucid-origin",
                       {"prompt": prompt[:2000], "width": 1344, "height": 768, "num_steps": 20,
                        "seed": random.randint(0, 2**31 - 1)})


def image_schnell(prompt: str) -> bytes:
    """Free but square 1024x1024, so it gets cropped and stretched (softer)."""
    return _cloudflare("@cf/black-forest-labs/flux-1-schnell", {"prompt": prompt[:2000], "steps": 6})


def image_openai(prompt: str) -> bytes:
    r = _post_with_retry(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": os.getenv("IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
              "size": "1536x1024", "n": 1, "quality": os.getenv("IMAGE_QUALITY", "low")})
    return base64.b64decode(r.json()["data"][0]["b64_json"])


def make_image(prompt: str, path: Path):
    providers = []
    if os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ACCOUNT_ID"):
        if os.getenv("CF_IMAGE_MODEL", "lucid") == "lucid":
            providers.append(image_lucid)
        providers.append(image_schnell)
    if os.getenv("OPENAI_API_KEY"):
        providers.append(image_openai)  # fallback (or sole provider)
    raw = None
    for fn in providers:
        try:
            raw = fn(prompt)
            break
        except Exception as e:
            print(f"{fn.__name__} failed: {e}")
    if raw is None:
        raise RuntimeError("no image provider succeeded")
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    scale = max(W / img.width, H / img.height)  # cover-crop to 16:9
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((left, top, left + W, top + H))
    img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3)).save(path)


def placeholder_image(label: str, path: Path):
    """--dry mode: a colorful stand-in so the whole pipeline can be tested for free."""
    rng = random.Random(label)
    img = Image.new("RGB", (W, H), tuple(rng.randint(120, 230) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(9):
        x, y, r = rng.randint(0, W), rng.randint(0, H), rng.randint(60, 220)
        d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randint(80, 255) for _ in range(3)))
    d.text((60, 40), label[:60], font=font(54), fill="white", stroke_width=3, stroke_fill="black")
    img.save(path)


def check_image(path: Path, scene: str, ch: dict) -> tuple[bool, str, str]:
    small = Image.open(path).convert("RGB")
    small.thumbnail((960, 540))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    r = claude([
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": f"""This is an illustration for a toddler nursery-rhyme video.
It should show: {scene}. The character should be {ch['name']}: {ch['look']}.
Flag ONLY clear, obvious problems: (a) badly distorted anatomy (extra/missing limbs or heads, melted or
merged faces), (b) scary, violent or inappropriate content, (c) visible text, letters or watermarks,
(d) the main character or scene is clearly wrong. Minor style variation is fine.
Reply with ONLY JSON: {{"ok": true|false, "severity": "none"|"minor"|"unsafe", "reason": "short"}}"""}],
        3000)
    return bool(r.get("ok")), r.get("severity", "none"), r.get("reason", "")


def make_checked_image(prompt: str, path: Path, scene: str, ch: dict, tries: int = 3):
    if DRY:
        placeholder_image(scene, path)
        return
    severity = "none"
    for attempt in range(1, tries + 1):
        make_image(prompt, path)
        try:
            ok, severity, why = check_image(path, scene, ch)
        except Exception as e:  # never block the pipeline on a failed check
            print("image check unavailable:", e)
            return
        if ok:
            return
        print(f"image check failed (attempt {attempt}, {severity}): {why}")
    if severity == "unsafe":
        raise RuntimeError(f"image repeatedly flagged unsafe: {scene}")
    print("accepting image after retries (minor issue only)")


# ---------- 3. audio ----------

def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def make_song(plan: list[dict], path: Path) -> bool:
    """Generate the sung song with ElevenLabs Music. Returns False if unavailable/failed."""
    key = os.getenv("ELEVENLABS_API_KEY")
    if DRY or not key:
        return False
    styles = ["children's nursery rhyme", "cheerful sung female vocal", "simple catchy melody",
              "gentle ukulele, glockenspiel and soft percussion", "about 100 bpm"]
    chunks = [{"text": s["song"], "duration_ms": int(s["dur"] * 1000), "positive_styles": styles,
               "negative_styles": ["dark", "aggressive", "spoken word"]} for s in plan]
    try:
        r = requests.post("https://api.elevenlabs.io/v1/music",
                          params={"output_format": "mp3_44100_128"},
                          headers={"xi-api-key": key},
                          json={"composition_plan": {"chunks": chunks},
                                "model_id": os.getenv("ELEVENLABS_MODEL", "music_v2_5")},
                          timeout=900)
        r.raise_for_status()
        path.write_bytes(r.content)
        return True
    except Exception as e:
        resp = getattr(e, "response", None)
        body = resp.text[:500] if resp is not None else ""
        print("ElevenLabs song failed, falling back to TTS:", e, body)
        return False


def run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("command failed: " + " ".join(cmd[:3]) + "\n" + r.stderr[-1200:])


def tts_track(plan: list[dict], path: Path):
    """Free fallback: voice each sung scene, silence for intro/outro, each fitted to its slot."""
    segs = []
    for i, s in enumerate(plan):
        seg = OUT / f"seg{i}.wav"
        if s["kind"] in ("verse", "chorus"):
            mp3 = OUT / f"seg{i}.mp3"
            asyncio.run(edge_tts.Communicate("\n".join(s["lines"]).replace("\n", " ... "), VOICE,
                                             rate="-15%", pitch="+8Hz").save(str(mp3)))
            tempo = max(1.0, duration(mp3) / (s["dur"] - 0.5))  # speed up if it would overrun the slot
            src, af = ["-i", str(mp3)], f"atempo={min(tempo, 2.0):.3f},apad"
        else:
            src, af = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"], "apad"
        run(["ffmpeg", "-y", *src, "-af", af, "-t", f"{s['dur']:.2f}", "-ar", "44100", "-ac", "2", str(seg)])
        segs.append(seg)
    listing = OUT / "segs.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame", str(path)])


# ---------- 4. video fallback, thumbnail, upload ----------

def build_static_video(plan: list[dict], audio: Path, out: Path) -> Path:
    """Last-resort fallback if the animated build fails: still images with burned-in lyrics."""
    entries = []
    for i, s in enumerate(plan):
        frame = OUT / f"static{i}.png"
        layer = OUT / f"static_layer{i}.png"
        lyric_layer(s["lines"], s["lines"], layer)
        Image.alpha_composite(Image.open(s["image"]).convert("RGBA"),
                              Image.open(layer)).convert("RGB").save(frame)
        entries.append((frame, s["dur"]))
    listing = OUT / "static.txt"
    listing.write_text("".join(f"file '{f.as_posix()}'\nduration {d}\n" for f, d in entries)
                       + f"file '{entries[-1][0].as_posix()}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(audio),
         "-vf", "format=yuv420p", "-r", "25", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out)])
    return out


def make_thumbnail(img_path: Path, text: str, path: Path):
    img = Image.open(img_path).convert("RGB").resize((1280, 720), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    words = text.upper().split()
    mid = (len(words) + 1) // 2
    lines = [" ".join(words[:mid]), " ".join(words[mid:])] if len(words) > 2 else [" ".join(words)]
    lines = [l for l in lines if l]
    size = 150
    while size > 40 and max(d.textlength(l, font=font(size)) for l in lines) > 1160:
        size -= 6
    f, y = font(size), 720 - 40 - int(size * 1.2) * len(lines)
    for l in lines:
        d.text(((1280 - d.textlength(l, font=f)) / 2, y), l, font=f, fill=(255, 235, 60),
               stroke_width=10, stroke_fill=(30, 30, 30))
        y += int(size * 1.2)
    img.save(path, "JPEG", quality=90)


def build_description(meta: dict) -> str:
    lyrics = []
    for i, v in enumerate(meta["verses"]):
        lyrics.append(f"[Verse {i + 1}]\n{v['lyrics']}")
        if i == 0:
            lyrics.append(f"[Chorus]\n{meta['chorus']['lyrics']}")
    return (f"{meta['description']}\n\nLyrics\n" + "\n\n".join(lyrics) +
            f"\n\nSubscribe to {CHANNEL} for a new kids song every day!\n"
            "The art, music and voice in this video are AI-generated.\n\n"
            "#nurseryrhymes #kidssongs #toddlersongs")[:4900]


def add_to_playlist(yt, vid: str, theme: str):
    title = f"{theme.title()} Songs for Kids"
    items = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute().get("items", [])
    pid = next((p["id"] for p in items if p["snippet"]["title"] == title), None)
    if not pid:
        pid = yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title, "description": f"{title} from {CHANNEL}"},
            "status": {"privacyStatus": PRIVACY}}).execute()["id"]
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()


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
        "snippet": {"title": meta["title"][:100], "description": build_description(meta),
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
    try:
        add_to_playlist(yt, vid, meta.get("theme", "nursery rhymes"))
    except Exception as e:
        print("playlist skipped:", e)
    return vid


# ---------- main ----------

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    history = load_history()
    ch = CHARACTERS[len(history) % len(CHARACTERS)]  # rotates with each successful upload
    meta = get_rhyme(history, ch)
    print(f"Topic: {meta['topic']} | {meta['title']} | Character: {ch['name']}")
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))

    def picture(name: str, scene: str) -> Path:
        path = OUT / f"{name}.png"
        make_checked_image(f"{STYLE}. Main character: {ch['name']}, {ch['look']}. Scene: {scene}",
                           path, scene, ch)
        return path

    hello = picture("intro", f"{ch['name']} waving hello at the viewer with a big happy smile, "
                             "standing in a sunny colorful meadow")
    chorus_img = picture("chorus", meta["chorus"]["scene"])
    verse_imgs = [picture(f"verse{i}", v["scene"]) for i, v in enumerate(meta["verses"])]

    short_title = meta["title"].split("|")[0].strip()
    chorus_lines = meta["chorus"]["lyrics"].split("\n")
    plan = [{"kind": "intro", "image": hello, "lines": [CHANNEL, short_title], "dur": INTRO_S,
             "song": "[Intro]\n{gentle instrumental intro, no vocals}"}]
    for i, v in enumerate(meta["verses"]):
        plan.append({"kind": "verse", "image": verse_imgs[i], "lines": v["lyrics"].split("\n"),
                     "dur": VERSE_S, "song": f"[Verse {i + 1}]\n{v['lyrics']}"})
        plan.append({"kind": "chorus", "image": chorus_img, "lines": chorus_lines, "dur": CHORUS_S,
                     "song": f"[Chorus]\n{meta['chorus']['lyrics']}"})
    plan.append({"kind": "outro", "image": hello, "lines": ["Thanks for watching!", f"Subscribe to {CHANNEL}"],
                 "dur": OUTRO_S, "song": "[Outro]\n{gentle instrumental ending, no vocals}"})

    song = OUT / "song.mp3"
    if not make_song(plan, song):  # after images so a failed image step doesn't waste credits
        tts_track(plan, song)

    final = OUT / "final.mp4"
    try:
        video = build_animated_video([s["image"] for s in plan], [s["lines"] for s in plan], song,
                                     final, OUT, [s["dur"] for s in plan])
    except Exception as e:
        print("animation failed, using static video:", e)
        video = build_static_video(plan, song, final)

    thumb = OUT / "thumbnail.jpg"
    make_thumbnail(hello, meta.get("thumb_text") or short_title, thumb)
    if "--no-upload" in sys.argv:
        print("Built", video)
        return
    vid = upload(video, thumb, meta)
    print("Uploaded: https://youtu.be/" + vid)
    HISTORY.write_text(json.dumps(history + [meta["topic"]], indent=2))


if __name__ == "__main__":
    main()
