"""Compare Cloudflare text-to-image models/sizes on one prompt. Saves samples + prints size/time/status."""
import base64
import io
import os
import time
from pathlib import Path

import requests
from PIL import Image

PROMPT = ("wide landscape composition, main subject centered, bright cheerful children's picture-book "
          "illustration, soft rounded shapes, vivid pastel colors, friendly cartoon characters, no text. "
          "Main character: a small yellow duckling with an orange bill and tiny blue rain boots. "
          "Scene: the duckling splashing in a rainbow-colored puddle on a sunny meadow with flowers")
ACCT, TOKEN = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
OUT = Path("out/imgtest")
OUT.mkdir(parents=True, exist_ok=True)

TESTS = {
    "schnell_default": ("@cf/black-forest-labs/flux-1-schnell", {"steps": 6}),
    "schnell_1344x768": ("@cf/black-forest-labs/flux-1-schnell", {"steps": 6, "width": 1344, "height": 768}),
    "sdxl_lightning_1344x768": ("@cf/bytedance/stable-diffusion-xl-lightning",
                                {"width": 1344, "height": 768, "num_steps": 8}),
    "lucid_1344x768": ("@cf/leonardo/lucid-origin", {"width": 1344, "height": 768, "num_steps": 20}),
    "flux2_klein4b_1344x768": ("@cf/black-forest-labs/flux-2-klein-4b", {"width": 1344, "height": 768}),
}

for name, (model, extra) in TESTS.items():
    t0 = time.time()
    try:
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/ai/run/{model}",
                          headers={"Authorization": f"Bearer {TOKEN}"},
                          json={"prompt": PROMPT, **extra}, timeout=300)
        if "image" in r.headers.get("content-type", ""):
            raw = r.content
        else:
            body = r.json()
            if not r.ok or body.get("success") is False:
                print(f"[{name}] HTTP {r.status_code}: {str(body)[:300]}")
                continue
            raw = base64.b64decode((body.get("result") or body)["image"])
        img = Image.open(io.BytesIO(raw))
        (OUT / f"{name}.{img.format.lower()}").write_bytes(raw)
        print(f"[{name}] OK {img.size} {img.format} {len(raw)//1024}KB {time.time() - t0:.1f}s")
    except Exception as e:
        print(f"[{name}] FAILED: {e}")
