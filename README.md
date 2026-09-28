# Agent Video Studio

A local editing toolkit for AI agents: inspect footage, plan cuts, transcribe speech, render a timeline and check the export. Built around Python, FFmpeg and reviewable JSON plans. Works with Codex, Claude Code or a terminal.

## What it does

- Ingest originals with SHA-256 verification and keep versioned exports.
- Inspect metadata, decode footage, make contact sheets and detect scene boundaries.
- Transcribe locally with faster-whisper and create SRT/ASS subtitles.
- Render cuts, crop/fit, transitions, grading, stabilization and subtitle overlays.
- Process dialogue, mix music with ducking and normalize loudness.
- Check export duration, frame timing, audio, colour metadata and decoding.
- Review cut boundaries, transcript coverage, voice and music levels with auxiliary scripts.

This is an agent-operated command-line studio, not a graphical video editor. The agent plans and reviews the edit; the scripts execute explicit operations. Technical QC does not replace watching and listening to the finished film.

## Install

Use Python 3.12 and an FFmpeg/ffprobe build on PATH with libx264, libass and zscale. Stabilization additionally needs vidstab. NVIDIA encoding is optional; the default example uses CPU encoding.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/studio.py doctor
```

On Linux/macOS use `.venv/bin/python` instead. Tested release commands and limitations are recorded in [docs/RELEASE.md](docs/RELEASE.md).

## First edit

Place your own footage in `00_INBOX`. Open the folder with an agent and ask:

> Make a vertical edit from the footage in 00_INBOX. Inspect and transcribe it, propose an edit, preserve the originals, and review the picture, captions and sound before delivery.

Or use the CLI:

```text
python scripts/studio.py init demo 00_INBOX/take.mp4
python scripts/studio.py inspect 00_INBOX/take.mp4
python scripts/studio.py download-model
python scripts/studio.py transcribe 00_INBOX/take.mp4
python scripts/studio.py render templates/edit.example.json
```

Edit the example's file path and in/out times first. `download-model` downloads Whisper weights; transcription runs locally afterwards. Agent providers may receive context according to your own agent settings. No API credentials are required by this release.

## Project layout

| Path | Purpose |
| --- | --- |
| `scripts/` | Renderer, analysis and review tools |
| `templates/` | Edit plan, brief and review checklist |
| `00_INBOX/` | Your source media (ignored by Git) |
| `01_PROJECTS/` | Ingest copies, analyses and transcripts (ignored) |
| `02_ASSETS/` | Your licensed fonts, music and LUTs (ignored) |
| `03_EXPORTS/` | Rendered outputs (ignored) |
| `models/` | Downloaded models (ignored) |

Create media folders as needed. Never commit client footage or credentials. This repository ships only source and text templates, with no footage, audio, model weights, third-party fonts or client projects.

## Verification

```text
python tests/smoke.py
python scripts/check_release.py
```

The smoke test creates synthetic media locally, renders a two-clip timeline and checks the output. Generated files stay ignored. It needs FFmpeg and the Python dependencies; no GPU or model download is needed.

## Scope and customization

The public release contains the standalone local pipeline. Machine-specific MCP bridges, cloud stock integrations and experimental GPU tools from the private workshop are not bundled. Bring your own licensed media and fonts. Samsung Log requires an appropriate LUT; a visual review is essential for HDR conversion and stabilization.

For configuration, team workflows and tailored deployment: [Mikołaj Mikołajczak](https://mikolajmikolajczak.pl/kontakt).

## License

MIT for this repository's source. FFmpeg, Python libraries and downloaded models retain their respective licenses. This license grants no rights to footage or other assets supplied by users.
