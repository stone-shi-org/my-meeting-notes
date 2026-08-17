# AGENTS.md

See [CLAUDE.md](CLAUDE.md) for architecture, conventions, and the non-obvious failure modes worth
knowing before changing anything.

Quick reference:

- Run: `docker compose up -d --build` → <http://localhost:4020> (admin / password on first boot)
- Test: `./test.sh` → JUnit XML in `test-reports/`
- Build: `./build.sh [-p]`
- Audio Simulator: `python3 audio_server.py` → <http://localhost:8000> (multi-channel meeting simulator & dataset audio playback to BlackHole/Speakers)
