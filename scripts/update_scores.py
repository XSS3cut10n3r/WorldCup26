#!/usr/bin/env python3
"""
World Cup 2026 family leaderboard updater.

Fetches all World Cup matches from football-data.org, applies the family
scoring rules from config.json to the team assignments in assignments.json,
and writes data.json for the website to display.

Run by GitHub Actions on a schedule. Requires the environment variable
FOOTBALL_DATA_TOKEN (a free football-data.org API key).

No third-party dependencies — standard library only.

GROUP STRUCTURE IS HARDCODED. The 2026 draw is fixed, so which teams sit in
which group (and the group names) come from groups.json, never from the API.
Only match results come from the API. This means an unreliable standings feed
can no longer reshape, rename, or break the group tables.
"""

import itertools
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

    # Hardcoded official group composition (groups.json). This is the single
    # source of truth for group membership, names, and letters — the API is
    # never consulted for group structure.
    groups_def = load_json(ROOT / "groups.json")
    team2group = {}
    for letter, teams in groups_def.items():
        for t in teams:
            team2group[normalize(t)] = letter

    matches = fetch_matches(token)
    matches.sort(key=lambda m: m.get("utcDate") or "")

    # Running state
    points = {owner: 0.0 for owner in assignments["participants"]}
    team_points = {}   # canonical team name -> points earned
    eliminated = set() # canonical team names knocked out
    events = []        # scoring events, newest last

    display_matches = []
    # Per-team group record computed from FINISHED matches ONLY, keyed by
    # normalized team name. The free-tier standings feed folds in the live
    # scoreline of in-progress games (a 0-0 kickoff shows as a played draw),
    # so we rebuild the table numbers from settled results instead.
    gstats = {}
    # Finished group matches per group letter, as (home_norm, away_norm,
    # home_goals, away_goals) — used for head-to-head tiebreaks. The group
    # letter is resolved from the hardcoded mapping, not the API's label.
    ghead = {}
    # Unplayed group fixtures per group letter, as (home_norm, away_norm) —
    # pairings only (no scoreline). Drives the group-winner clinch math.
    group_remaining = {}

    for m in matches:
        stage = m.get("stage", "")
        status = m.get("status", "")
        score = m.get("score", {}) or {}
        full_time = score.get("fullTime", {}) or {}
        # football-data.org reports a shootout via duration == PENALTY_SHOOTOUT
        # and ADDS the shootout tally onto the 120' score in fullTime (e.g. a
        # 1-1 that ends 6-5 on penalties shows fullTime 7-6, penalties 6-5). So
        # the tied scoreline we display is fullTime minus the penalties, and the
        # shootout score itself comes from the penalties node.
        is_pens = (score.get("duration") == "PENALTY_SHOOTOUT")
        pens = (score.get("penalties") or {}) if is_pens else {}
        pen_home, pen_away = pens.get("home"), pens.get("away")
        home_goals, away_goals = full_time.get("home"), full_time.get("away")
        if is_pens:
            if home_goals is not None and pen_home is not None:
                home_goals -= pen_home
            if away_goals is not None and pen_away is not None:
                away_goals -= pen_away
        home, away = m.get("homeTeam", {}) or {}, m.get("awayTeam", {}) or {}
        home_hit, away_hit = match_team(home, lookup), match_team(away, lookup)

        entry = {
            "stage": STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
            "stageCode": stage,
            "status": status,
            "utcDate": m.get("utcDate"),
            "home": {
                "name": home.get("name") or "TBD",
                "owner": home_hit[0] if home_hit else None,
                "goals": home_goals,
                "penGoals": pen_home,
            },
            "away": {
                "name": away.get("name") or "TBD",
                "owner": away_hit[0] if away_hit else None,
                "goals": away_goals,
                "penGoals": pen_away,
            },
            "penalties": is_pens,
            "winner": score.get("winner"),
        }
        display_matches.append(entry)

        # Tally settled group results into per-team records (finished only).
        if stage == "GROUP_STAGE" and status in FINISHED:
            gh, ga = full_time.get("home"), full_time.get("away")
            if gh is not None and ga is not None:
                hn = normalize(home.get("name") or "")
                an = normalize(away.get("name") or "")
                # Group letter from the hardcoded mapping (robust to whatever
                # the API calls the group, or if it omits it entirely).
                letter = team2group.get(hn) or team2group.get(an)
                if hn and an and letter:
                    ghead.setdefault(letter, []).append((hn, an, gh, ga))
                for tm, gf, gag in ((home, gh, ga), (away, ga, gh)):
                    nm = normalize(tm.get("name") or "")
                    if not nm:
                        continue
                    rec = gstats.setdefault(nm, {
                        "played": 0, "won": 0, "draw": 0, "lost": 0,
                        "gf": 0, "ga": 0, "points": 0})
                    rec["played"] += 1
                    rec["gf"] += gf
                    rec["ga"] += gag
                    if gf > gag:
                        rec["won"] += 1
                        rec["points"] += 3
                    elif gf == gag:
                        rec["draw"] += 1
                        rec["points"] += 1
                    else:
                        rec["lost"] += 1

        # Record still-unplayed group fixtures (pairings only) for clinch math.
        # A live match counts as unplayed: its outcome isn't settled yet.
        if stage == "GROUP_STAGE" and status not in FINISHED:
            hn = normalize(home.get("name") or "")
            an = normalize(away.get("name") or "")
            letter = team2group.get(hn) or team2group.get(an)
            if hn and an and letter:
                group_remaining.setdefault(letter, []).append((hn, an))

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

    # Eliminations from group standings are computed further down, once the
    # group tables and third-place ranking are known — see the
    # "Standings-based eliminations" block. (Knockout losers were already
    # marked during the match loop above.)

    # live / recent / upcoming are built AFTER the bracket (further down),
    # so knockout fixtures show computed teams instead of the raw feed's TBDs.

    # ---- Team conduct (maintained by hand) -----------------------------
    # Cards live in cards-manual.json: { "Team": {"yellow": n,
    # "indirect_red": n, "direct_red": n, "yellow_direct_red": n}, ... }.
    # Edit that file after match days; this script picks it up on its next
    # scheduled run. Scoring: yellow -1, second-yellow red -3, straight
    # red -4, yellow-then-straight-red -5. Computed before the group tables
    # because conduct is a group tiebreaker.
    card_keys = ("yellow", "indirect_red", "direct_red", "yellow_direct_red")
    manual = {}
    manual_path = ROOT / "cards-manual.json"
    if manual_path.exists():
        try:
            manual = {normalize(k): v for k, v in load_json(manual_path).items()}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: couldn't read cards-manual.json ({e}); "
                  "conduct treated as all zeros.")

    conduct_scores = {}
    conduct_teams = []
    for owner, teams in assignments["participants"].items():
        for team in teams:
            raw = manual.get(normalize(team), {})
            counts = {k: int(raw.get(k, 0)) for k in card_keys}
            score = -(counts["yellow"] + 3 * counts["indirect_red"]
                      + 4 * counts["direct_red"] + 5 * counts["yellow_direct_red"])
            conduct_scores[normalize(team)] = score
            conduct_teams.append({"team": team, "owner": owner,
                                  **counts, "score": score})
    conduct_teams.sort(key=lambda r: (-r["score"], r["team"]))
    rank, prev = 0, None
    for i, r in enumerate(conduct_teams, start=1):
        if r["score"] != prev:
            rank, prev = i, r["score"]
        r["rank"] = rank

    # Per-person "worst conduct" leaderboard: sum of their teams' conduct
    # scores, most negative total ranked first.
    conduct_people = []
    for owner, teams in assignments["participants"].items():
        tlist = [{"name": t, "score": conduct_scores[normalize(t)]} for t in teams]
        conduct_people.append({
            "name": owner,
            "score": sum(t["score"] for t in tlist),
            "teams": tlist,
        })
    conduct_people.sort(key=lambda p: (p["score"], p["name"].lower()))
    rank, prev = 0, None
    for i, p in enumerate(conduct_people, start=1):
        if p["score"] != prev:
            rank, prev = i, p["score"]
        p["rank"] = rank

    fifa_ranks = {normalize(k): v
                  for k, v in (config.get("fifaRanking") or {}).items()}

    def rank_group(rows, finished):
        """Order one group by the official 2026 hierarchy, applied recursively.

        Head-to-head (points, GD, goals among the tied teams) is tried first;
        the instant it splits the tied set, each still-level subgroup is
        re-resolved FROM THE TOP using only the matches between its own
        members — so goals run up against a team that's already been separated
        out no longer count. Only when head-to-head can't separate a subgroup
        does it fall to overall GD, goals, conduct score, then FIFA ranking."""

        def overall_key(r):
            return (-r["gd"], -r["gf"], -conduct_scores.get(r["norm"], 0),
                    fifa_ranks.get(r["norm"]) or 999, r["team"])

        def resolve(teams):
            if len(teams) == 1:
                return list(teams)
            names = {r["norm"] for r in teams}
            h2h = {nm: {"pts": 0, "gf": 0, "ga": 0} for nm in names}
            for (hn, an, gh, ga) in finished:
                if hn in names and an in names:
                    h2h[hn]["gf"] += gh; h2h[hn]["ga"] += ga
                    h2h[an]["gf"] += ga; h2h[an]["ga"] += gh
                    if gh > ga:
                        h2h[hn]["pts"] += 3
                    elif gh < ga:
                        h2h[an]["pts"] += 3
                    else:
                        h2h[hn]["pts"] += 1; h2h[an]["pts"] += 1

            def h2h_key(r):
                hh = h2h[r["norm"]]
                return (-hh["pts"], -(hh["gf"] - hh["ga"]), -hh["gf"])

            ordered = sorted(teams, key=h2h_key)
            blocks = []
            for r in ordered:
                if blocks and h2h_key(r) == h2h_key(blocks[-1][0]):
                    blocks[-1].append(r)
                else:
                    blocks.append([r])
            if len(blocks) == 1:           # head-to-head separated nobody
                return sorted(teams, key=overall_key)
            out = []
            for b in blocks:               # re-resolve each still-tied subgroup
                out.extend(resolve(b))
            return out

        ranked = []
        by_points = sorted(rows, key=lambda r: -r["points"])
        i = 0
        while i < len(by_points):
            j = i
            while j < len(by_points) and by_points[j]["points"] == by_points[i]["points"]:
                j += 1
            ranked.extend(resolve(by_points[i:j]))
            i = j
        return ranked

    # ---- Mathematical group-winner clinch detection --------------------
    # Returns the normalized name of the team that has WON its group, or None.
    # "Won" means guaranteed 1st place no matter how the remaining group games
    # go — across every possible scoreline, not just every win/draw/loss.
    #
    # We enumerate each win/draw/loss combination of the group's unplayed
    # matches (3**n, n<=6) and require the candidate to finish 1st in all of
    # them. Within a combination, a clinch is only credited to information that
    # future scorelines CANNOT change: points and head-to-head RESULTS are
    # fixed by the win/draw/loss pattern; conduct and FIFA rank are fixed
    # outright; but a goal-difference or goals-scored separation is trusted
    # only when every match feeding it is already finished (head-to-head GD/GF
    # needs the matches among the tied teams played; overall GD/GF needs those
    # teams to have completed all three games). Any tie that still hinges on a
    # mutable GD is treated as unsecured — which is correct, because the
    # trailing team could always erase that gap by winning its last game big.
    # Result: sound (never a false clinch) and complete (fires the moment a
    # clinch becomes real, e.g. a leader whose only possible points-rival has
    # already lost to it head-to-head).
    def group_winner(letter):
        teams = [normalize(t) for t in groups_def[letter]]
        rec = lambda t: gstats.get(t, {"played": 0, "gf": 0, "ga": 0, "points": 0})
        pts0 = {t: rec(t)["points"] for t in teams}
        gd0 = {t: rec(t)["gf"] - rec(t)["ga"] for t in teams}
        gf0 = {t: rec(t)["gf"] for t in teams}
        pl = {t: rec(t)["played"] for t in teams}
        fin = ghead.get(letter, [])
        rem = group_remaining.get(letter, [])
        has_rem = {t: False for t in teams}
        for (hn, an) in rem:
            if hn in has_rem:
                has_rem[hn] = True
            if an in has_rem:
                has_rem[an] = True
        cond = {t: conduct_scores.get(t, 0) for t in teams}
        fifa = {t: fifa_ranks.get(t) for t in teams}

        def h2h(subgroup, assigned):
            names = set(subgroup)
            hp = {t: 0 for t in subgroup}
            hgd = {t: 0 for t in subgroup}
            hgf = {t: 0 for t in subgroup}
            for (hn, an, gh, ga) in fin:          # finished: full scoreline known
                if hn in names and an in names:
                    hgf[hn] += gh; hgf[an] += ga
                    hgd[hn] += gh - ga; hgd[an] += ga - gh
                    if gh > ga:
                        hp[hn] += 3
                    elif gh < ga:
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            for (hn, an, o) in assigned:          # assumed: result only, no score
                if hn in names and an in names:
                    if o == "H":
                        hp[hn] += 3
                    elif o == "A":
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            return hp, hgd, hgf

        def secured_first(x, subgroup, assigned):
            """True iff x is guaranteed strictly 1st within this tied set,
            using only separations future scorelines can't overturn."""
            if len(subgroup) == 1:
                return True
            names = set(subgroup)
            hp, hgd, hgf = h2h(subgroup, assigned)
            # Head-to-head GD/GF are usable only if no match among these teams
            # is still to be played in this scenario.
            intra_open = any(h in names and a in names for (h, a, _) in assigned)
            hkey = (lambda t: (hp[t],)) if intra_open else \
                   (lambda t: (hp[t], hgd[t], hgf[t]))
            best = max(hkey(t) for t in subgroup)
            top = [t for t in subgroup if hkey(t) == best]
            if x not in top:
                return False
            if len(top) == 1:
                return True
            if len(top) < len(subgroup):
                return secured_first(x, top, assigned)   # re-apply to reduced set
            # Head-to-head can't separate this set. Overall criteria are only
            # trustworthy once every team here has played all three games.
            if all((not has_rem[t]) and pl[t] >= 3 for t in subgroup):
                okey = lambda t: (gd0[t], gf0[t], cond[t], -(fifa[t] or 999))
                return all(okey(x) > okey(t) for t in subgroup if t != x)
            return False

        def guaranteed_first(x, pts, assigned):
            xp = pts[x]
            if any(pts[t] > xp for t in teams if t != x):
                return False
            tie = [t for t in teams if pts[t] == xp]
            if len(tie) == 1:
                return True
            return secured_first(x, tie, assigned)

        for x in teams:
            clinched = True
            for combo in itertools.product("HDA", repeat=len(rem)):
                pts = dict(pts0)
                assigned = []
                for (hn, an), o in zip(rem, combo):
                    if o == "H":
                        pts[hn] += 3
                    elif o == "A":
                        pts[an] += 3
                    else:
                        pts[hn] += 1; pts[an] += 1
                    assigned.append((hn, an, o))
                if not guaranteed_first(x, pts, assigned):
                    clinched = False
                    break
            if clinched:
                return x        # at most one team can clinch 1st place
        return None

    # ---- Mathematical group-stage elimination --------------------------
    # The dual of the clinch above: returns the normalized name of the team
    # that can NO LONGER reach its group's top three, or None. In a four-team
    # group "top three" == "not last", so this fires exactly when a team is
    # guaranteed to finish 4th across every remaining win/draw/loss pattern.
    # Only the top three of a group can ever qualify (two automatic places plus
    # the best-third race), so a team locked into last is mathematically out
    # even with games still to play — the case the standings block below would
    # otherwise miss until the group is complete.
    #
    # Soundness mirrors the clinch (never a false elimination): a rival is
    # credited as ABOVE the team only via separations the team cannot overturn
    # — points and head-to-head RESULTS are fixed by the win/draw/loss pattern,
    # but any tie still hinging on a mutable goal difference is left open,
    # because the trailing team could always erase it by winning big. At most
    # one team per group can be guaranteed last.
    def group_last(letter):
        teams = [normalize(t) for t in groups_def[letter]]
        rec = lambda t: gstats.get(t, {"played": 0, "gf": 0, "ga": 0, "points": 0})
        pts0 = {t: rec(t)["points"] for t in teams}
        gd0 = {t: rec(t)["gf"] - rec(t)["ga"] for t in teams}
        gf0 = {t: rec(t)["gf"] for t in teams}
        pl = {t: rec(t)["played"] for t in teams}
        fin = ghead.get(letter, [])
        rem = group_remaining.get(letter, [])
        has_rem = {t: False for t in teams}
        for (hn, an) in rem:
            if hn in has_rem:
                has_rem[hn] = True
            if an in has_rem:
                has_rem[an] = True
        cond = {t: conduct_scores.get(t, 0) for t in teams}
        fifa = {t: fifa_ranks.get(t) for t in teams}

        def h2h(subgroup, assigned):
            names = set(subgroup)
            hp = {t: 0 for t in subgroup}
            hgd = {t: 0 for t in subgroup}
            hgf = {t: 0 for t in subgroup}
            for (hn, an, gh, ga) in fin:
                if hn in names and an in names:
                    hgf[hn] += gh; hgf[an] += ga
                    hgd[hn] += gh - ga; hgd[an] += ga - gh
                    if gh > ga:
                        hp[hn] += 3
                    elif gh < ga:
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            for (hn, an, o) in assigned:
                if hn in names and an in names:
                    if o == "H":
                        hp[hn] += 3
                    elif o == "A":
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            return hp, hgd, hgf

        def secured_last(x, subgroup, assigned):
            """True iff x is guaranteed strictly LAST within this tied set,
            using only separations future scorelines can't overturn for x."""
            if len(subgroup) == 1:
                return True
            names = set(subgroup)
            hp, hgd, hgf = h2h(subgroup, assigned)
            intra_open = any(h in names and a in names for (h, a, _) in assigned)
            hkey = (lambda t: (hp[t],)) if intra_open else \
                   (lambda t: (hp[t], hgd[t], hgf[t]))
            worst = min(hkey(t) for t in subgroup)
            bottom = [t for t in subgroup if hkey(t) == worst]
            if x not in bottom:
                return False
            if len(bottom) == 1:
                return True
            if len(bottom) < len(subgroup):
                return secured_last(x, bottom, assigned)
            # Head-to-head can't separate this set. Overall criteria are only
            # trustworthy once every team here has played all three games (no
            # mutable goal difference left for x to exploit).
            if all((not has_rem[t]) and pl[t] >= 3 for t in subgroup):
                okey = lambda t: (gd0[t], gf0[t], cond[t], -(fifa[t] or 999))
                return all(okey(x) < okey(t) for t in subgroup if t != x)
            return False

        def guaranteed_last(x, pts, assigned):
            xp = pts[x]
            if any(pts[t] < xp for t in teams if t != x):
                return False            # someone is below x on points
            tie = [t for t in teams if pts[t] == xp]
            if len(tie) == 1:
                return True             # x uniquely lowest on points
            return secured_last(x, tie, assigned)

        for x in teams:
            doomed = True
            for combo in itertools.product("HDA", repeat=len(rem)):
                pts = dict(pts0)
                assigned = []
                for (hn, an), o in zip(rem, combo):
                    if o == "H":
                        pts[hn] += 3
                    elif o == "A":
                        pts[an] += 3
                    else:
                        pts[hn] += 1; pts[an] += 1
                    assigned.append((hn, an, o))
                if not guaranteed_last(x, pts, assigned):
                    doomed = False
                    break
            if doomed:
                return x        # at most one team can be guaranteed last
        return None

    clinched_by_letter = {letter: ({w} if (w := group_winner(letter)) else set())
                          for letter in groups_def}

    # ---- Group tables & best third-place race -------------------------
    # Built from the HARDCODED groups.json (membership, names, letters) plus
    # the per-team records computed from finished matches above. No API
    # standings feed is involved, so the groups can never be reshaped,
    # renamed, or dropped by a bad/rate-limited response.
    groups = []
    for letter in sorted(groups_def):          # "A" .. "L"
        table = []
        for tname in groups_def[letter]:
            nm = normalize(tname)
            hit = lookup.get(nm)
            rec = gstats.get(nm, {"played": 0, "won": 0, "draw": 0,
                                  "lost": 0, "gf": 0, "ga": 0, "points": 0})
            table.append({
                "pos": None,
                "team": tname,
                "norm": nm,
                "owner": hit[0] if hit else None,
                "played": rec["played"],
                "won": rec["won"],
                "draw": rec["draw"],
                "lost": rec["lost"],
                "gf": rec["gf"],
                "ga": rec["ga"],
                "gd": rec["gf"] - rec["ga"],
                "points": rec["points"],
                "conduct": conduct_scores.get(nm, 0),
                "fifaRank": fifa_ranks.get(nm),
            })
        # Full FIFA tiebreaker ordering (head-to-head, then overall).
        table = rank_group(table, ghead.get(letter, []))
        winners = clinched_by_letter.get(letter, set())
        for i, r in enumerate(table, start=1):
            r["pos"] = i
            # Group winners are locked here (possibly mid-group, via the
            # head-to-head math). Runner-up / third-place locks are filled in
            # later, once group_done and the best-thirds ranking are known.
            r["clinched"] = r["norm"] in winners
            r["clinchType"] = "winner" if r["clinched"] else None
            r.pop("norm", None)
        # A group is "ranked" only once EVERY team has played >=1 game.
        # Until then we don't highlight leaders/third or lock bracket slots.
        ranked = all(r["played"] >= 1 for r in table)
        groups.append({"group": "Group " + letter, "table": table, "ranked": ranked})

    thirds = []
    for g in groups:
        # Only groups where every team has played at least once belong in the
        # best-third race; unranked groups are omitted entirely.
        if not g.get("ranked"):
            continue
        third = next((r for r in g["table"] if r["pos"] == 3), None)
        if third:
            t = dict(third)
            t["group"] = g["group"]
            t["ranked"] = True
            t["conduct"] = conduct_scores.get(normalize(t["team"]), 0)
            t["fifaRank"] = fifa_ranks.get(normalize(t["team"]))
            thirds.append(t)
    # Rank third-placed teams by the same overall criteria used within groups:
    # points, goal difference, goals, conduct (higher is better), FIFA rank.
    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"],
                               -r["conduct"], r["fifaRank"] or 999, r["team"]))
    # Highlight the current top 8 third-placed teams (the Round-of-32 cut)
    # among the groups that are ranked, even while other groups are still
    # being played — this is the live "if it ended now" projection. Unranked
    # groups are already excluded above, so their thirds never appear here.
    for i, t in enumerate(thirds):
        t["rank"] = i + 1
        t["qualifies"] = i < 8
    for i, t in enumerate(thirds):
        key = (t["points"], t["gd"], t["gf"], t["conduct"], t["fifaRank"])
        t["manualTie"] = any(
            (o["points"], o["gd"], o["gf"], o["conduct"], o["fifaRank"]) == key
            for j, o in enumerate(thirds) if j != i
        )

    # ---- Standings-based eliminations & leaderboard --------------------
    # A team is out when: it loses a knockout match (marked in the match
    # loop), it finishes 4th in a COMPLETED group, or — once ALL groups are
    # complete — it's a third-placed team outside the best-8 cut. A group is
    # complete when every one of its four teams has played its three games.
    def _group_played(letter):
        return [gstats.get(normalize(t), {}).get("played", 0)
                for t in groups_def[letter]]
    group_done = {letter: all(p >= 3 for p in _group_played(letter))
                  for letter in groups_def}
    all_groups_done = len(group_done) == 12 and all(group_done.values())

    qualified_third_names = {normalize(t["team"]) for t in thirds
                             if t.get("qualifies")}
    # A third-placed team is only locked into the Round of 32 once EVERY group
    # is complete and the best-eight ranking is final (matching the group-row
    # logic below); until then its top-8 slot is just a live projection.
    for t in thirds:
        t["clinched"] = all_groups_done and bool(t.get("qualifies"))
        t["clinchType"] = "third" if t["clinched"] else None
    for g in groups:
        letter = g["group"].split()[-1]
        if not group_done.get(letter, False):
            continue
        for r in g["table"]:
            out = (r["pos"] == 4) or (
                r["pos"] == 3 and all_groups_done
                and normalize(r["team"]) not in qualified_third_names)
            if out:
                hit = lookup.get(normalize(r["team"]))
                if hit:
                    eliminated.add(hit[1])

    # Mathematical elimination for groups still in progress: a team that can no
    # longer reach its group's top three is out now, not only once the group
    # finishes. (Completed groups are covered by the pos==4 rule above.)
    for letter in groups_def:
        if group_done.get(letter, False):
            continue
        dead = group_last(letter)
        if dead:
            hit = lookup.get(dead)
            if hit:
                eliminated.add(hit[1])

    # ---- Runner-up & third-place qualification locks -------------------
    # Unlike a group winner (which the head-to-head math can lock mid-group),
    # these are only certain once the games are in:
    #   * a RUNNER-UP is through the instant its own group is complete, since
    #     the top two of every group advance automatically; and
    #   * a THIRD-PLACED team is through only once EVERY group is complete and
    #     the best-eight-of-twelve ranking is final — until then a third still
    #     sitting in the cut could be bumped by a third from an unfinished
    #     group. So third-place locks all light up together at group-stage end.
    for g in groups:
        letter = g["group"].split()[-1]
        for r in g["table"]:
            if r.get("clinched"):
                continue
            if r["pos"] == 2 and group_done.get(letter, False):
                r["clinched"] = True
                r["clinchType"] = "second"
            elif (r["pos"] == 3 and all_groups_done
                  and normalize(r["team"]) in qualified_third_names):
                r["clinched"] = True
                r["clinchType"] = "third"

    # Per-team advancement, derived from the group tables above, for reuse on
    # the leaderboard chips.
    advanced_norms = {normalize(r["team"]) for g in groups for r in g["table"]
                      if r.get("clinched")}
    clinch_type_by_norm = {normalize(r["team"]): r.get("clinchType")
                           for g in groups for r in g["table"]
                           if r.get("clinched")}

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
                    # True once this team is guaranteed into the Round of 32
                    # (won its group, or locked as a runner-up / top-8 third).
                    "clinched": normalize(t) in advanced_norms,
                    "clinchType": clinch_type_by_norm.get(normalize(t)),
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

    # ---- Knockout bracket: official template + live projection ---------
    # bracket-template.json holds FIFA's published structure: the R32 slot
    # assignments, how winners feed through the rounds, and the full Annex C
    # table (all 495 combinations) deciding which qualified third-placed
    # team faces which group winner. During the group stage the bracket is
    # filled with PROJECTED teams from the current standings; slots harden
    # into confirmed teams as groups finish (or as a winner clinches), and
    # real results attach from the API by team identity.
    template = load_json(ROOT / "bracket-template.json")

    slot_resolved = {}  # "1A"/"2A" -> {"name", "owner", "projected"}
    for g in groups:
        letter = g["group"].split()[-1]
        # Don't project a winner/runner-up until every team in the group has
        # played at least once; until then the slot shows its generic label.
        if not g.get("ranked"):
            continue
        for pos, prefix in ((1, "1"), (2, "2")):
            row = next((r for r in g["table"] if r["pos"] == pos), None)
            if row:
                # A clinched group winner is locked in even before the group
                # finishes, so its slot counts as confirmed (not projected).
                clinched = (prefix == "1"
                            and normalize(row["team"]) in clinched_by_letter.get(letter, set()))
                slot_resolved[prefix + letter] = {
                    "name": row["team"], "owner": row["owner"],
                    "projected": not (group_done.get(letter, False) or clinched),
                }

    third_assign = {}  # winner-slot letter -> third-placed side dict
    qual = [t for t in thirds if t.get("qualifies")]
    # Allowed third-place groups for each Round-of-32 slot (host winner group
    # letter -> set of group letters whose third may land there).
    allowed = {}
    for mdef in template["r32"]:
        if mdef["away"].startswith("T:"):
            allowed[mdef["home"][1]] = set(mdef["away"][2:])
    if len(qual) == 8:
        # Exactly eight qualifiers: FIFA's Annex C gives the official mapping.
        combo = "".join(sorted(t["group"].split()[-1] for t in qual))
        assignment = template["thirdAllocation"].get(combo)
        if assignment:
            by_group = {t["group"].split()[-1]: t for t in qual}
            for slot, gletter in zip(template["thirdSlots"], assignment):
                t = by_group[gletter]
                third_assign[slot] = {
                    "name": t["team"], "owner": t["owner"],
                    "projected": not all_groups_done,
                }
    elif qual:
        # Fewer than eight ranked groups: Annex C isn't defined, so place the
        # available thirds best-effort into slots whose allowed-group set
        # matches, maximizing how many fit (bipartite matching, rank order).
        # Tentative — re-derived exactly via Annex C once 8 groups are ranked.
        qg = [(t, t["group"].split()[-1]) for t in qual]
        match_for_slot = {}

        def assign(ti, seen):
            gl = qg[ti][1]
            for slot in template["thirdSlots"]:
                if gl in allowed.get(slot, set()) and slot not in seen:
                    seen.add(slot)
                    if slot not in match_for_slot or assign(match_for_slot[slot], seen):
                        match_for_slot[slot] = ti
                        return True
            return False

        for ti in range(len(qg)):
            assign(ti, set())
        for slot, ti in match_for_slot.items():
            t = qg[ti][0]
            third_assign[slot] = {
                "name": t["team"], "owner": t["owner"], "projected": True,
            }

    api_real = {}  # stageCode -> fixtures with both teams known
    api_tbd = {}   # stageCode -> TBD fixtures (date fallback), date order
    for m in display_matches:
        if m["stageCode"] == "GROUP_STAGE":
            continue
        if m["home"]["name"] != "TBD" and m["away"]["name"] != "TBD":
            api_real.setdefault(m["stageCode"], []).append(m)
        else:
            api_tbd.setdefault(m["stageCode"], []).append(m)
    for v in api_tbd.values():
        v.sort(key=lambda e: e["utcDate"] or "")

    results = {}  # match number -> {"winner": side, "loser": side}

    def side_from_code(code, match_def):
        if code[0] in "12":
            label = ("Winner Group " if code[0] == "1"
                     else "Runner-up Group ") + code[1]
            r = slot_resolved.get(code)
        elif code.startswith("T:"):
            label = "3rd place " + "/".join(code[2:])
            r = third_assign.get(match_def["home"][1])
        else:  # W## / L##
            ref = int(code[1:])
            label = ("Winner" if code[0] == "W" else "Loser") + f" match {ref}"
            r = results.get(ref, {}).get(
                "winner" if code[0] == "W" else "loser")
        if r:
            return {"name": r["name"], "owner": r.get("owner"),
                    "goals": None, "penGoals": None,
                    "projected": bool(r.get("projected")),
                    "slot": label, "resolved": True}
        return {"name": "TBD", "owner": None, "goals": None, "penGoals": None,
                "projected": False, "slot": label, "resolved": False}

    def build_round(stage_code, defs):
        entries = []
        used = set()
        for mdef in sorted(defs, key=lambda d: d["match"]):
            home = side_from_code(mdef["home"], mdef)
            away = side_from_code(mdef["away"], mdef)
            entry = {
                "matchNo": mdef["match"],
                "stage": STAGE_LABELS.get(stage_code, stage_code),
                "stageCode": stage_code,
                "status": "SCHEDULED", "utcDate": None,
                "home": home, "away": away,
                "penalties": False, "winner": None,
            }
            # Attach the real fixture by team identity: any confirmed
            # (non-projected) side appearing in an API fixture pins it.
            fixture = None
            for f in api_real.get(stage_code, []):
                if id(f) in used:
                    continue
                fnames = {normalize(f["home"]["name"]),
                          normalize(f["away"]["name"])}
                for s in (home, away):
                    if s["resolved"] and not s["projected"] \
                            and normalize(s["name"]) in fnames:
                        fixture = f
                        break
                if fixture:
                    break
            if fixture:
                used.add(id(fixture))
                # Align fixture orientation to the template's home side.
                fh, fa = fixture["home"], fixture["away"]
                flip = (home["resolved"] and not home["projected"]
                        and normalize(home["name"]) == normalize(fa["name"])) \
                    or (away["resolved"] and not away["projected"]
                        and normalize(away["name"]) == normalize(fh["name"]))
                if flip:
                    fh, fa = fa, fh
                for side, f_side in ((home, fh), (away, fa)):
                    side.update(name=f_side["name"], owner=f_side["owner"],
                                goals=f_side["goals"], penGoals=f_side.get("penGoals"),
                                projected=False, resolved=True)
                entry["status"] = fixture["status"]
                entry["utcDate"] = fixture["utcDate"]
                entry["penalties"] = fixture["penalties"]
                w = fixture.get("winner")
                if w and w != "DRAW":
                    if flip:
                        w = "AWAY_TEAM" if w == "HOME_TEAM" else "HOME_TEAM"
                    entry["winner"] = w
                    if fixture["status"] in FINISHED:
                        win_side = home if w == "HOME_TEAM" else away
                        lose_side = away if w == "HOME_TEAM" else home
                        results[mdef["match"]] = {
                            "winner": {"name": win_side["name"],
                                       "owner": win_side["owner"],
                                       "projected": False},
                            "loser": {"name": lose_side["name"],
                                      "owner": lose_side["owner"],
                                      "projected": False},
                        }
            entries.append(entry)
        # Date fallback: unattached template matches inherit the dates of
        # the API's still-TBD fixtures for this stage, in schedule order.
        pool = list(api_tbd.get(stage_code, []))
        for entry in entries:
            if entry["utcDate"] is None and pool:
                entry["utcDate"] = pool.pop(0)["utcDate"]
        return entries

    bracket = []
    for stage_code, key in (("LAST_32", "r32"), ("LAST_16", "r16"),
                            ("QUARTER_FINALS", "qf"), ("SEMI_FINALS", "sf"),
                            ("FINAL", "final"), ("THIRD_PLACE", "third")):
        entries = build_round(stage_code, template[key])
        if entries:
            bracket.append({
                "stage": STAGE_LABELS.get(stage_code, stage_code),
                "stageCode": stage_code,
                "matches": entries,
            })
    # FINAL/THIRD_PLACE depend on SEMI results; rebuild them now that the
    # results dict is fully populated (loser/winner refs resolve in order,
    # so this second pass only matters if rounds were processed before
    # their feeders — they aren't, but the final pair re-checks cheaply).
    bracket.sort(key=lambda r: ["LAST_32", "LAST_16", "QUARTER_FINALS",
                                "SEMI_FINALS", "FINAL",
                                "THIRD_PLACE"].index(r["stageCode"]))

    # ---- Homepage match lists (bracket-aware) ---------------------------
    # Group games come from the API feed; knockout games come from the
    # computed bracket, which always knows the teams: confirmed once groups
    # finish, projected (italics) before that, and slot labels like
    # "Winner match 89" only where a result genuinely doesn't exist yet.
    ko_entries = [m for r in bracket for m in r["matches"]]
    group_entries = [m for m in display_matches
                     if m["stageCode"] == "GROUP_STAGE"]
    live = [e for e in group_entries + ko_entries if e["status"] in LIVE]
    finished = sorted(
        [e for e in group_entries + ko_entries if e["status"] in FINISHED],
        key=lambda e: e["utcDate"] or "")[-5:]
    upcoming = sorted(
        [e for e in group_entries if e["status"] in UPCOMING] +
        [e for e in ko_entries
         if e["status"] not in FINISHED and e["status"] not in LIVE],
        key=lambda e: e["utcDate"] or "9999-12-31T00:00:00Z")[:10]

    # Attach FIFA world ranking to each side shown in the leaderboard match
    # lists (display only — no scoring, bracket, or group logic depends on it).
    for entry in live + finished + upcoming:
        for side in (entry.get("home"), entry.get("away")):
            if side and side.get("name") and side["name"] != "TBD":
                side["fifaRank"] = fifa_ranks.get(normalize(side["name"]))

    # ---- "Winner clinches" pills for upcoming group games ---------------
    # For each upcoming group fixture we ask, rigorously: if THIS game ends a
    # particular way, is a given team then guaranteed a top-2 finish (or the
    # group outright) no matter how every other unplayed group game goes? The
    # same conservative, scoreline-agnostic enumeration the group-winner clinch
    # uses is generalized here to "guaranteed top-k": points and head-to-head
    # RESULTS are fixed by each win/draw/loss pattern, conduct and FIFA are
    # fixed, and goal-difference separations are trusted only once the matches
    # feeding them are actually played. Because group order depends solely on
    # intra-group results, this is exact — and it stays silent unless the
    # guarantee genuinely holds (so a marquee game can correctly show nothing).
    def group_analyzer(letter):
        teams = [normalize(t) for t in groups_def[letter]]
        rec = lambda t: gstats.get(t, {"played": 0, "gf": 0, "ga": 0, "points": 0})
        pts0 = {t: rec(t)["points"] for t in teams}
        gd0 = {t: rec(t)["gf"] - rec(t)["ga"] for t in teams}
        gf0 = {t: rec(t)["gf"] for t in teams}
        pl = {t: rec(t)["played"] for t in teams}
        fin = ghead.get(letter, [])
        rem = group_remaining.get(letter, [])
        has_rem = {t: False for t in teams}
        for (hn, an) in rem:
            if hn in has_rem:
                has_rem[hn] = True
            if an in has_rem:
                has_rem[an] = True
        cond = {t: conduct_scores.get(t, 0) for t in teams}
        fifa = {t: fifa_ranks.get(t) for t in teams}

        def h2h(subgroup, assigned):
            names = set(subgroup)
            hp = {t: 0 for t in subgroup}
            hgd = {t: 0 for t in subgroup}
            hgf = {t: 0 for t in subgroup}
            for (hn, an, gh, ga) in fin:
                if hn in names and an in names:
                    hgf[hn] += gh; hgf[an] += ga
                    hgd[hn] += gh - ga; hgd[an] += ga - gh
                    if gh > ga:
                        hp[hn] += 3
                    elif gh < ga:
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            for (hn, an, o) in assigned:
                if hn in names and an in names:
                    if o == "H":
                        hp[hn] += 3
                    elif o == "A":
                        hp[an] += 3
                    else:
                        hp[hn] += 1; hp[an] += 1
            return hp, hgd, hgf

        def max_above(x, subgroup, assigned):
            """Greatest number of subgroup teams that could finish strictly
            above x, treating any still-mutable goal difference adversarially.
            (Mirrors the group-winner soundness, generalized from 1st to top-k.)"""
            if len(subgroup) <= 1:
                return 0
            names = set(subgroup)
            hp, hgd, hgf = h2h(subgroup, assigned)
            intra_open = any(h in names and a in names for (h, a, _) in assigned)
            hkey = (lambda t: (hp[t],)) if intra_open else \
                   (lambda t: (hp[t], hgd[t], hgf[t]))
            kx = hkey(x)
            above = sum(1 for t in subgroup if t != x and hkey(t) > kx)
            eqx = [t for t in subgroup if t != x and hkey(t) == kx]
            if not eqx:
                return above
            tied = [x] + eqx
            if len(tied) < len(subgroup):
                return above + max_above(x, tied, assigned)   # re-apply to tied set
            if intra_open:                                    # margins free -> all could pass x
                return above + len(eqx)
            if all((not has_rem[t]) and pl[t] >= 3 for t in subgroup):
                okey = lambda t: (gd0[t], gf0[t], cond[t], -(fifa[t] or 999))
                kx2 = okey(x)
                return above + sum(1 for t in eqx if okey(t) > kx2)
            return above + len(eqx)

        def guaranteed_topk(x, pts, assigned, k):
            a = sum(1 for t in teams if t != x and pts[t] > pts[x])
            if a >= k:
                return False
            tied = [x] + [t for t in teams if t != x and pts[t] == pts[x]]
            return a + max_above(x, tied, assigned) <= k - 1

        def enumerate_guaranteed(forced, team, k):
            others = [i for i in range(len(rem)) if i not in forced]
            for combo in itertools.product("HDA", repeat=len(others)):
                pts = dict(pts0)
                assigned = []
                for i, o in list(forced.items()) + list(zip(others, combo)):
                    hn, an = rem[i]
                    if o == "H":
                        pts[hn] += 3
                    elif o == "A":
                        pts[an] += 3
                    else:
                        pts[hn] += 1; pts[an] += 1
                    assigned.append((hn, an, o))
                if not guaranteed_topk(team, pts, assigned, k):
                    return False
            return True

        return teams, rem, enumerate_guaranteed

    _analyzers = {}
    for entry in upcoming:
        if entry["stageCode"] != "GROUP_STAGE" or entry["status"] not in UPCOMING:
            continue
        hn = normalize(entry["home"]["name"] or "")
        an = normalize(entry["away"]["name"] or "")
        letter = team2group.get(hn) or team2group.get(an)
        if not (hn and an and letter):
            continue
        if letter not in _analyzers:
            _analyzers[letter] = group_analyzer(letter)
        _teams, rem, eg = _analyzers[letter]
        idx = next((i for i, (rh, ra) in enumerate(rem) if {rh, ra} == {hn, an}), None)
        if idx is None:
            continue
        rh, _ra = rem[idx]
        home_win = "H" if rh == hn else "A"
        away_win = "H" if rh == an else "A"
        home_clinched = hn in advanced_norms
        away_clinched = an in advanced_norms

        h_adv = eg({idx: home_win}, hn, 2)
        a_adv = eg({idx: away_win}, an, 2)
        h_grp = eg({idx: home_win}, hn, 1)
        a_grp = eg({idx: away_win}, an, 1)
        draw_both = eg({idx: "D"}, hn, 2) and eg({idx: "D"}, an, 2)

        either_in = home_clinched or away_clinched
        winner_group = h_grp and a_grp and not either_in
        winner_adv = h_adv and a_adv and not winner_group and not either_in
        show_symmetric = winner_group or winner_adv
        # Directional "win to advance" only when the symmetric pill doesn't
        # already cover that side, and that side isn't already through.
        home_tag = h_adv and not show_symmetric and not home_clinched
        away_tag = a_adv and not show_symmetric and not away_clinched

        # Mark already-qualified sides so the page can gold the name + "Through".
        entry["home"]["clinched"] = home_clinched
        entry["away"]["clinched"] = away_clinched

        if (winner_group or winner_adv or draw_both or home_tag or away_tag):
            entry["clinch"] = {
                "group": letter,
                "winnerWinsGroup": bool(winner_group),
                "winnerAdvances": bool(winner_adv),
                "drawBoth": bool(draw_both and not either_in),
                "home": {"winAdvances": bool(home_tag)},
                "away": {"winAdvances": bool(away_tag)},
            }


    # ---- Raw ingredients for the in-browser "simulate remaining games" ----
    # The page is otherwise a dumb renderer of computed data; to let it roll
    # random what-if results client-side it needs the inputs this script keeps
    # to itself: the fixtures still to play, who's in each group, the bracket
    # template (including FIFA's Annex C third-place map), the scoring table,
    # and per-team owner/conduct/FIFA. Everything is keyed by the canonical
    # groups.json name so the browser never has to re-normalize.
    norm2disp = {}
    for _letter, _tnames in groups_def.items():
        for _t in _tnames:
            norm2disp[normalize(_t)] = _t

    def _disp(nm):
        return norm2disp.get(nm)

    sim_group_results = {}
    sim_group_remaining = {}
    for letter in groups_def:
        sim_group_results[letter] = [
            {"h": _disp(hn), "a": _disp(an), "hg": gh, "ag": ga}
            for (hn, an, gh, ga) in ghead.get(letter, [])
            if _disp(hn) and _disp(an)
        ]
        sim_group_remaining[letter] = [
            {"h": _disp(hn), "a": _disp(an)}
            for (hn, an) in group_remaining.get(letter, [])
            if _disp(hn) and _disp(an)
        ]

    sim_teams = {}
    for letter, tnames in groups_def.items():
        for t in tnames:
            nm = normalize(t)
            hit = lookup.get(nm)
            sim_teams[t] = {
                "owner": hit[0] if hit else None,
                "canon": hit[1] if hit else None,   # name as shown on leaderboard chips
                "conduct": conduct_scores.get(nm, 0),
                "fifa": fifa_ranks.get(nm),
                "group": letter,
            }

    sim_ko_played = []
    for m in display_matches:
        if m["stageCode"] == "GROUP_STAGE" or m["status"] not in FINISHED:
            continue
        hd = _disp(normalize(m["home"]["name"] or ""))
        ad = _disp(normalize(m["away"]["name"] or ""))
        if not hd or not ad:
            continue
        sim_ko_played.append({
            "stageCode": m["stageCode"], "home": hd, "away": ad,
            "homeGoals": m["home"]["goals"], "awayGoals": m["away"]["goals"],
            "penalties": m["penalties"],
            "penHome": m["home"]["penGoals"], "penAway": m["away"]["penGoals"],
            "winner": m["winner"],
        })

    payload = {
        "leaderboard": rows,
        "events": events[-5:][::-1],
        "live": live,
        "recent": finished[::-1],
        "upcoming": upcoming,
        "bracket": bracket,
        "groups": groups,
        "thirdPlace": thirds,
        "conduct": {
            "teams": conduct_teams,
            "people": conduct_people,
            "hasData": any(t["score"] != 0 for t in conduct_teams),
        },
        "scoring": scoring,
        "tournamentStarted": any(m["status"] in FINISHED | LIVE for m in display_matches),
        "sim": {
            "scoring": scoring,
            "template": template,
            "groupsDef": {letter: list(groups_def[letter]) for letter in groups_def},
            "groupResults": sim_group_results,
            "groupRemaining": sim_group_remaining,
            "teams": sim_teams,
            "knockoutPlayed": sim_ko_played,
        },
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
