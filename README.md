# 🏆 Family World Cup 2026 Leaderboard

A self-updating GitHub Pages site that tracks your family's World Cup 2026 pool.
A GitHub Action checks live scores every 20 minutes via the
[football-data.org](https://www.football-data.org/) API, applies your family
scoring rules, and updates the site — no server, no cost, runs by itself for
the whole tournament.

## Scoring rules (current settings)

| Result | Points |
|---|---|
| Group-stage win | 1 |
| Group-stage draw | 0.5 |
| Round of 32 win | 2 |
| Round of 16 win | 3 |
| Quarter-final win | 4 |
| Semi-final win | 5 |
| Third-place match win | 3 |
| Final win | 6 |

Penalty-shootout wins count as full wins. Change any value in **`config.json`**
— for example, to score the bronze-medal match like a semi-final, set
`"THIRD_PLACE": 5`.

Team assignments live in **`assignments.json`**. Edit that file any time;
the next scheduled run picks it up automatically.

## One-time setup (about 10 minutes)

1. **Get a free API key.** Register at
   [football-data.org/client/register](https://www.football-data.org/client/register)
   — the free tier covers the World Cup. The key arrives by email.

2. **Create the repository.** On GitHub, create a new **public** repo (public
   is required for free GitHub Pages), e.g. `family-world-cup`. Upload all the
   files in this folder, keeping the folder structure (the
   `.github/workflows/update-scores.yml` path matters). Easiest ways: drag the
   files into the GitHub web uploader, or `git push` from a Codespace/your
   machine.

3. **Add the API key as a secret.** In the repo:
   **Settings → Secrets and variables → Actions → New repository secret**.
   Name it exactly `FOOTBALL_DATA_TOKEN` and paste your API key as the value.

4. **Allow the workflow to push.** **Settings → Actions → General →
   Workflow permissions** → select **Read and write permissions** → Save.

5. **Turn on GitHub Pages.** **Settings → Pages** → under *Build and
   deployment*, set Source to **Deploy from a branch**, branch **main**,
   folder **/ (root)** → Save. Your site URL appears at the top of that page
   (usually `https://<your-username>.github.io/<repo-name>/`).

6. **Run it once manually.** Go to the **Actions** tab → **Update scores** →
   **Run workflow**. When it finishes green, refresh your site — real fixture
   data should appear. From then on it runs itself every 20 minutes.

## How it works

```
.github/workflows/update-scores.yml   schedule: fetch + commit every 20 min
scripts/update_scores.py              calls the API, applies scoring, writes data.json
assignments.json                      who owns which teams
config.json                           points per round
data.json                             generated output (don't edit by hand)
index.html                            the website — reads data.json
```

The script only commits when something actually changed, so the repo history
stays clean between match days. During live matches, scores on the site move
as the Action runs (and the page also re-checks itself every 5 minutes while
open).

## Troubleshooting

- **Site shows "No score data yet"** — run the workflow once from the Actions
  tab (step 6), and check it succeeded.
- **Workflow fails with HTTP 403/401** — the `FOOTBALL_DATA_TOKEN` secret is
  missing or mistyped (step 3).
- **Workflow fails on the push step** — workflow permissions aren't set to
  read/write (step 4).
- **A team isn't getting points** — the API may spell it differently than
  `assignments.json`. Common spellings are already handled (USA, Türkiye,
  Korea Republic, Ivory Coast, Cabo Verde, DR Congo, Czechia, …). If one slips
  through, add a line to the `ALIASES` dict near the top of
  `scripts/update_scores.py`.
- **Scheduled runs stop after the tournament** — GitHub disables schedules
  after 60 days without repo activity, which is fine; you can also delete or
  disable the workflow once the final is played.
