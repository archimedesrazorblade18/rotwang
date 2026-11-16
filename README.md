# Rotwang

Contract work for a statistician in two industries, life sciences and energy,
read from company career sites every Monday and shown in an app.

The app: https://archimedesrazorblade18.github.io/rotwang/

## What happens every Monday

GitHub runs `python radar.py --weekly` on its own computers (the "Weekly sweep"
under the Actions tab). That run:

1. takes in the company lists (`rotwang_targets.json`, plus public registries once a month),
2. finds each company's careers board (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Paylocity),
3. reads every open job on every board it found,
4. adds recruiter and job-index listings (Adzuna if a key is set; Remotive, Remote OK, Jobicy, Himalayas, The Muse),
5. keeps contract roles in statistics, data science, programming and AI, plus senior
   leadership roles worth pitching as contract work,
6. writes `docs/data.json`, which the app reads.

To run it any other day: Actions, Weekly sweep, Run workflow.

## Better results

These are optional. Each is added under Settings, Secrets and variables, Actions, New repository secret.

| Secret name | What it adds |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude reads each posting and decides whether it is your field, a real contract, and what it pays. Without it, keyword rules decide and more off-target roles get through. |
| `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` | Recruiter and staffing-firm listings, and contract searches by job title across the Adzuna index. Free at developer.adzuna.com. |
| `RADAR_SEC_EMAIL` | Lets the monthly company refresh include every listed US company in the energy and life-science industry codes. The SEC requires a contact email on each request. |

## Adding a company by hand

Edit `rotwang_targets.json` and add the name to the right group. If its careers
page is on a system the sweep can't guess (most Workday sites), run on any computer
with Python:

    python radar.py --add "Company name" https://company.wd1.myworkdayjobs.com/SiteName

## What is not in this repository

Anything private: API keys, email settings, CVs. Saved and applied marks live
only in the browser on the computer where you clicked them.
