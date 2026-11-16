# dota-coach

Free coaching for players who can't pay for Dota Plus. Python stdlib only — no
pip install, no account, no API key, no ML model.

## The web page

`index.html` is the whole product for anyone who isn't you. Paste a match ID or
a Dotabuff/OpenDota link, pick your hero from the ten players, read the report.

It is a static file. OpenDota sends `Access-Control-Allow-Origin`, so the page
calls the API straight from the browser — **no backend, no hosting bill**. Drop
`index.html` and `data/` on GitHub Pages and it runs free forever.

```bash
python -m http.server 8765   # then open http://localhost:8765
```

The match id lives in the URL hash, so `yoursite/#9092560118` is a shareable
report link and a reload after a parse picks up where you left off.

## What the CLI does

**`review`** — give it a match id, get your numbers against every other game on
that hero: GPM, XPM, last hits, hero damage, your quietest five minutes, your
first big item timing. Nothing to install. This is the part to ship first.

```bash
python coach.py review 9092563405 --player 1
```

`--player` takes an account id, a slot number 0-9, or part of a hero name.

**`live`** — reads Dota's official Game State Integration feed and prints what
you can afford right now, plus what to save for.

```bash
python coach.py live
```

**`demo`** — self-check, no network needed.

```bash
python coach.py demo
```

## Setup

```bash
python bake.py
```

Pulls hero item aggregates from OpenDota into `data/dota.json` (~2–3 min, it
paces itself under the 60 req/min free limit). Re-run it daily from cron or a
GitHub Action and commit the JSON. **The app never calls OpenDota for item
data at runtime**, so the 2000 req/day free cap is never a user-facing limit.
Only `review` hits the network, once per match.

## Live mode setup

1. Copy `gamestate_integration_coach.cfg` into:

   ```
   <Steam>/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
   ```

   Create the `gamestate_integration` folder if it isn't there.

2. Dota 2 → Properties → Launch Options → add `-gamestateintegration`

3. `python coach.py live`, then start a match.

GSI is a Valve feature. It is not an injector and carries no VAC risk.

## What GSI will and won't give you

Gives: your hero, items, gold, GPM/XPM, abilities, level, KDA, game clock,
draft. Enough for build advice and buy timing.

Does not give: enemy items, enemy gold, anyone's position, map vision, cooldowns
on enemy heroes. Anything that reads those is reading game memory, and that is
a ban. Don't.

Spectator mode nests the payload per team and slot; `suggest()` detects it and
bails rather than guessing.

## Known limits

- Any finished match works. A **replay-parsed** match additionally gets the
  gold-curve window and the first-big-item timing, since those need `gold_t`
  and `purchase_log`. Benchmarks do *not* need a parse, only processing, which
  takes a few minutes after the game ends.
- `deaths_per_min` and `hero_healing_per_min` percentiles are ignored on
  purpose. Most players sit at 0, which ties up near the 95th percentile and
  reads as praise for doing nothing. Deaths are judged on raw count instead.
- Item advice is popularity, not win rate. Popular ≠ correct. Swapping to
  `winRate`-weighted ordering is a change in `bake.py:resolve` alone.
- Components name their goal (`Broadsword (1000g) -> Battle Fury`) via a
  `builds_into` map baked from `/constants/items`. An item with several
  ancestors in the hero's pool points at the most-bought one, which is a guess:
  a Yasha could be heading for Manta Style or Sange and Yasha, and the tool
  picks whichever is more popular on that hero. It does not read your build.
- `builds_into` is baked, so a `data/dota.json` from before that change makes
  every arrow disappear rather than crash. Re-run `bake.py` after pulling.
- The all-ranks percentiles have no role or bracket filter. Every Anti-Mage is
  compared to every other Anti-Mage, Herald and Immortal, safe lane or not.
  **This is why the "In this game" section exists and is shown first** — see
  below.
- OpenDota benchmarks pool every game mode. Turbo gives double gold and xp, so
  a Turbo game scores near the top on everything. `review` prints a warning on
  Turbo matches rather than pretending the numbers are comparable.

## Rank brackets (optional, needs a STRATZ token)

```bash
export STRATZ_TOKEN=...            # free, https://stratz.com/api, Steam sign-in
python bake_stratz.py --probe      # one call: proves the query, settles units
python bake_stratz.py              # the real bake, writes data/brackets.json
```

Set the token as an environment variable; never paste it into a file you commit
or into a chat. On PowerShell that is `$env:STRATZ_TOKEN = "..."`.

Signing in to stratz.com with Steam gives you a **Default Token** automatically
— no application, no approval, no referral requirement. Its limits are 20
calls/second, 250/minute, 2,000/hour and 10,000/day; a full bake is about 7
requests, so this never comes close. Tokens last roughly a year. STRATZ
requires the header `User-Agent: STRATZ_API`, which `bake_stratz.py` sends.

The page shows "powered by STRATZ" in the footer whenever bracket data is
loaded, which is the credit their Default Token terms ask for.

**Run `--probe` first.** It makes a single request for one hero and prints the
first row verbatim, so a wrong field name, a bad token or an empty result shows
up immediately rather than after 127 heroes. It also reads the returned
magnitudes to decide whether `xp`, `cs` and `heroDamage` are per-minute or
per-match totals, and prints the exact `VS_BRACKET` lines to paste — flagging
any whose unit disagrees with the OpenDota field it would be compared against.

This adds a **"Versus Legend–Ancient Pos 4 Crystal Maiden"** section comparing
you to the average for your hero, at your rank, in your position. Without the
token nothing breaks: the page hides the rank picker, the CLI skips the
section, everything else works exactly as before.

The token is read by `bake_stratz.py` and nowhere else. The output is a static
file, so **the key never reaches a browser** — do not put it in `index.html`.

A real bake covers 127 heroes in 2,283 cells (~409 KB). Sample depth per
bracket, which is the whole reason for using STRATZ over OpenDota:

| Bracket | Cells | Median n |
|---------|-------|----------|
| Herald–Guardian | 587 | 478 |
| Crusader–Archon | 612 | 1,265 |
| Legend–Ancient | 608 | 1,416 |
| Divine–Immortal | 476 | 744 |

Pudge at Herald–Guardian has 514 to 7,407 matches per position. OpenDota had 0
and 2.

Four things to know about this data:

- They are **averages, not percentiles**. The report says "+18% vs average",
  never "top 20%" — STRATZ exposes no distribution on this field.
- STRATZ pairs medals, so there are **four brackets, not eight**:
  Herald–Guardian, Crusader–Archon, Legend–Ancient, Divine–Immortal.
- **`goldPerMinute` is always null** on this type, despite existing in the
  schema. `networth` carries the economy comparison instead. `xp` is stored but
  not shown: it is a per-match total and OpenDota only publishes `xp_per_min`.
- Net worth, last hits and hero damage are **per-match totals**, and STRATZ
  publishes no average duration to divide by. A game outside 30–45 minutes
  skews them, so the report says so when that happens.

Position is guessed from net worth order within the team, which is the usual
proxy and is wrong often enough that the report says it is a guess.

`--probe` re-checks all of this against a live response, so run it again if
STRATZ changes the schema under you.

## Why OpenDota cannot do rank brackets

Comparing a Herald to every Immortal who ever played the hero is useless, so
the obvious fix is per-bracket benchmarks. OpenDota does not serve them, but
its `/explorer` SQL endpoint can join `player_matches` to `public_matches`,
which carries `avg_rank_tier`. That query runs in about 3 seconds. It is also
a dead end, and here is the measurement that killed it — Pudge, the most-played
hero in the game, joined rows per bracket:

| Herald | Guardian | Crusader | Archon | Legend | Ancient | Divine | Immortal |
|--------|----------|----------|--------|--------|---------|--------|----------|
| 0      | 2        | 11       | 26     | 119    | 235     | 494    | 628      |

`player_matches` holds only replay-parsed matches, and parse requests come
overwhelmingly from high-MMR players. Percentiles built on `n=2` would be
noise presented with false authority, and worst exactly for the players this
tool is meant to help.

**So the tool ranks you against the nine other players in your own game
instead.** Same bracket, same patch, same game length, same mode, by
construction — no sample-size problem, no extra API, and it works on matches
that were never replay-parsed. It also sidesteps Turbo entirely, since Turbo
inflates all ten players equally.

Real per-bracket numbers need a source that ingests every match rather than
only parsed ones. That is what `bake_stratz.py` above is for.

## Where this beats Dota Plus

Dota Plus tells you what to buy. It never tells you why you lost. The `review`
output is the gap: "your GPM was bottom 2% for this hero, and minutes 10–15
earned you 50 gold." That is pure arithmetic on free data.
