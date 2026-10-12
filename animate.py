"""Animated scene builder (ffmpeg): camera moves, drifting sparkles, line-by-line lyrics, transitions."""
import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 25
FADE = 0.7  # transition length (s)
TRANSITIONS = ["fade", "slideleft", "circleopen", "wiperight", "slideup", "fadewhite"]


def font(size: int):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _run(cmd: list[str]):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-1500:])


def _star(path: Path, size: int):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c, pts = size / 2, []
    for i in range(10):
        r = c * 0.95 if i % 2 == 0 else c * 0.4
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((c + r * math.cos(a), c + r * math.sin(a)))
    ImageDraw.Draw(img).polygon(pts, fill=(255, 224, 90, 210))
    img.save(path)


def _bubble(path: Path, size: int):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), outline=(255, 255, 255, 200), width=3, fill=(180, 220, 255, 70))
    d.ellipse((size * 0.25, size * 0.2, size * 0.45, size * 0.35), fill=(255, 255, 255, 180))
    img.save(path)


def _lyric_layer(shown: list[str], total: int, path: Path):
    f = font(64)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    band_h = 90 * total + 40
    d.rectangle((0, H - band_h, W, H), fill=(0, 0, 0, 150))
    for i, line in enumerate(shown):
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, H - band_h + 25 + i * 90), line, font=f, fill="white",
               stroke_width=2, stroke_fill="black")
    layer.save(path)


def _moves(n: int):
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return [
        (f"1+0.15*on/{n}", cx, cy),                       # zoom in
        (f"1.15-0.15*on/{n}", cx, cy),                    # zoom out
        ("1.15", f"(iw-iw/zoom)*on/{n}", cy),             # pan right
        ("1.15", f"(iw-iw/zoom)*(1-on/{n})", cy),         # pan left
    ]


def _scene(i: int, img: Path, lines: list[str], dur: float, work: Path) -> Path:
    length = dur + FADE  # extra tail is consumed by the crossfade into the next scene
    frames = int(length * FPS)
    z, x, y = _moves(frames)[i % 4]
    inputs = ["-i", str(img)]
    graph = [f"[0:v]scale=3840:2160:flags=lanczos,zoompan=z='{z}':x='{x}':y='{y}'"
             f":d={frames}:s={W}x{H}:fps={FPS}[b]"]
    cur, idx, rng = "b", 1, random.Random(i)

    def add_input(p: Path):
        nonlocal idx
        inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.2f}", "-i", str(p)])
        idx += 1
        return idx - 1

    for j in range(8):  # drifting sparkles & bubbles (behind the lyrics)
        p = work / f"p{i}_{j}.png"
        (_star if j % 2 == 0 else _bubble)(p, rng.randint(36, 90))
        n = add_input(p)
        graph.append(
            f"[{cur}][{n}:v]overlay=x='{rng.randint(100, W - 100)}+{rng.randint(20, 60)}"
            f"*sin(t*{rng.uniform(1, 2.5):.2f}+{rng.uniform(0, 6.28):.2f})'"
            f":y='H-mod({rng.randint(0, H)}+{rng.randint(40, 90)}*t,H+h)'[p{j}]")
        cur = f"p{j}"

    total, start, span = len(lines), 0.6, max(dur - 1.5, 1.0)
    for k in range(1, total + 1):  # lyric lines appear one by one
        p = work / f"lyr{i}_{k}.png"
        _lyric_layer(lines[:k], total, p)
        n = add_input(p)
        a = start + (k - 1) * span / total
        b = start + k * span / total if k < total else length + 1
        graph.append(f"[{cur}][{n}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[l{k}]")
        cur = f"l{k}"

    graph.append(f"[{cur}]format=yuv420p[v]")
    out = work / f"clip{i}.mp4"
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]",
          "-t", f"{length:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)])
    return out


def build_animated_video(images: list[Path], lyrics: list[list[str]], audio: Path,
                         out: Path, work: Path, verse_s: float) -> Path:
    clips = [_scene(i, img, lines, verse_s, work) for i, (img, lines) in enumerate(zip(images, lyrics))]
    n = len(clips)
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    cmd += ["-i", str(audio)]
    if n == 1:
        graph = "[0:v]null[v]"
    else:
        parts, prev = [], "[0:v]"
        for k in range(1, n):
            lab = "[v]" if k == n - 1 else f"[x{k}]"
            parts.append(f"{prev}[{k}:v]xfade=transition={TRANSITIONS[(k - 1) % len(TRANSITIONS)]}"
                         f":duration={FADE}:offset={k * verse_s:.3f}{lab}")
            prev = lab
        graph = ";".join(parts)
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", f"{n}:a", "-t", f"{n * verse_s:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out)]
    _run(cmd)
    return out
