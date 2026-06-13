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
STANDINGS_URL = "https://api.football-data.org/v4/competitions/WC/standings"

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


def fetch_standings(token: str) -> list:
    """Group tables. Failure here is non-fatal: scores still update."""
    req = urllib.request.Request(STANDINGS_URL, headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"Warning: standings fetch failed ({e}); group tables skipped this run.")
        return []
    return payload.get("standings", [])


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

    display_matches = []
    group_counts = {}  # group letter -> [finished, total]
    # Per-team group record computed from FINISHED matches ONLY, keyed by
    # normalized team name. The free-tier standings feed folds in the live
    # scoreline of in-progress games (a 0-0 kickoff shows as a played draw),
    # so we rebuild the table numbers from settled results instead.
    gstats = {}

    for m in matches:
        stage = m.get("stage", "")
        status = m.get("status", "")
        if stage == "GROUP_STAGE":
            letter = (m.get("group") or "")[-1:]
            if letter:
                c = group_counts.setdefault(letter, [0, 0])
                c[1] += 1
                if status in FINISHED:
                    c[0] += 1
        score = m.get("score", {}) or {}
        full_time = score.get("fullTime", {}) or {}
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
                "goals": full_time.get("home"),
            },
            "away": {
                "name": away.get("name") or "TBD",
                "owner": away_hit[0] if away_hit else None,
                "goals": full_time.get("away"),
            },
            "penalties": (score.get("duration") == "PENALTY_SHOOTOUT"),
            "winner": score.get("winner"),
        }
        display_matches.append(entry)

        # Tally settled group results into per-team records (finished only).
        if stage == "GROUP_STAGE" and status in FINISHED:
            gh, ga = full_time.get("home"), full_time.get("away")
            if gh is not None and ga is not None:
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

    # ---- Group tables & best third-place race -------------------------
    groups = []
    raw_standings = fetch_standings(token)
    for s in raw_standings:
        # Keep total (not home/away-split) tables that belong to a group.
        # Deliberately no check on s["stage"]: the World Cup standings
        # endpoint only contains group tables, and stage labels have
        # proven unreliable.
        if s.get("type") not in (None, "TOTAL"):
            continue
        raw_group = (s.get("group") or "").strip()
        if not raw_group or not s.get("table"):
            continue
        gname = raw_group.replace("GROUP_", "Group ")
        if not gname.lower().startswith("group"):
            gname = "Group " + gname
        table = []
        for row in s.get("table", []):
            team = row.get("team", {}) or {}
            hit = match_team(team, lookup)
            nm = normalize(team.get("name") or "")
            rec = gstats.get(nm, {"played": 0, "won": 0, "draw": 0,
                                  "lost": 0, "gf": 0, "ga": 0, "points": 0})
            table.append({
                "pos": row.get("position"),  # feed order; used as fallback
                "team": team.get("name", "?"),
                "owner": hit[0] if hit else None,
                "played": rec["played"],
                "won": rec["won"],
                "draw": rec["draw"],
                "lost": rec["lost"],
                "gf": rec["gf"],
                "ga": rec["ga"],
                "gd": rec["gf"] - rec["ga"],
                "points": rec["points"],
            })
        if table:
            # Order by settled results (points, GD, GF), then the feed's own
            # position as a stable tiebreaker so head-to-head ordering from
            # the provider is preserved when our computed stats are level.
            table.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"],
                                      r["pos"] if r["pos"] is not None else 99))
            for i, r in enumerate(table, start=1):
                r["pos"] = i
            groups.append({"group": gname, "table": table})
    groups.sort(key=lambda g: g["group"])
    if raw_standings and not groups:
        s0 = raw_standings[0]
        print(f"Note: {len(raw_standings)} standings entries received but none "
              f"parsed as group tables. First entry: stage={s0.get('stage')!r} "
              f"type={s0.get('type')!r} group={s0.get('group')!r} "
              f"keys={sorted(s0.keys())}")
    elif not raw_standings:
        print("Note: standings endpoint returned no entries "
              "(rate limit or data not yet published).")

    # ---- Team conduct (maintained by hand) -----------------------------
    # Cards live in cards-manual.json: { "Team": {"yellow": n,
    # "indirect_red": n, "direct_red": n, "yellow_direct_red": n}, ... }.
    # Edit that file after match days; this script picks it up on its next
    # scheduled run. Scoring: yellow -1, second-yellow red -3, straight
    # red -4, yellow-then-straight-red -5.
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

    thirds = []
    for g in groups:
        third = next((r for r in g["table"] if r["pos"] == 3), None)
        if third:
            t = dict(third)
            t["group"] = g["group"]
            t["conduct"] = conduct_scores.get(normalize(t["team"]), 0)
            t["fifaRank"] = fifa_ranks.get(normalize(t["team"]))
            thirds.append(t)
    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"],
                               -r["conduct"], r["fifaRank"] or 999, r["team"]))
    for i, t in enumerate(thirds):
        t["rank"] = i + 1
        t["qualifies"] = i < 8  # top 8 of 12 advance to the Round of 32
    for i, t in enumerate(thirds):
        key = (t["points"], t["gd"], t["gf"], t["conduct"], t["fifaRank"])
        t["manualTie"] = any(
            (o["points"], o["gd"], o["gf"], o["conduct"], o["fifaRank"]) == key
            for j, o in enumerate(thirds) if j != i
        )

    # ---- Standings-based eliminations & leaderboard --------------------
    # A team is out when: it loses a knockout match (marked in the match
    # loop), it finishes 4th in a COMPLETED group, or — once ALL groups are
    # complete — it's a third-placed team outside the best-8 cut. This is
    # driven purely by the standings, never by which knockout fixtures the
    # API happens to have published yet.
    group_done = {letter: (c[1] > 0 and c[0] == c[1])
                  for letter, c in group_counts.items()}
    all_groups_done = len(group_done) == 12 and all(group_done.values())

    qualified_third_names = {normalize(t["team"]) for t in thirds
                             if t.get("qualifies")}
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

    # ---- Knockout bracket: official template + live projection ---------
    # bracket-template.json holds FIFA's published structure: the R32 slot
    # assignments, how winners feed through the rounds, and the full Annex C
    # table (all 495 combinations) deciding which qualified third-placed
    # team faces which group winner. During the group stage the bracket is
    # filled with PROJECTED teams from the current standings; slots harden
    # into confirmed teams as groups finish, and real results attach from
    # the API by team identity.
    template = load_json(ROOT / "bracket-template.json")

    slot_resolved = {}  # "1A"/"2A" -> {"name", "owner", "projected"}
    for g in groups:
        letter = g["group"].split()[-1]
        for pos, prefix in ((1, "1"), (2, "2")):
            row = next((r for r in g["table"] if r["pos"] == pos), None)
            if row:
                slot_resolved[prefix + letter] = {
                    "name": row["team"], "owner": row["owner"],
                    "projected": not group_done.get(letter, False),
                }

    third_assign = {}  # winner-slot letter -> third-placed side dict
    qual = [t for t in thirds if t.get("qualifies")]
    if len(qual) == 8:
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
                    "goals": None, "projected": bool(r.get("projected")),
                    "slot": label, "resolved": True}
        return {"name": "TBD", "owner": None, "goals": None,
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
                                goals=f_side["goals"], projected=False,
                                resolved=True)
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
