# Public release verification

Verified on 2026-09-29 with Windows, Python 3.12.9 and FFmpeg 8.0.1.

- Fresh virtual environment installed successfully from requirements.txt.
- CPU-only synthetic two-clip render passed, including decoding, duration, colour metadata and audio QC.
- CLI entry points for studio, pause analysis and transcript mapping loaded successfully.
- Python source compilation passed.
- Tracked-file audit passed: UTF-8 source/text only, no media, runtime directories, detected credentials or personal filesystem paths.

The public repository starts with new history. It does not include the private workshop history, source footage, exports, transcripts, local agent configuration, credentials, models or vendor environments. Tests generate synthetic media in ignored runtime folders.

Not validated for this release: Linux/macOS runtime, fresh model download/transcription, every rendering combination, optional GPU filters, and creative quality on arbitrary footage. Review exported video and audio before delivery. The tested synthetic render establishes that the baseline local pipeline runs; it does not establish automatic editorial quality.
