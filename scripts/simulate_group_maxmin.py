#!/usr/bin/env python3
"""
simulate_group_maxmin.py — the exact third-place qualification cutoff.

In The Family Cup, 32 teams reach the Round of 32: the 12 group winners, the 12
runners-up, and the 8 BEST third-placed teams. Those 8 are picked by ranking all
twelve third-placed sides on (in order):

    Pts  ->  GD  ->  GF  ->  Conduct  ->  FIFA rank  ->  team name

The team sitting 8th in that ranking is the marginal qualifier: it is the LAST
side in, and its stat line IS the cutoff. To go through as a best third you have
to be at least as good as that 8th team. The team 9th in the ranking is the first
side OUT — the other side of the exact tiebreak boundary.

This script reuses your simulate_pool.py wholesale — same Elos, same weights,
same scoring engine (make_engine), same group ranking (rank_group), same
remaining-fixture simulation — but runs ONLY the group half of the tournament
(the knockouts can't change who the best 8 thirds are) tens of thousands of times
and records, every run, the full line of the 8th-placed third (the cutoff) and
the 9th (the first team out). Over all those runs it reports:

  * the HIGHEST cutoff ever seen — the strongest line a side needed to sneak in
    as the 8th best third (the hardest the bar ever got), and which team it was;
  * the LOWEST cutoff ever seen — the weakest line that was still good enough for
    8th (the easiest the bar ever got), and which team it was;
  * the typical (median) cutoff, the full distribution of cutoff lines, and which
    teams most often end up on the qualification bubble.

Strength is compared on exactly the qualification order the page uses
(Pts, GD, GF, Conduct, FIFA), so "highest/lowest" mean strongest/weakest by the
real tiebreak rules — across the board.

    python3 simulate_group_maxmin.py                  # 1,000,000 runs, all cores
    python3 simulate_group_maxmin.py --sims 50000
    python3 simulate_group_maxmin.py --seed 1 --workers 1
    python3 simulate_group_maxmin.py --json cutoff.json

Speed/seeding/parallelism mirror simulate_pool.py: the work is split across CPU
cores, each worker runs an independent seeded slice of the same model, and the
per-line tallies are summed — statistically identical to one process, faster.
Because extremes live in the tails, more sims surface harder highs and softer
lows; the default of 1,000,000 gives those tails room to show up.
"""

import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool

# Reuse the real model + engine + ranking straight from your simulator, so this
# tool can never silently drift from what the "Simulate" button actually does.
# simulate_pool.py may sit next to this file, in a scripts/ subfolder, or in the
# parent of this file (e.g. this script in scripts/, simulate_pool in the root).
# Put all of those on the path so the import works no matter where you run from.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.join(_here, "scripts"), os.path.dirname(_here), os.getcwd()):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from simulate_pool import load, build_model, make_engine, rank_group


CUTOFF_INDEX = 7    # 8th-best third — the last team IN (this is the cutoff)
FIRSTOUT_INDEX = 8  # 9th-best third — the first team OUT


# ----------------------------------------------------------------------------
# The GROUP-STAGE half of simulate_pool.make_runner().run(), nothing more.
# Plays the remaining group games with the same engine, ranks each group the same
# way, and sorts the twelve thirds by the same key. Returns the twelve thirds
# best-to-worst, so the caller can read off index 7 (cutoff) and 8 (first out).
# Each third dict carries the full line: p, points, gd, gf, ga, conduct, fifa.
# ----------------------------------------------------------------------------
def make_group_runner(model):
    sim = model["sim"]
    fifa_of = model["fifa_of"]
    conduct_of = model["conduct_of"]
    groups_def = sim["groupsDef"]
    group_results = sim.get("groupResults") or {}
    group_remaining = sim.get("groupRemaining") or {}

    _, elo_score, _ = make_engine(model)

    def run_groups():
        thirds = []
        for letter in groups_def:
            names = groups_def[letter]
            rec = {t: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0}
                   for t in names}
            all_res = []

            def apply(hn, an, hg, ag):
                all_res.append({"h": hn, "a": an, "hg": hg, "ag": ag})
                for (t, gf, ga) in ((hn, hg, ag), (an, ag, hg)):
                    r = rec[t]
                    r["p"] += 1; r["gf"] += gf; r["ga"] += ga
                    if gf > ga:
                        r["w"] += 1; r["pts"] += 3
                    elif gf == ga:
                        r["d"] += 1; r["pts"] += 1
                    else:
                        r["l"] += 1

            for m in (group_results.get(letter) or []):
                apply(m["h"], m["a"], m["hg"], m["ag"])
            for m in (group_remaining.get(letter) or []):
                hg, ag = elo_score(m["h"], m["a"], group=True)
                apply(m["h"], m["a"], hg, ag)

            rows = [{"team": t, "points": rec[t]["pts"],
                     "gd": rec[t]["gf"] - rec[t]["ga"], "gf": rec[t]["gf"],
                     "ga": rec[t]["ga"], "p": rec[t]["p"],
                     "conduct": conduct_of.get(t, 0), "fifa": fifa_of.get(t)}
                    for t in names]
            ranked = rank_group(rows, all_res)
            if len(ranked) >= 3:
                third = dict(ranked[2]); third["group"] = letter
                thirds.append(third)

        # Best-thirds order — the five official criteria, ending at FIFA rank:
        #   Points -> GD -> GF -> Conduct -> FIFA.
        # (simulate_pool.run() appends t["team"] as a final deterministic
        # fallback; that is not a tournament rule and, because every team has a
        # unique FIFA ranking, it can never actually decide anything — so it is
        # dropped here and FIFA is the last word. None FIFA maps to 999.)
        thirds.sort(key=lambda t: (-t["points"], -t["gd"], -t["gf"],
                                   -(t["conduct"] or 0),
                                   (999 if t["fifa"] is None else t["fifa"])))
        return thirds

    return run_groups


# ----------------------------------------------------------------------------
# A line is the tuple the cutoff is reported on, in header order:
#     (P, Pts, GD, GF, Con, FIFA)
# strength_key sorts lines by the real qualification order (smaller = stronger);
# P is excluded because it is constant once every group is complete.
# ----------------------------------------------------------------------------
def line_tuple(t):
    return (t["p"], t["points"], t["gd"], t["gf"], (t["conduct"] or 0),
            (None if t["fifa"] is None else t["fifa"]))


def strength_key(line):
    _, pts, gd, gf, con, fifa = line
    return (-pts, -gd, -gf, -con, (999 if fifa is None else fifa))


# ----------------------------------------------------------------------------
# Worker: own process, own seeded RNG, same rebuilt model. Tallies, per line,
# how many runs ended with that line at the cutoff (8th) / first-out (9th) slot,
# plus which team held the slot. Returns plain dicts so they pickle and sum.
# ----------------------------------------------------------------------------
def _run_chunk(task):
    n, seed, data_path, elo_path = task
    random.seed(seed)
    model = build_model(load(elo_path), load(data_path))
    run_groups = make_group_runner(model)

    cut = defaultdict(lambda: [0, defaultdict(int)])  # line -> [count, {team:count}]
    out = defaultdict(lambda: [0, defaultdict(int)])
    for _ in range(n):
        thirds = run_groups()
        if len(thirds) > CUTOFF_INDEX:
            t = thirds[CUTOFF_INDEX]
            e = cut[line_tuple(t)]; e[0] += 1; e[1][t["team"]] += 1
        if len(thirds) > FIRSTOUT_INDEX:
            t = thirds[FIRSTOUT_INDEX]
            e = out[line_tuple(t)]; e[0] += 1; e[1][t["team"]] += 1

    return ({k: [v[0], dict(v[1])] for k, v in cut.items()},
            {k: [v[0], dict(v[1])] for k, v in out.items()})


def merge(dst, src):
    for line, (c, teams) in src.items():
        e = dst[line]
        e[0] += c
        for tm, n in teams.items():
            e[1][tm] += n


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
HEADER = " P   Pts    GD   GF   Con   FIFA"


def fmt_line(line):
    p, pts, gd, gf, con, fifa = line
    fifa_s = "—" if fifa is None else str(fifa)
    return f"{p:>2}   {pts:>3}   {gd:>+3}   {gf:>2}   {con:>3}   {fifa_s:>4}"


def teams_str(teams, limit=4):
    """ '(team:count, …)' ordered by frequency, capped for readability. """
    items = sorted(teams.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    shown = ", ".join(f"{tm} ({n:,})" for tm, n in items[:limit])
    if len(items) > limit:
        shown += f", +{len(items) - limit} more"
    return shown


# ----------------------------------------------------------------------------
# SVG graphic — the artifact the site serves at its hidden /#bubble view.
# Self-contained, no external assets, NO embedded timestamp (so identical model
# output yields identical bytes and the pipeline's `git diff --staged --quiet`
# no-change check still fires). News-desk styling: serif masthead headline,
# monospaced "scoreboard" numerals, a three-row cut-off table with heat tabs,
# and a leader-scaled bubble bar chart. No em dashes anywhere in the copy.
# ----------------------------------------------------------------------------
def _xesc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _sgn(v):
    # Unicode minus (U+2212) for negative numeric values; "+" for positive GD.
    return f"+{v}" if v > 0 else (f"\u2212{-v}" if v < 0 else "0")


def _pct(x):  # x is a percentage value (0..100)
    if x >= 1:    return f"{x:.1f}%"
    if x >= 0.1:  return f"{x:.2f}%"
    if x >= 0.01: return f"{x:.3f}%"
    return f"{x:.4f}%"


def render_bubble_svg(N, total_cut, hardest, easiest, median, cut, bubble_top):
    sims = f"{N:,}"

    def line_cells(line):
        p, pts, gd, gf, con, fifa = line
        return [str(p), str(pts), _sgn(gd), str(gf), _sgn(con),
                ("NR" if fifa is None else str(fifa))]

    def modal_team(line):
        teams = cut.get(line, [0, {}])[1]
        if not teams:
            return None
        return max(teams.items(), key=lambda kv: (kv[1], ))[0]

    def line_pct(line):
        return _pct(100.0 * cut.get(line, [0])[0] / total_cut) if total_cut else "0%"

    cx = [456, 508, 560, 612, 664, 716]      # numeric column centres
    rows = [
        ("LOWEST",  "easiest bar", "#2E6E66", easiest, modal_team(easiest) or "Unknown",
         ["The easiest bar that still got", "a side in: " + line_pct(easiest) + " of runs."]),
        ("TYPICAL", "median run",  "#B07D27", median, None,
         ["The midpoint cut-off across", "all simulated tournaments."]),
        ("HIGHEST", "hardest bar", "#A23139", hardest, modal_team(hardest) or "Unknown",
         ["The hardest bar a side faced", "and still made 8th: " + line_pct(hardest) + "."]),
    ]

    out = []
    a = out.append
    a('<svg viewBox="0 0 1080 1520" xmlns="http://www.w3.org/2000/svg" '
      'font-family="Helvetica Neue, Arial, sans-serif">')
    a('<defs><style>'
      '.serif{font-family:Georgia,"Times New Roman",serif;}'
      '.sans{font-family:"Helvetica Neue",Arial,sans-serif;}'
      '.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}'
      '</style></defs>')
    a('<rect x="0" y="0" width="1080" height="1520" fill="#FCFBF8"/>')

    # masthead
    a('<rect x="0" y="0" width="1080" height="64" fill="#15171C"/>')
    a('<text class="sans" x="80" y="40" fill="#FCFBF8" font-size="14" font-weight="700" '
      'letter-spacing="2.5">WORLD CUP 2026&#160;&#160;&#183;&#160;&#160;GROUP STAGE</text>')
    a('<text class="sans" x="1000" y="40" fill="#FCFBF8" font-size="14" font-weight="700" '
      'letter-spacing="2.5" text-anchor="end">MONTE-CARLO PROJECTION</text>')

    # headline + deck
    a('<text class="serif" x="80" y="165" fill="#15171C" font-size="58" font-weight="700" '
      'letter-spacing="-0.5">The race for the last</text>')
    a('<text class="serif" x="80" y="227" fill="#15171C" font-size="58" font-weight="700" '
      'letter-spacing="-0.5">third-place ticket</text>')
    a(f'<text class="sans" x="80" y="286" fill="#4A4E57" font-size="20">Eight of the twelve '
      f'third-placed teams advance to the Round of 32. Across {sims}</text>')
    a('<text class="sans" x="80" y="316" fill="#4A4E57" font-size="20">simulations of the group '
      'games still to be played, this is how good a side has had to</text>')
    a('<text class="sans" x="80" y="346" fill="#4A4E57" font-size="20">be to claim that eighth '
      'and final place: the cut-off seat.</text>')
    a('<line x1="80" y1="384" x2="1000" y2="384" stroke="#E2DFD7" stroke-width="1"/>')

    # table heading
    a('<text class="sans" x="80" y="432" fill="#15171C" font-size="15" font-weight="700" '
      'letter-spacing="2.5">THE CUT-OFF LINE</text>')
    a('<rect x="80" y="442" width="46" height="3" fill="#15171C"/>')
    a('<text class="sans" x="80" y="467" fill="#4A4E57" font-size="15">The 8th-best third-placed '
      'team in each scenario: the last side to qualify.</text>')

    # column headers
    a('<text class="sans" x="240" y="506" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5">TEAM</text>')
    for c, lab in zip(cx, ["P", "PTS", "GD", "GF", "CON", "FIFA"]):
        a(f'<text class="sans" x="{c}" y="506" fill="#7C808A" font-size="12" font-weight="700" '
          f'letter-spacing="1.5" text-anchor="middle">{lab}</text>')
    a('<text class="sans" x="762" y="506" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5">WHAT IT MEANS</text>')
    a('<line x1="80" y1="518" x2="1000" y2="518" stroke="#C9C5BA" stroke-width="1.5"/>')

    # three rows
    tops = [524, 620, 716]
    for (tier, sub, color, line, team, notes), top in zip(rows, tops):
        center = top + 48
        a(f'<rect x="80" y="{top + 14}" width="6" height="68" fill="{color}"/>')
        a(f'<text class="sans" x="100" y="{center - 6}" fill="{color}" font-size="15" '
          f'font-weight="700">{tier}</text>')
        a(f'<text class="sans" x="100" y="{center + 14}" fill="#7C808A" font-size="12.5">{sub}</text>')
        if team is None:
            a(f'<text class="serif" x="240" y="{center + 8}" fill="#7C808A" font-size="19" '
              f'font-style="italic">Median outcome</text>')
        else:
            a(f'<text class="serif" x="240" y="{center + 6}" fill="#15171C" font-size="22">'
              f'{_xesc(team)}</text>')
        cells = line_cells(line)
        for c, val, i in zip(cx, cells, range(6)):
            bold = ' font-weight="700"' if i in (1, 2) else ''  # emphasise Pts, GD
            a(f'<text class="mono" x="{c}" y="{center + 7}" fill="#15171C" font-size="20"'
              f'{bold} text-anchor="middle">{val}</text>')
        a(f'<text class="sans" x="762" y="{center - 6}" fill="#4A4E57" font-size="13.5">'
          f'{_xesc(notes[0])}</text>')
        a(f'<text class="sans" x="762" y="{center + 13}" fill="#4A4E57" font-size="13.5">'
          f'{_xesc(notes[1])}</text>')
    a('<line x1="80" y1="634" x2="1000" y2="634" stroke="#EFEDE7" stroke-width="1"/>')
    a('<line x1="80" y1="730" x2="1000" y2="730" stroke="#EFEDE7" stroke-width="1"/>')
    a('<line x1="80" y1="812" x2="1000" y2="812" stroke="#C9C5BA" stroke-width="1.5"/>')

    # legend
    a('<text class="sans" x="80" y="836" fill="#7C808A" font-size="12.5">'
      '<tspan fill="#15171C" font-weight="700">P</tspan> played&#160;&#160;&#160;'
      '<tspan fill="#15171C" font-weight="700">Pts</tspan> points&#160;&#160;&#160;'
      '<tspan fill="#15171C" font-weight="700">GD</tspan> goal difference&#160;&#160;&#160;'
      '<tspan fill="#15171C" font-weight="700">GF</tspan> goals for&#160;&#160;&#160;'
      '<tspan fill="#15171C" font-weight="700">Con</tspan> conduct score&#160;&#160;&#160;'
      '<tspan fill="#15171C" font-weight="700">FIFA</tspan> world ranking</text>')
    a('<text class="sans" x="80" y="856" fill="#7C808A" font-size="12.5">Thirds are ranked in turn '
      'by points, goal difference, goals scored, conduct score, then FIFA ranking.</text>')

    # bubble bar chart
    a('<text class="sans" x="80" y="906" fill="#15171C" font-size="15" font-weight="700" '
      'letter-spacing="2.5">MOST OFTEN ON THE BUBBLE</text>')
    a('<rect x="80" y="916" width="46" height="3" fill="#15171C"/>')
    a(f'<text class="sans" x="80" y="941" fill="#4A4E57" font-size="15">Share of {sims} simulations '
      f'in which a team finishes 8th, the last side to qualify.</text>')

    top8 = bubble_top[:8]
    lead = (top8[0][1] / total_cut) if (top8 and total_cut) else 1.0
    lead = lead or 1.0
    LEADW = 620.0
    for i, (tm, k) in enumerate(top8):
        share = (k / total_cut) if total_cut else 0.0
        w = max(2.0, share / lead * LEADW)
        bar_y = 1006 + i * 50
        ty = bar_y + 18
        op = '' if i == 0 else ' opacity="0.5"'
        bold = ' font-weight="700"' if i == 0 else ''
        a(f'<text class="sans" x="250" y="{ty}" fill="#15171C" font-size="15" '
          f'text-anchor="end">{_xesc(tm)}</text>')
        a(f'<rect x="266" y="{bar_y}" width="{w:.1f}" height="26" fill="#2B3A55"{op}/>')
        a(f'<text class="mono" x="{266 + w + 8:.1f}" y="{ty}" fill="#15171C" font-size="15"'
          f'{bold}>{share * 100:.1f}%</text>')

    rem = max(0.0, 100.0 - sum((k / total_cut * 100.0) for _t, k in top8)) if total_cut else 0.0
    a(f'<text class="sans" x="80" y="1410" fill="#7C808A" font-size="12.5">About {rem:.0f}% of '
      f'eighth-place finishes are spread across other third-placed teams.</text>')

    # footer
    a('<line x1="80" y1="1436" x2="1000" y2="1436" stroke="#E2DFD7" stroke-width="1"/>')
    a('<rect x="80" y="1456" width="11" height="11" fill="#A23139"/>')
    a('<text class="sans" x="100" y="1466" fill="#15171C" font-size="15" font-weight="700">'
      'Provisional: these projections will change once the next round of group fixtures is '
      'played.</text>')
    a(f'<text class="sans" x="80" y="1492" fill="#7C808A" font-size="12.5">Method&#160;&#160;&#183;'
      f'&#160;&#160;{sims} Monte-Carlo simulations of the remaining group stage, conditioned on '
      f'results so far. The cut-off is the 8th-best third-placed team.</text>')
    a('<text class="sans" x="80" y="1511" fill="#7C808A" font-size="12.5">Source&#160;&#160;&#183;'
      '&#160;&#160;group-stage simulation model</text>')
    a('</svg>')
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Highest/lowest third-place qualification cutoff (8th best third).")
    ap.add_argument("--sims", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None,
                    help="parallel worker processes (default: all CPU cores). "
                         "Use 1 for single-process behaviour.")
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--elo", default="elo.json")
    ap.add_argument("--top", type=int, default=8,
                    help="how many 'bubble' teams to list (default 8).")
    ap.add_argument("--json", default=None,
                    help="also write a JSON file with the full cutoff distribution, "
                         "the highest/lowest extremes, the median cutoff, and the "
                         "bubble-team tallies.")
    ap.add_argument("--svg", default=None,
                    help="also write a self-contained, news-style SVG graphic of the "
                         "highest/lowest/typical cut-off and the bubble teams (the "
                         "artifact the site serves at its hidden /#bubble view). No "
                         "timestamp is embedded, so identical inputs produce identical "
                         "bytes and the pipeline's no-change check still fires.")
    args = ap.parse_args()

    data = load(args.data)
    elo = load(args.elo)
    model = build_model(elo, data)  # parent copy: warning + groups sanity only

    if model["R"] is None:
        print("WARNING: elo.json has no ratings; scorelines fall back to uniform "
              "random (the same graceful fallback the page uses).", file=sys.stderr)

    n_thirds = len(model["sim"]["groupsDef"])
    if n_thirds <= CUTOFF_INDEX:
        print(f"Only {n_thirds} group(s) — there is no 8th-best third to form a "
              f"cutoff. Need at least {CUTOFF_INDEX + 1} groups.", file=sys.stderr)
        sys.exit(1)

    N = args.sims
    workers = args.workers if args.workers else (os.cpu_count() or 1)
    workers = max(1, min(workers, N))
    base, rem = divmod(N, workers)
    counts = [base + (1 if i < rem else 0) for i in range(workers)]

    seeder = random.Random(args.seed)
    tasks = [(counts[i], seeder.randrange(2 ** 31 - 1), args.data, args.elo)
             for i in range(workers)]

    print(f"Simulating {N:,} group stages across {workers} worker(s) …",
          file=sys.stderr)

    cut = defaultdict(lambda: [0, defaultdict(int)])
    out = defaultdict(lambda: [0, defaultdict(int)])
    if workers == 1:
        c, o = _run_chunk(tasks[0])
        merge(cut, c); merge(out, o)
    else:
        with Pool(processes=workers) as pool:
            done = 0
            for c, o in pool.imap_unordered(_run_chunk, tasks):
                merge(cut, c); merge(out, o)
                done += 1
                print(f"  …worker {done}/{workers} finished", file=sys.stderr)

    total_cut = sum(v[0] for v in cut.values())
    if not total_cut:
        print("No simulated run produced a full set of qualifying thirds.",
              file=sys.stderr)
        sys.exit(1)

    # Order every observed cutoff line strongest -> weakest, with cumulative share.
    lines = sorted(cut.keys(), key=strength_key)
    cum = 0
    dist = []
    for ln in lines:
        c = cut[ln][0]
        cum += c
        dist.append((ln, c, cut[ln][1], c / total_cut, cum / total_cut))

    hardest = lines[0]                      # strongest line ever at 8th = highest bar
    easiest = lines[-1]                     # weakest line ever at 8th  = lowest bar
    # Median cutoff: first line (strongest->weakest) whose cumulative share >= 50%.
    median = next(ln for (ln, _c, _t, _p, cu) in dist if cu >= 0.5)

    # Teams most often ON the bubble (held the 8th, last-in slot).
    bubble = defaultdict(int)
    for ln, (_c, teams) in cut.items():
        for tm, k in teams.items():
            bubble[tm] += k
    bubble_top = sorted(bubble.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    # ------------------------------------------------------------------ report
    print(f"\nThird-place qualification cutoff over {N:,} simulated group stages")
    print(f"(model + weights from {args.elo}, state from {args.data})")
    print("The cutoff is the 8th-best third — the last team to qualify. To go "
          "through\nas a best third, a side must be at least this good.\n")

    print("HIGHEST cutoff ever seen  (hardest bar — you needed this just to be 8th)")
    print(f"   {HEADER}")
    print(f"   {fmt_line(hardest)}")
    print(f"   occurred in {cut[hardest][0]:,} run(s) "
          f"({100 * cut[hardest][0] / total_cut:.4f}%)")
    print(f"   team(s) that scraped in on it: {teams_str(cut[hardest][1])}\n")

    print("LOWEST cutoff ever seen   (softest bar — this little was enough for 8th)")
    print(f"   {HEADER}")
    print(f"   {fmt_line(easiest)}")
    print(f"   occurred in {cut[easiest][0]:,} run(s) "
          f"({100 * cut[easiest][0] / total_cut:.4f}%)")
    print(f"   team(s) that scraped in on it: {teams_str(cut[easiest][1])}\n")

    print("TYPICAL cutoff  (median run)")
    print(f"   {HEADER}")
    print(f"   {fmt_line(median)}\n")

    print("Full distribution of the cutoff line (strongest cutoff at the top):")
    print(f"   {HEADER}      runs        %     cum%")
    for ln, c, _teams, share, cu in dist:
        print(f"   {fmt_line(ln)}   {c:>9,}   {100 * share:6.2f}   {100 * cu:6.2f}")
    print()

    print(f"Teams most often on the qualification bubble (held the 8th, last-in slot):")
    for tm, k in bubble_top[:args.top]:
        print(f"   {tm:<22} {k:>9,}  ({100 * k / total_cut:5.2f}% of runs)")
    print()

    # First team OUT (9th) — the other side of the exact tiebreak boundary.
    total_out = sum(v[0] for v in out.values())
    if total_out:
        out_lines = sorted(out.keys(), key=strength_key)
        strongest_out = out_lines[0]
        print("Strongest side to JUST MISS  (best 9th-placed third — unluckiest out)")
        print(f"   {HEADER}")
        print(f"   {fmt_line(strongest_out)}")
        print(f"   occurred in {out[strongest_out][0]:,} run(s) "
              f"({100 * out[strongest_out][0] / total_out:.4f}%)")
        print(f"   team(s): {teams_str(out[strongest_out][1])}\n")

    # -------------------------------------------------------------------- svg
    if args.svg:
        svg = render_bubble_svg(N, total_cut, hardest, easiest, median, cut, bubble_top)
        old = None
        try:
            with open(args.svg, encoding="utf-8") as f:
                old = f.read()
        except Exception:
            old = None
        if old == svg:
            print(f"No bubble-graphic changes since last run; {args.svg} left untouched.")
        else:
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(svg)
            print(f"Wrote {args.svg} ({N:,} sims).")

    # ------------------------------------------------------------------- json
    if args.json:
        def line_obj(ln):
            p, pts, gd, gf, con, fifa = ln
            return {"P": p, "Pts": pts, "GD": gd, "GF": gf, "Con": con, "FIFA": fifa}

        def team_list(teams):
            return [{"name": tm, "count": n}
                    for tm, n in sorted(teams.items(),
                                        key=lambda kv: (-kv[1], kv[0].lower()))]

        payload = {
            "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sims": N,
            "runs_with_cutoff": total_cut,
            "highest": {"line": line_obj(hardest), "runs": cut[hardest][0],
                        "share": cut[hardest][0] / total_cut,
                        "teams": team_list(cut[hardest][1])},
            "lowest": {"line": line_obj(easiest), "runs": cut[easiest][0],
                       "share": cut[easiest][0] / total_cut,
                       "teams": team_list(cut[easiest][1])},
            "median": {"line": line_obj(median)},
            "distribution": [
                {"line": line_obj(ln), "runs": c, "share": share, "cum": cu,
                 "teams": team_list(cut[ln][1])}
                for (ln, c, _t, share, cu) in dist],
            "bubble_teams": [{"name": tm, "runs": k, "share": k / total_cut}
                             for tm, k in bubble_top],
        }
        if total_out:
            payload["strongest_missed"] = {
                "line": line_obj(strongest_out), "runs": out[strongest_out][0],
                "share": out[strongest_out][0] / total_out,
                "teams": team_list(out[strongest_out][1])}

        import json as _json
        with open(args.json, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"Wrote {args.json} ({N:,} sims).")


if __name__ == "__main__":
    main()
