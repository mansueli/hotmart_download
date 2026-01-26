# Hotmart Course Downloader + Transcriber

End-to-end pipeline to download Hotmart course videos and materials, then build a full transcript with module + lesson titles. Input is just the product id (e.g. `4459938`) and the tool resumes safely on re-runs.

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium
python run_course.py 4459938
```

On first run (or if cookies are expired), a browser window opens so you can complete Google authentication. Once logged in, cookies are saved to `cookies.json` automatically and used for future runs.
The repository `.gitignore` keeps cookies, outputs, and large media out of version control.

## What It Produces

Output goes to `outputs/<product_id>/`:

- `videos/` downloaded mp4s
- `materials/` attachments (pdfs, etc.)
- `transcripts/` per-item text outputs
- `course_manifest.json` source-of-truth manifest (modules, lessons, attachments)
- `state.json` resumable status snapshot
- `COURSE_TRANSCRIPT.md` combined transcript

## Resuming Behavior

The pipeline is idempotent:

- existing videos/attachments are skipped
- existing transcripts are skipped
- failures are logged to `transcripts/FAILED_ITEMS.txt`

Re-run `python run_course.py <product_id>` to resume from the last successful item.

## Flags

```bash
python run_course.py 4459938 \
  --output-dir outputs \
  --cookies cookies.json \
  --refresh-manifest \
  --retry-failed
```

If Google blocks automated login, use your local Chrome for authentication.
This reads cookies from your Chrome profile, so make sure you're already logged in to Hotmart.

```bash
python run_course.py "https://hotmart.com/pt-br/club/jannuzzi/products/4459938" \
  --auth-browser system \
  --chrome-bin /usr/bin/google-chrome
```

## System Dependencies

- `ffmpeg` for video downloads/transcription
- `pdftotext` for PDF attachments

Ubuntu/Debian:

```bash
sudo apt install ffmpeg poppler-utils
```

## Tests

```bash
python -m unittest discover -s tests
```

## Notes

- Output transcripts include module + lesson titles to disambiguate repeated lesson names.
- Attachments are included in the combined transcript (PDFs via `pdftotext`).

## Legacy Scripts

The repo still includes standalone scripts (`download_videos.py`, `transcribe_videos.py`) for advanced use, but `run_course.py` is the recommended entrypoint.
