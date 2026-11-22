# gh-bot-scan

Find autonomous contributor accounts on GitHub, then measure what attracts them.

Built because a single account opened 192 pull requests in 72 hours across unrelated repositories, and profiling that one account answered the wrong question. This scores a *population*.

## Two stages, because one does not work

**Stage 1 — score accounts.** Features are facts from the API, none needs taste:

| Feature | Weight | Why |
|---|---|---|
| `volume` — PRs in the window | 0.28 | the blunt signal |
| `fork_ratio` — forks ÷ public repos | 0.22 | bots fork to push a branch |
| `spread` — distinct repos in recent events | 0.18 | one PR in many places |
| `anonymity` — no bio, name, company | 0.12 | profile exists only to satisfy a form |
| `no_following` — following == 0 | 0.10 | never follows anyone back |
| `many_repos`, `few_followers` | 0.10 | scale, asymmetry |

**Stage 2 — spread across upstream repos.** This is the stage that matters, and the reason the tool exists: stage 1 cannot tell a census bot from a busy *maintainer*, because a maintainer also works through forks and can hit 130 PRs a week.

The separator is whose repositories the PRs land in. A maintainer concentrates; a census bot spreads, roughly one PR per repo.

Measured, last 7 days:

```
login                   PRs distinct spread
SAY-5                    80       77   0.96      ← census bot
bfs2021                 355       96   0.96      ← census bot
cryptodj413              12        3   0.25
chadaibbhot-git           4        2   0.50
wolf31o2                130       17   0.17      ← maintainer, not a bot
wcatz                    54        7   0.13
chrisguiney              23        2   0.09
```

Threshold that held: **spread ≥ 0.9 and ≥ 20 PRs/week**. `wolf31o2` is the counter-example that proves the second stage is necessary — 130 PRs a week, and a human.

## Usage

```bash
# find candidates from a set of repos, score them
python3 main.py --seed blinklabs-io/dingo masslight/ottehr --days 7 --limit 40

# test whether named accounts spread across other people's repos
python3 main.py --spread SAY-5 bfs2021 wolf31o2 --days 7
```

Authenticated with `gh auth token`; every response is cached under `data/cache/`, so a re-run costs no API calls. Rate limiting is handled by sleeping until `X-RateLimit-Reset`.

## What this measures, and what it does not

It measures **behaviour**: volume, spread, anonymity. Not intent, not the model behind an account, and not whether the work is good. Several accounts that score high produce genuinely useful patches; the point of finding them is to know where to spend review time and which labels they scan for.

A high score is a question, not a verdict. The next stage — what attracts the population, not one account — is the one that changes what you write in a ticket.

## Status

Stages 1 and 2 work. The pooled attraction model (all detected accounts → their PRs → the issues they fixed → features → pickup rates) is the next piece.
