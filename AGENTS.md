# Project Notes for Codex Agents

## Primary Workflow
- Use `run_course.py` as the default entrypoint.
- Input is a product id or full product URL.
- Outputs live under `outputs/<product_id>/`.

## Authentication
- Cookies are stored in `cookies.json` at repo root (gitignored).
- If cookies are missing or expired, the runner opens a browser for Google auth and refreshes cookies automatically.

## Resume Behavior
- The pipeline is resumable by default; re-running the same command continues from the last successful step.
- Failures are tracked in `outputs/<product_id>/transcripts/FAILED_ITEMS.txt`.

## Cleanup
- Legacy/previous artifacts live under `old/` and are not part of the active pipeline.
