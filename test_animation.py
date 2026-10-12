"""Zero-cost animation test: placeholder images + a tone, no API calls. Used by test-animation.yml."""
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from animate import H, W, build_animated_video, font

work = Path("out/test")
work.mkdir(parents=True, exist_ok=True)
VERSE = 6.0
COLORS = [(120, 200, 255), (255, 200, 140), (170, 230, 160)]

images, lyrics = [], []
for i, c in enumerate(COLORS):
    img = Image.new("RGB", (W, H), c)
    d = ImageDraw.Draw(img)
    for k in range(6):  # something visible to show camera movement
        d.ellipse((200 + k * 250, 150 + (k % 3) * 200, 400 + k * 250, 350 + (k % 3) * 200),
                  fill=(255 - k * 30, 120 + k * 20, 90 + k * 25))
    d.text((60, 40), f"Scene {i + 1}", font=font(90), fill="white")
    p = work / f"s{i}.png"
    img.save(p)
    images.append(p)
    lyrics.append([f"Scene {i + 1} line one", "line two goes here", "and line three"])

audio = work / "tone.mp3"
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={VERSE * 3}",
                str(audio)], check=True, capture_output=True)
video = build_animated_video(images, lyrics, audio, work / "final.mp4", work, VERSE)
for t in (1.5, 3.5, 6.2, 9.0, 14.0):  # preview frames
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                    str(work / f"frame_{t}.png")], check=True, capture_output=True)
print("OK", video)
