# CareerFit Intelligence Studio - Cloud Optimized

CareerFit Intelligence Studio is a manual, profile-aware job discovery application. Users build a profile from public websites and uploaded PDF/DOCX resumes, add company career sources, run scans on demand, and review ranked job matches in a product-style Streamlit UI.

## Local quick start

```bash
cd careerfit-intelligence-cloud-optimized
bash scripts/setup.sh
source .venv/bin/activate
bash scripts/run.sh
```

## Performance defaults

This version is tuned for fast interactive use:

- Fast ATS board scans by default.
- Job-board response caching with a 6-hour TTL.
- Parallel company fetches.
- Capped resume extraction for very large documents.
- Optional Workday detail fetching only when you disable Fast Scan mode.

For free Streamlit hosting, keep `CAREERFIT_COMPANY_WORKERS` between 2 and 4 and keep Fast Scan mode enabled for broad scans.

## Deployment

See `docs/STREAMLIT_DEPLOYMENT.md`.

## Multi-user product wording update

This build uses professional multi-user product language across the single-page command workflow. Users can add profile inputs, career documents, and employer career sources, then run a complete match analysis from one workspace.
