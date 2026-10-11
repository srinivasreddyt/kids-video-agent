# Kids Video Agent

Daily pipeline: Claude writes an original nursery rhyme -> edge-tts voices it -> OpenAI generates
illustrations -> ffmpeg assembles a video with lyrics -> uploaded to YouTube (marked made-for-kids,
AI-disclosed). Runs on GitHub Actions cron.

## Setup
1. **Anthropic key**: console.anthropic.com -> API keys -> `ANTHROPIC_API_KEY`.
2. **Images**: free Cloudflare Workers AI (FLUX Schnell) via `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`; optional `OPENAI_API_KEY` as a paid fallback.
3. **YouTube**: create a Google Cloud project, enable *YouTube Data API v3*, configure the OAuth
   consent screen (add yourself as a test user), create an *OAuth client ID* of type **Desktop app**,
   download it as `client_secret.json`. Then run once on your PC:
   `pip install -r requirements.txt && python auth_setup.py client_secret.json`
   and copy the 3 printed values.
4. Push this folder to a new GitHub repo. Settings -> Secrets and variables -> Actions -> add
   `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.
5. Actions tab -> "Daily kids video" -> Run workflow to test. Videos upload as **private** first;
   review a few, then set repo variable `YT_PRIVACY` to `public`.
6. Optional: drop royalty-free `.mp3` files into `assets/music/` for background music.

## Notes
- Unverified Google API projects force uploads to private until you pass YouTube's API audit.
- Default quota allows ~6 uploads/day. Uploads cost 1600 units of the 10,000 daily quota.
- Voice is a rhythmic child-like TTS, not true singing. For real singing, swap `make_audio`
  for a music-generation service.
- Test locally without uploading: `python agent.py --no-upload` (needs ffmpeg + API keys).
