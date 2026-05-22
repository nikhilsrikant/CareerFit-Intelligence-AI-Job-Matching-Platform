# Deploy CareerFit Intelligence Studio on Streamlit Community Cloud

## 1. Prepare the repo

Push the project folder to GitHub. Keep these files out of Git:

```gitignore
.env
data/cache/
data/runtime_profiles/
outputs/
*.pdf
*.docx
```

## 2. Deploy

1. Go to Streamlit Community Cloud.
2. Create a new app.
3. Select your GitHub repository and branch.
4. Set the app entry point to `streamlit_app.py`.
5. In Advanced settings, use Python 3.12 if asked.
6. Paste secrets/environment values if needed.
7. Deploy.

## 3. Recommended free-hosting settings

```toml
CAREERFIT_DEFAULT_THRESHOLD = "0.85"
CAREERFIT_DEFAULT_SEARCH = "intern"
CAREERFIT_COMPANY_WORKERS = "3"
CAREERFIT_USE_FETCH_CACHE = "true"
CAREERFIT_FETCH_CACHE_SECONDS = "21600"
CAREERFIT_PROFILE_FETCH_TIMEOUT = "8"
CAREERFIT_HTTP_TIMEOUT = "16"
CAREERFIT_FETCH_WORKDAY_DETAILS = "false"
CAREERFIT_MAX_PDF_PAGES = "12"
CAREERFIT_MAX_PROFILE_CHARS = "90000"
```

## 4. Scaling path

Community Cloud is excellent for a portfolio demo. If usage grows, split the system into:

- Streamlit/Next.js frontend
- FastAPI backend
- Postgres storage
- Background worker queue for career-source fetching
- Object storage for temporary resume files
