# CareerFit Studio - One-Click Dashboard

This build removes the multi-page left navigation workflow and turns CareerFit into a single command-center page.

## What changed

- All inputs, company sources, computation controls, results, and diagnostics are available on one page.
- Primary button: **Build profile + scan all jobs**.
- Sidebar now contains only compute controls, not page navigation.
- Added persisted UI company sources in `data/runtime_companies.json`.
- Results, source status, platform analytics, logs, and exports remain visible on the same screen.

## Recommended workflow

1. Paste portfolio / GitHub / public profile URLs.
2. Upload resume/CV PDFs or real DOCX files.
3. Add career URLs for target companies.
4. Click **Build profile + scan all jobs**.
5. Review high-fit cards and export CSV if needed.

## Run locally

```bash
bash scripts/setup.sh
source .venv/bin/activate
bash scripts/run.sh
```

