#!/usr/bin/env python3
"""
espn_live_overlay.py — lightly patch LIVE games into data.json using ESPN.

football-data.org remains the single source of truth: update_scores.py builds the
whole data.json (leaderboard, groups, bracket, conduct, scoring, the sim block).
This script runs AFTER it and does one narrow job — make sure games that are
actually being played show up as live, even when the football-data feed is slow to
flip a fixture to IN_PLAY (which is why live games were vanishing from the
leaderboard page).

It is deliberately minimal and non-authoritative:
  * It only touches the `live` / `recent` / `upcoming` match-card arrays.
  * It NEVER changes leaderboard points, groups, bracket, conduct, or the sim
    block. Points still come from football-data whenever it next updates, so a
    score can briefly lead the points during a feed lag — same as any lag today.
  * Owner / FIFA rank / canonical name for each team are read out of data.json
    itself (sim.teams), so no extra join files or second source are needed.
  * Group stage only. A live group game is the case that was breaking; knockout
    rounds (with penalties / round labels we don't have a sample for) are left to
    football-data untouched.

Idempotent: every card this script adds is tagged "_src", and each run first
removes its own previous tags before re-deriving from ESPN. If nothing is live
(and nothing it previously added needs clearing), data.json is left byte-for-byte
unchanged, so the pipeline's no-change check still fires and no empty commit is
made. If ESPN can't be reached, it warns and leaves data.json untouched (exit 0),
so it can never break the scores pipeline.

Usage:
    python3 scripts/espn_live_overlay.py            # patch ./data.json in place
    DATA_FILE=other.json python3 scripts/espn_live_overlay.py
    ESPN_FIXTURE=sample.json python3 scripts/espn_live_overlay.py   # read a saved
                                                    # scoreboard instead of fetching

Standard library only. No API key.
"""

import copy
import json
import os
import sys
import unicodedata
import urllib.request

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
ESPN_FIXTURE = os.environ.get("ESPN_FIXTURE")  # optional local file, for testing
SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
    "scoreboard?limit=300&dates=20260611-20260719"
)

# ESPN display names -> your canonical names (same direction as sync_cards.py).
NAME_MAP = {
    "Cape Verde": "Cabo Verde",
    "Ivory Coast": "Côte d'Ivoire",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "Turkey": "Türkiye",
    "USA": "United States",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Congo DR": "DR Congo",
}

# Tag we stamp on cards we inject, so each run can cleanly remove its own previous
# additions and re-derive. football-data's own cards never carry this key.
TAG = "_src"
TAG_LIVE = "espn-live"
TAG_FINAL = "espn-final"


def _norm(s):
    """Fold accents, unify apostrophes, lowercase, collapse spaces — for matching
    ESPN spellings to canonical names robustly."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    for ch in "\u2018\u2019\u02bc\u0060\u00b4":
        s = s.replace(ch, "'")
    return " ".join(s.lower().split())


def fetch_espn():
    if ESPN_FIXTURE:
        with open(ESPN_FIXTURE, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(SCOREBOARD, headers={"User-Agent": "live-overlay/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pair_key(home_name, away_name):
    """Order-independent identity for a fixture, robust to spelling/accents."""
    return tuple(sorted((_norm(home_name), _norm(away_name))))


def build_team_lookup(data):
    """canonical name -> {owner, fifa}, plus a normalized-name index, both read
    straight out of data.json so no external join data is needed."""
    meta, by_norm = {}, {}
    for nm, t in ((data.get("sim") or {}).get("teams") or {}).items():
        meta[nm] = {"owner": t.get("owner"), "fifa": t.get("fifa")}
        by_norm[_norm(nm)] = nm
    # groups table as a secondary source (covers any team missing from sim.teams)
    for g in (data.get("groups") or []):
        for r in (g.get("table") or []):
            nm = r.get("team")
            if nm and nm not in meta:
                meta[nm] = {"owner": r.get("owner"), "fifa": r.get("fifaRank")}
                by_norm.setdefault(_norm(nm), nm)
    return meta, by_norm


def canonicalize(espn_name, meta, by_norm):
    """ESPN display name -> canonical name present in data.json, or None."""
    cand = NAME_MAP.get(espn_name, espn_name)
    if cand in meta:
        return cand
    hit = by_norm.get(_norm(cand)) or by_norm.get(_norm(espn_name))
    return hit  # None if we can't confidently place the team


def espn_group_events(espn):
    """Yield (competition, state) for GROUP-STAGE events only."""
    for ev in espn.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        slug = (ev.get("season") or {}).get("slug") or (comp.get("season") or {}).get("slug") or ""
        note = comp.get("altGameNote") or ev.get("name") or ""
        is_group = slug == "group-stage" or "Group" in note
        if not is_group:
            continue
        state = (((comp.get("status") or {}).get("type") or {}).get("state")) or ""
        yield comp, state


def sides(comp):
    """Return (home_competitor, away_competitor) or (None, None)."""
    h = a = None
    for c in comp.get("competitors") or []:
        if c.get("homeAway") == "home":
            h = c
        elif c.get("homeAway") == "away":
            a = c
    return h, a


def goals_of(competitor):
    try:
        return int(competitor.get("score"))
    except (TypeError, ValueError):
        return 0


def team_block(competitor, meta, by_norm, with_goals=True):
    espn_name = (competitor.get("team") or {}).get("displayName") \
        or (competitor.get("team") or {}).get("name") or ""
    canon = canonicalize(espn_name, meta, by_norm)
    if canon is None:
        return None, espn_name
    m = meta.get(canon, {})
    block = {
        "name": canon,
        "owner": m.get("owner"),
        "goals": goals_of(competitor) if with_goals else None,
        "penGoals": None,
        "fifaRank": m.get("fifa"),
    }
    return block, espn_name


def make_card(comp, meta, by_norm, status):
    h, a = sides(comp)
    if not (h and a):
        return None, None
    home, hn = team_block(h, meta, by_norm)
    away, an = team_block(a, meta, by_norm)
    if home is None or away is None:
        # A team we can't place to an owner/rank — skip rather than inject a
        # broken card. (Returns the unmatched ESPN name for a warning.)
        return None, (hn if home is None else an)

    card = {
        "stage": "Group stage",
        "stageCode": "GROUP_STAGE",
        "status": status,
        "utcDate": comp.get("date") or comp.get("startDate"),
        "home": home,
        "away": away,
        "penalties": False,
        "winner": None,
        TAG: None,  # set by caller
    }
    return card, pair_key(home["name"], away["name"])


def main():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except Exception as e:
        print(f"ERROR: could not read {DATA_FILE}: {e}", file=sys.stderr)
        return 1

    original = copy.deepcopy(data)

    try:
        espn = fetch_espn()
    except Exception as e:
        print(f"NOTE: ESPN unreachable ({e}); leaving {DATA_FILE} untouched.",
              file=sys.stderr)
        print("LIVE_COUNT=-1")  # unknown; the loop treats this as "no change in state"
        return 0  # never break the scores pipeline over the live overlay

    meta, by_norm = build_team_lookup(data)

    for key in ("live", "recent", "upcoming"):
        data.setdefault(key, [])

    # 1) Strip our own previous injections everywhere, so this run fully re-derives.
    for key in ("live", "recent", "upcoming"):
        data[key] = [m for m in data[key] if m.get(TAG) not in (TAG_LIVE, TAG_FINAL)]

    # 2) Walk ESPN group games: collect live cards, and final cards for games that
    #    football-data hasn't published anywhere yet (the disappearing-at-FT gap).
    live_cards, final_cards, unplaceable = [], [], set()
    for comp, state in espn_group_events(espn):
        if state == "in":
            card, key = make_card(comp, meta, by_norm, "IN_PLAY")
            if card:
                card[TAG] = TAG_LIVE
                live_cards.append(card)
            elif key:
                unplaceable.add(key)
        elif state == "post":
            card, key = make_card(comp, meta, by_norm, "FINISHED")
            if card:
                hg, ag = card["home"]["goals"], card["away"]["goals"]
                card["winner"] = ("DRAW" if hg == ag else
                                  "HOME_TEAM" if hg > ag else "AWAY_TEAM")
                card[TAG] = TAG_FINAL
                final_cards.append(card)

    live_keys = {pair_key(c["home"]["name"], c["away"]["name"]) for c in live_cards}

    # 3) A live game must not also sit in recent/upcoming (or a stale football-data
    #    live entry) — remove any matching pair so it shows once, as live.
    for key in ("live", "recent", "upcoming"):
        data[key] = [m for m in data[key]
                     if pair_key((m.get("home") or {}).get("name"),
                                 (m.get("away") or {}).get("name")) not in live_keys]

    # 4) Existing fixtures across the file (after live removal), so final-gap cards
    #    only fill in when football-data truly hasn't published the game yet.
    present = set()
    for key in ("live", "recent", "upcoming"):
        for m in data[key]:
            present.add(pair_key((m.get("home") or {}).get("name"),
                                 (m.get("away") or {}).get("name")))

    # 5) Inject. Live games sorted by kickoff; final-gap fillers prepended to
    #    recent (newest first) only when otherwise absent.
    live_cards.sort(key=lambda c: c.get("utcDate") or "")
    data["live"] = live_cards + data["live"]

    add_finals = [c for c in final_cards
                  if pair_key(c["home"]["name"], c["away"]["name"]) not in present
                  and pair_key(c["home"]["name"], c["away"]["name"]) not in live_keys]
    add_finals.sort(key=lambda c: c.get("utcDate") or "", reverse=True)
    data["recent"] = add_finals + data["recent"]

    if unplaceable:
        for nm in sorted(unplaceable):
            print(f"WARNING: ESPN team '{nm}' not matched to a canonical name; "
                  f"its live game was skipped.", file=sys.stderr)

    # 6) Write only if something actually changed (keeps the no-change check happy).
    print(f"LIVE_COUNT={len(live_cards)}")
    if data == original:
        print("No live changes; data.json left untouched.")
        return 0

    text = json.dumps(data, indent=2, ensure_ascii=False)
    if raw.endswith("\n"):
        text += "\n"
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Patched {DATA_FILE}: {len(live_cards)} live, "
          f"{len(add_finals)} final-gap game(s) injected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
