#!/usr/bin/env python3
"""gh-bot-scan — find autonomous contributor accounts, then what attracts them.

Phase 1 (scan): seed from repos, expand through contributors, score each account
on features that do not require judgement, emit candidates ranked by score.

Rate limits: authenticated REST is 5000/hour. Every response is cached in
data/cache/, so a re-run costs nothing. --pace sleeps between calls.
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

CACHE = os.path.join(os.path.dirname(__file__), "data", "cache")
os.makedirs(CACHE, exist_ok=True)
TOKEN = None


def token():
    global TOKEN
    if TOKEN is None:
        TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    return TOKEN


def api(path, pace=0.0):
    key = re.sub(r"[^A-Za-z0-9]+", "_", path)[:180]
    fn = os.path.join(CACHE, key + ".json")
    if os.path.exists(fn):
        try:
            return json.load(open(fn))
        except Exception:
            pass
    url = path if path.startswith("http") else "https://api.github.com" + path
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token(),
        "Accept": "application/vnd.github+json",
        "User-Agent": "gh-bot-scan",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
                break
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = int(e.headers.get("X-RateLimit-Reset", "0"))
                wait = max(5, min(60, wait - int(time.time())))
                sys.stderr.write("rate limited, sleeping %ds\n" % wait)
                time.sleep(wait)
                continue
            if e.code == 404:
                data = None
                break
            raise
        finally:
            if pace:
                time.sleep(pace)
    else:
        raise SystemExit("giving up on " + path)
    json.dump(data, open(fn, "w"))
    return data


def days_ago(n):
    return (datetime.now(timezone.utc).timestamp() - n * 86400)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def contributors(repo):
    out = []
    for page in (1, 2):
        d = api("/repos/%s/contributors?per_page=100&page=%d" % (repo, page)) or []
        out += [c["login"] for c in d]
        if len(d) < 100:
            break
    return out


def profile(login):
    return api("/users/" + login) or {}


def repos(login):
    return api("/users/%s/repos?per_page=100&sort=updated" % login) or []


def pr_count(login, days):
    since = iso(days_ago(days))
    d = api("/search/issues?q=author:%s+type:pr+created:%%3E=%s&per_page=1" % (login, since))
    return (d or {}).get("total_count", 0)


def events(login):
    return api("/users/%s/events/public?per_page=100" % login) or []


def features(login, days):
    p = profile(login)
    if not p:
        return None
    rs = repos(login)
    forks = sum(1 for r in rs if r.get("fork"))
    langs = {r.get("language") for r in rs if r.get("language")}
    stars = sum(r.get("stargazers_count", 0) for r in rs)
    ev = events(login)
    ev_repos = {e["repo"]["name"] for e in ev if e.get("repo")}
    created = p.get("created_at", "2026-01-01T00:00:00Z")
    age_days = (datetime.now(timezone.utc) - datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
    return dict(
        login=login,
        public_repos=p.get("public_repos", 0),
        forks=forks,
        fork_ratio=round(forks / max(1, len(rs)), 3) if rs else 0.0,
        followers=p.get("followers", 0),
        following=p.get("following", 0),
        stars_own=stars,
        languages=len(langs),
        prs_window=pr_count(login, days),
        event_repos=len(ev_repos),
        event_types=len({e.get("type") for e in ev}),
        has_bio=bool(p.get("bio")),
        has_name=bool(p.get("name")),
        has_company=bool(p.get("company")),
        account_days=age_days,
        type=p.get("type"),
    )


def score(f):
    """Each component is 0..1; weights sum to 1. No component needs taste."""
    c = {}
    c["fork_ratio"] = min(1.0, max(0.0, (f["fork_ratio"] - 0.5) / 0.5))
    c["no_following"] = 1.0 if f["following"] == 0 else 0.0
    c["volume"] = min(1.0, f["prs_window"] / 30.0) if f["prs_window"] < 1000 else 1.0
    c["spread"] = min(1.0, f["event_repos"] / 10.0)
    c["anonymity"] = (0 if f["has_bio"] else 0.4) + (0 if f["has_name"] else 0.3) + (0 if f["has_company"] else 0.3)
    c["few_followers"] = 1.0 if f["followers"] < 20 else 0.0
    c["many_repos"] = min(1.0, max(0.0, (f["public_repos"] - 50) / 450.0))
    w = dict(fork_ratio=0.22, no_following=0.10, volume=0.28, spread=0.18, anonymity=0.12, few_followers=0.04, many_repos=0.06)
    return round(sum(c[k] * w[k] for k in w), 3), {k: round(v, 2) for k, v in c.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", nargs="*", default=[], help="owner/repo seeds")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=60, help="accounts to profile")
    ap.add_argument("--pace", type=float, default=0.0)
    ap.add_argument("--out", default="data/users.csv")
    ap.add_argument("--spread", nargs="*", help="logins to test for cross-repo spread")
    a = ap.parse_args()

    if a.spread:
        print("%-20s %6s %8s %6s %6s %9s" % ("login", "PRs", "distinct", "spread", "1/repo", "rate"))
        for login in a.spread:
            s = spread(login, a.days)
            print("%-20s %6d %8d %6.2f %6.2f %9s" % (
                s["login"], s["prs_total"], s["distinct_upstream"], s["spread_ratio"],
                s["one_pr_per_repo"], s["next_pr_speed"]))
        return

    seen, queue = set(), []
    for s in a.seed:
        for login in contributors(s):
            if login not in seen or login.endswith("[bot]"):
                if login not in seen:
                    seen.add(login)
                    queue.append(login)
    sys.stderr.write("candidates from seeds: %d\n" % len(queue))

    rows = []
    for i, login in enumerate(queue[: a.limit], 1):
        try:
            f = features(login, a.days)
        except SystemExit as e:
            sys.stderr.write("skip %s: %s\n" % (login, e))
            continue
        if not f:
            continue
        f["score"], f["components"] = score(f)
        rows.append(f)
        sys.stderr.write("[%d/%d] %-24s score=%.3f prs=%d repos=%d follow=%d\n" % (
            i, min(len(queue), a.limit), login, f["score"], f["prs_window"], f["public_repos"], f["following"]))

    rows.sort(key=lambda r: -r["score"])
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    cols = ["login", "score", "prs_window", "fork_ratio", "public_repos", "followers", "following",
            "event_repos", "account_days", "has_bio", "has_name", "stars_own"]
    with open(a.out, "w") as fh:
        fh.write(",".join(cols) + "\n")
        for r in rows:
            fh.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
    print("# accounts: %d" % len(rows))
    print("%-24s %5s %6s %6s %5s %5s %s" % ("login", "score", "prs", "forks", "repos", "flw", "cap"))
    for r in rows[:15]:
        print("%-24s %5.3f %6d %6.2f %5d %5d %s" % (
            r["login"], r["score"], r["prs_window"], r["fork_ratio"], r["public_repos"], r["following"],
            "" if r["has_bio"] else "no-bio"))



def spread(login, days):
    """Stage 2: does this account open PRs in other people's repos, one each?

    A maintainer works through forks of their own project; a census bot spreads
    across strangers' repos with roughly one PR per repo. The score alone cannot
    tell them apart, this can.
    """
    since = iso(days_ago(days))
    d = api("/search/issues?q=author:%s+type:pr+created:%%3E=%s&per_page=100" % (login, since)) or {}
    items = d.get("items", [])
    total = d.get("total_count", len(items))
    repos = []
    for it in items:
        parts = it["repository_url"].split("/")
        repos.append(parts[-2] + "/" + parts[-1])
    upstream = [r for r in repos if r.split("/")[0].lower() != login.lower()]
    distinct = len(set(upstream))
    return dict(
        login=login,
        prs_total=total,
        prs_sampled=len(items),
        distinct_upstream=distinct,
        spread_ratio=round(distinct / max(1, len(repos)), 3) if repos else 0.0,
        one_pr_per_repo=round(distinct / max(1, len(upstream)), 3) if upstream else 0.0,
        next_pr_speed="%.1f/h" % ((len(items) / (days * 24.0)) if days else 0),
    )

if __name__ == "__main__":
    main()
