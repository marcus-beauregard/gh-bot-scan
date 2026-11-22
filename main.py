#!/usr/bin/env python3
"""gh-bot-scan — find autonomous contributor accounts, then what attracts them.

Phase 1 (scan): seed from repos, expand through contributors, score each account
on features that do not require judgement, emit candidates ranked by score.

Rate limits: authenticated REST is 5000/hour. Every response is cached in
data/cache/, so a re-run costs nothing. --pace sleeps between calls.
"""
import argparse, csv, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

import store

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
            cached = json.load(open(fn))
            try:
                store.remember(path, cached)   # warm the store from cache too
            except Exception as e:
                sys.stderr.write("store: %s for %s\n" % (e, path))
            return cached
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
    try:
        store.remember(path, data)
    except Exception as e:            # the store must never break a scan
        sys.stderr.write("store: %s for %s\n" % (e, path))
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
    try:
        d = api("/search/issues?q=author:%s+type:pr+created:%%3E=%s&per_page=1" % (login, since))
    except Exception:
        return 0                          # invalid search syntax (brackets), not a candidate
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
    ap.add_argument("--attract", nargs="*", help="logins to pool into one attraction dataset")
    ap.add_argument("--stats", action="store_true", help="print what the store holds")
    ap.add_argument("--per-bot", type=int, default=60)
    ap.add_argument("--controls", type=int, default=1)
    a = ap.parse_args()

    if a.stats:
        print(json.dumps(store.stats(), indent=1))
        return

    if a.attract:
        rows = attract(a.attract, a.days, a.per_bot, a.controls, "data/attract.csv")
        report(rows)
        return

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
        if login.endswith("[bot]"):        # labelled bots are not what we hunt
            continue
        try:
            f = features(login, a.days)
        except SystemExit as e:
            sys.stderr.write("skip %s: %s\n" % (login, e))
            continue
        if not f:
            continue
        f["score"], f["components"] = score(f)
        try:
            db = store.connect()
            store.record_score(db, login, f["score"], f["components"])
            db.commit(); db.close()
        except Exception as e:
            sys.stderr.write("store: %s\n" % e)
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


REF = re.compile(r"(?i)(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s*#(\d+)")
REF_ANY = re.compile(r"#(\\d+)")


def issue_features(issue, repo_meta, pr_created):
    body = issue.get("body") or ""
    labels = [l["name"].lower() for l in issue.get("labels", [])]
    created = issue.get("created_at", "2026-01-01T00:00:00Z")
    age_days = (datetime.strptime(pr_created[:19], "%Y-%m-%dT%H:%M:%S")
                - datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")).days
    return dict(
        repo=repo_meta["full_name"],
        number=issue["number"],
        labels="|".join(labels),
        has_gfi=int("good first issue" in labels),
        has_helpwanted=int("help wanted" in labels),
        body_len=len(body),
        has_code_fence=int("\x60\x60\x60" in body),
        has_checklist=int("- [ ]" in body),
        n_comments=issue.get("comments", 0),
        is_assigned=int(bool(issue.get("assignees"))),
        age_days=max(0, age_days),
        stars=repo_meta.get("stargazers_count", 0),
    )


def attract(logins, days, per_bot, controls_per_repo, out_csv):
    """Pooled pickup rates across several detected accounts.

    For each bot: sample their recent PRs, resolve the issue each fixed, and pair
    it with untouched issues from the same repo. The pairing is what makes the
    rate meaningful — repos differ wildly in how picky their queues are.
    """
    rows = []
    for login in logins:
        since = iso(days_ago(days))
        d = api("/search/issues?q=author:%s+type:pr+created:%%3E=%s&per_page=100" % (login, since)) or {}
        items = d.get("items", [])[:per_bot]
        sys.stderr.write("%s: %d PRs sampled\n" % (login, len(items)))
        seen_repos = {}
        for pr in items:
            m = REF.search(pr.get("body") or "") or REF_ANY.search(pr.get("title") or "")
            if not m:
                continue
            parts = pr["repository_url"].split("/")
            owner, name = parts[-2], parts[-1]
            num = int(m.group(1))
            repo_key = owner + "/" + name
            if repo_key not in seen_repos:
                rm = api("/repos/%s" % repo_key) or {}
                seen_repos[repo_key] = rm
            rm = seen_repos[repo_key]
            issue = api("/repos/%s/issues/%d" % (repo_key, num))
            if not issue or "pull_request" in issue:
                continue
            rows.append(dict(bot=login, picked=1, **issue_features(issue, rm, pr["created_at"])))
            try:
                db = store.connect()
                store.upsert_issue(db, repo_key, issue)
                store.record_pick(db, repo_key, num, login)
                store.record_sample(db, login, repo_key, num, "picked")
                db.commit(); db.close()
            except Exception as e:
                sys.stderr.write("store: %s\n" % e)
        # controls: untouched open issues in the same repos
        for repo_key, rm in seen_repos.items():
            picked_numbers = {r["number"] for r in rows if r["repo"] == repo_key}
            listing = api("/repos/%s/issues?state=open&per_page=%d" % (repo_key, 30)) or []
            taken = 0
            for issue in listing:
                if "pull_request" in issue or issue["number"] in picked_numbers:
                    continue
                rows.append(dict(bot=login, picked=0, **issue_features(issue, rm, iso(time.time()))))
                try:
                    db = store.connect()
                    store.upsert_issue(db, repo_key, issue)
                    store.record_sample(db, login, repo_key, issue["number"], "control")
                    db.commit(); db.close()
                except Exception as e:
                    sys.stderr.write("store: %s\n" % e)
                taken += 1
                if taken >= controls_per_repo:
                    break
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    if rows:
        fh = csv.DictWriter(open(out_csv, "w"), fieldnames=list(rows[0].keys()))
        fh.writeheader()
        fh.writerows(rows)
    return rows


def report(rows):
    def rate(sel, name):
        sub = [r for r in rows if sel(r)]
        if not sub:
            return "%-30s n=0" % name
        p = sum(r["picked"] for r in sub)
        return "%-30s n=%-5d picked=%-4d rate=%5.1f%%" % (name, len(sub), p, 100.0 * p / len(sub))
    print("rows:", len(rows), "picked:", sum(r["picked"] for r in rows))
    for bot in sorted({r["bot"] for r in rows}):
        sub = [r for r in rows if r["bot"] == bot]
        print("\n== %s (n=%d)" % (bot, len(sub)))
        for sel, label in [
            (lambda r: r["has_gfi"] == 1, "good first issue"),
            (lambda r: r["has_gfi"] == 0, "no good first issue"),
            (lambda r: r["has_code_fence"] == 1, "fenced code block"),
            (lambda r: r["has_code_fence"] == 0, "no fence"),
            (lambda r: r["age_days"] <= 1, "issue <=1 day old"),
            (lambda r: r["age_days"] > 7, "issue >7 days old"),
        ]:
            print(rate(lambda r, s=sel: s(r) and r["bot"] == bot, label))

if __name__ == "__main__":
    main()
