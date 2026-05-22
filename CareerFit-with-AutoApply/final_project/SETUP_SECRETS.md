# Streamlit Cloud Setup — Paste this into App Settings → Secrets

Go to: your app on share.streamlit.io → ⋮ → Settings → Secrets

Paste the block below and fill in YOUR real info:

```toml
first_name = "Jane"
last_name  = "Doe"
email      = "jane@email.com"
phone      = "+1 555-123-4567"
city       = "San Francisco"
state      = "CA"
country    = "United States"

linkedin_url  = "https://linkedin.com/in/janedoe"
github_url    = "https://github.com/janedoe"
portfolio_url = "https://janedoe.dev"

current_company  = "Acme Corp"
current_title    = "Software Engineer"
years_experience = 3

university      = "UC Berkeley"
degree          = "B.S. Computer Science"
graduation_year = 2022

available_start_date = "Immediately"
salary_expectation   = "130000"
work_authorization   = "Yes"
requires_sponsorship = "No"

gender           = "Prefer not to say"
ethnicity        = "Prefer not to say"
veteran_status   = "I am not a protected veteran"
disability_status = "No, I don't have a disability"
referral_source  = "LinkedIn"

dry_run = true

skills = ["Python", "Machine Learning", "SQL", "React", "AWS"]

cover_letter_template = """Dear Hiring Team at {company},

I am excited to apply for the {job_title} role. My expertise in {skills} aligns closely with what your team is building. I am eager to contribute to {company}'s mission.

Thank you for considering my application.

Best regards,
{first_name}"""
```

⚠️  Keep dry_run = true until you've tested the agent fills correctly.
    When ready to actually submit, change it to dry_run = false.
