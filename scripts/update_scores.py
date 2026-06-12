#!/usr/bin/env python3
"""
World Cup 2026 family leaderboard updater.

Fetches all World Cup matches from football-data.org, applies the family
scoring rules from config.json to the team assignments in assignments.json,
and writes data.json for the website to display.

Run by GitHub Actions on a schedule. Requires the environment variable
FOOTBALL_DATA_TOKEN (a free football-data.org API key).

No third-party dependencies — standard library only.
"""

import json
import os
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://api.football-data.org/v4/competitions/WC/matches"

# Known finished-match statuses in the football-data.org v4 API.
FINISHED = {"FINISHED", "AWARDED"}
LIVE = {"IN_PLAY", "PAUSED"}
UPCOMING = {"SCHEDULED", "TIMED"}

# Map API stage names -> human labels shown on the site.
STAGE_LABELS = {
    "GROUP_STAGE": "Group stage",
    "LAST_32": "Round of 32",
    "LAST_16": "Round of 16",
    "QUARTER_FINALS": "Quarter-final",
    "SEMI_FINALS": "Semi-final",
    "THIRD_PLACE": "Third-place match",
    "FINAL": "Final",
}

# Alternate spellings: normalized alias -> normalized canonical assignment name.
# Both sides are passed through normalize() before lookup, so accents,
# case, and punctuation never matter.
ALIASES = {
    "usa": "united states",
    "united states of america": "united states",
    "korea republic": "south korea",
    "republic of korea": "south korea",
    "turkey": "turkiye",
    "czech republic": "czechia",
    "congo dr": "dr congo",
    "democratic republic of the congo": "dr congo",
    "dr of congo": "dr congo",
    "cape verde": "cabo verde",
    "cape verde islands": "cabo verde",
    "ivory coast": "cote divoire",
    "ir iran": "iran",
    "iran ir": "iran",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "holland": "netherlands",
}


def normalize(name: str) -> str:
    """Lowercase, strip accents, drop punctuation, collapse spaces."""
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().replace("'", "").replace("’", "")
    name = "".join(c if c.isalnum() or c.isspace() else " " for c in name)
    name = " ".join(name.split())
    return ALIASES.get(name, name)


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_matches(token: str) -> list:
    req = urllib.request.Request(API_URL, headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        sys.exit(f"API request failed: HTTP {e.code}. Response: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"API request failed: {e.reason}")
    return payload.get("matches", [])


def team_owner_lookup(assignments: dict) -> dict:
    """normalized team name -> (owner, canonical team name as the family wrote it)"""
    lookup = {}
    for owner, teams in assignments["participants"].items():
        for team in teams:
            lookup[normalize(team)] = (owner, team)
    return lookup


def match_team(api_team: dict, lookup: dict):
    """Try the API team's name, shortName, and TLA against the assignments."""
    for key in ("name", "shortName", "tla"):
        value = api_team.get(key)
        if value:
            hit = lookup.get(normalize(value))
            if hit:
                return hit
    return None


def stage_points(stage: str, scoring: dict, is_draw: bool) -> float:
    if stage == "GROUP_STAGE":
        return scoring["GROUP_STAGE_DRAW"] if is_draw else scoring["GROUP_STAGE_WIN"]
    return scoring.get(stage, 0)


def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        sys.exit("Missing FOOTBALL_DATA_TOKEN environment variable.")

    config = load_json(ROOT / "config.json")
    assignments = load_json(ROOT / "assignments.json")
    scoring = config["scoring"]
    lookup = team_owner_lookup(assignments)

    matches = fetch_matches(token)
    matches.sort(key=lambda m: m.get("utcDate") or "")

    # Running state
    points = {owner: 0.0 for owner in assignments["participants"]}
    team_points = {}   # canonical team name -> points earned
    eliminated = set() # canonical team names knocked out
    events = []        # scoring events, newest last
    knockout_teams = set()  # normalized names seen in any knockout fixture

    display_matches = []

    for m in matches:
        stage = m.get("stage", "")
        status = m.get("status", "")
        score = m.get("score", {}) or {}
        full_time = score.get("fullTime", {}) or {}
        home, away = m.get("homeTeam", {}) or {}, m.get("awayTeam", {}) or {}
        home_hit, away_hit = match_team(home, lookup), match_team(away, lookup)

        if stage != "GROUP_STAGE" and home.get("name") and away.get("name"):
            knockout_teams.add(normalize(home["name"]))
            knockout_teams.add(normalize(away["name"]))

        entry = {
            "stage": STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
            "stageCode": stage,
            "status": status,
            "utcDate": m.get("utcDate"),
            "home": {
                "name": home.get("name") or "TBD",
                "owner": home_hit[0] if home_hit else None,
                "goals": full_time.get("home"),
            },
            "away": {
                "name": away.get("name") or "TBD",
                "owner": away_hit[0] if away_hit else None,
                "goals": full_time.get("away"),
            },
            "penalties": (score.get("duration") == "PENALTY_SHOOTOUT"),
        }
        display_matches.append(entry)

        if status not in FINISHED:
            continue

        winner = score.get("winner")  # HOME_TEAM | AWAY_TEAM | DRAW (accounts for penalties)
        if winner is None:
            continue

        if winner == "DRAW":
            if stage == "GROUP_STAGE":
                pts = stage_points(stage, scoring, is_draw=True)
                for hit in (home_hit, away_hit):
                    if hit and pts:
                        owner, team = hit
                        points[owner] += pts
                        team_points[team] = team_points.get(team, 0) + pts
                        events.append({
                            "owner": owner, "team": team, "points": pts,
                            "label": f"{team} drew ({entry['stage']})",
                            "utcDate": m.get("utcDate"),
                        })
            continue

        win_hit = home_hit if winner == "HOME_TEAM" else away_hit
        lose_hit = away_hit if winner == "HOME_TEAM" else home_hit
        lose_name = (away if winner == "HOME_TEAM" else home).get("name")

        # Knockout losers are out of the tournament.
        if stage != "GROUP_STAGE" and stage != "THIRD_PLACE" and lose_name:
            if lose_hit:
                eliminated.add(lose_hit[1])

        if win_hit:
            pts = stage_points(stage, scoring, is_draw=False)
            if pts:
                owner, team = win_hit
                points[owner] += pts
                team_points[team] = team_points.get(team, 0) + pts
                suffix = " on penalties" if entry["penalties"] else ""
                events.append({
                    "owner": owner, "team": team, "points": pts,
                    "label": f"{team} won{suffix} ({entry['stage']})",
                    "utcDate": m.get("utcDate"),
                })

    # Once every group game has finished AND the knockout bracket has real
    # teams in it, anyone assigned a team that didn't make the bracket is
    # eliminated too. (Checking both avoids falsely eliminating teams whose
    # group is still in progress while the bracket fills in.)
    group_matches = [m for m in matches if m.get("stage") == "GROUP_STAGE"]
    group_done = bool(group_matches) and all(
        m.get("status") in FINISHED for m in group_matches
    )
    if group_done and knockout_teams:
        for norm_name, (owner, team) in lookup.items():
            if norm_name not in knockout_teams and team not in eliminated:
                # Only mark group-stage exits after that team's group games are done:
                # the team is eliminated if it appears in no knockout fixture at all.
                eliminated.add(team)

    # Leaderboard with shared ranks for ties.
    rows = []
    for owner, teams in assignments["participants"].items():
        rows.append({
            "name": owner,
            "points": round(points[owner], 1),
            "teams": [
                {
                    "name": t,
                    "points": round(team_points.get(t, 0), 1),
                    "eliminated": t in eliminated,
                }
                for t in teams
            ],
        })
    rows.sort(key=lambda r: (-r["points"], r["name"].lower()))
    rank, prev_pts = 0, None
    for i, row in enumerate(rows, start=1):
        if row["points"] != prev_pts:
            rank, prev_pts = i, row["points"]
        row["rank"] = rank

    live = [m for m in display_matches if m["status"] in LIVE]
    finished = [m for m in display_matches if m["status"] in FINISHED][-12:]
    upcoming = [m for m in display_matches if m["status"] in UPCOMING][:12]

    payload = {
        "leaderboard": rows,
        "events": events[-20:][::-1],
        "live": live,
        "recent": finished[::-1],
        "upcoming": upcoming,
        "scoring": scoring,
        "tournamentStarted": any(m["status"] in FINISHED | LIVE for m in display_matches),
    }

    out_path = ROOT / "data.json"
    if out_path.exists():
        try:
            existing = load_json(out_path)
            existing.pop("generated", None)
            if existing == payload:
                print("No changes since last run; data.json left untouched.")
                return
        except (json.JSONDecodeError, OSError):
            pass

    payload["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote data.json — {len(matches)} matches processed, "
          f"{len(events)} scoring events.")


if __name__ == "__main__":
    main()
