#!/usr/bin/env python3
"""store.py — the SQLite record behind gh-bot-scan.

A cache answers "what did I fetch". A store answers "what do I know": every
user, repo and issue the scan has seen, with the fields the analysis needs,
so a later run can widen the sample or re-run an analysis without touching
the API.

The JSON cache stays — it is the replay log. This is the queryable view.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scan.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  login TEXT PRIMARY KEY,
  public_repos INTEGER, followers INTEGER, following INTEGER,
  created_at TEXT, has_bio INTEGER, has_name INTEGER, has_company INTEGER,
  kind TEXT, fetched_at TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS repos (
  full_name TEXT PRIMARY KEY, owner TEXT, name TEXT,
  stars INTEGER, language TEXT, fork INTEGER, topics TEXT,
  open_issues INTEGER, fetched_at TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  repo TEXT, number INTEGER, title TEXT, labels TEXT,
  has_gfi INTEGER, has_helpwanted INTEGER, body_len INTEGER,
  has_code_fence INTEGER, has_checklist INTEGER, n_comments INTEGER,
  assigned INTEGER, created_at TEXT, is_pr INTEGER,
  fetched_at TEXT,
  PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS scores (
  login TEXT, score REAL, components TEXT, scored_at TEXT,
  PRIMARY KEY (login, scored_at)
);
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY, kind TEXT, args TEXT, at TEXT, outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_repo ON issues(repo);
CREATE INDEX IF NOT EXISTS idx_issues_gfi ON issues(has_gfi);
CREATE INDEX IF NOT EXISTS idx_scores_login ON scores(login);
CREATE TABLE IF NOT EXISTS picks (
  repo TEXT, number INTEGER, bot TEXT, picked_at TEXT,
  PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS idx_picks_bot ON picks(bot);
CREATE TABLE IF NOT EXISTS samples (
  bot TEXT, repo TEXT, number INTEGER, role TEXT, run_at TEXT,
  PRIMARY KEY (bot, repo, number)
);
CREATE INDEX IF NOT EXISTS idx_samples_role ON samples(role);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None):
    p = path or DB
    os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p)
    db.executescript(SCHEMA)
    return db


def _labels(issue):
    return "|".join(sorted(l["name"].lower() for l in issue.get("labels", [])))


def remember(path, data, db=None):
    """Route one API response into the store. Unknown shapes are ignored."""
    if data is None:
        return
    own = db is None
    db = db or connect()
    try:
        if re.fullmatch(r"/users/[^/]+", path or ""):
            upsert_user(db, data)
        elif re.fullmatch(r"/repos/[^/]+/[^/]+", path or "") or path.startswith("/repos/") and path.count("/") == 3:
            upsert_repo(db, data)
        elif re.match(r"^/repos/[^/]+/[^/]+/issues/\d+$", path or ""):
            owner, name = path.split("/")[2:4]
            upsert_issue(db, owner + "/" + name, data)
        elif re.match(r"^/users/[^/]+/repos", path or "") and isinstance(data, list):
            for r in data:
                upsert_repo(db, r)
        elif re.match(r"^/repos/[^/]+/[^/]+/issues(\?|$)", path or "") and isinstance(data, list):
            owner, name = path.split("/")[2:4]
            for issue in data:
                upsert_issue(db, owner + "/" + name, issue)
        if own:
            db.commit()
    finally:
        if own:
            db.close()


def upsert_user(db, u):
    if not isinstance(u, dict) or "login" not in u:
        return
    db.execute(
        "INSERT INTO users (login, public_repos, followers, following, created_at, has_bio, has_name, has_company, kind, fetched_at, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(login) DO UPDATE SET public_repos=excluded.public_repos, followers=excluded.followers,"
        " following=excluded.following, created_at=excluded.created_at, has_bio=excluded.has_bio,"
        " has_name=excluded.has_name, has_company=excluded.has_company, kind=excluded.kind,"
        " fetched_at=excluded.fetched_at, raw=excluded.raw",
        (u["login"], u.get("public_repos", 0), u.get("followers", 0), u.get("following", 0),
         u.get("created_at", ""), 1 if u.get("bio") else 0, 1 if u.get("name") else 0,
         1 if u.get("company") else 0, u.get("type", ""), now(), json.dumps(u)[:20000]))


def upsert_repo(db, r):
    if not isinstance(r, dict) or "full_name" not in r:
        return
    owner, _, name = r["full_name"].partition("/")
    db.execute(
        "INSERT INTO repos (full_name, owner, name, stars, language, fork, topics, open_issues, fetched_at, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(full_name) DO UPDATE SET stars=excluded.stars, language=excluded.language,"
        " fork=excluded.fork, topics=excluded.topics, open_issues=excluded.open_issues,"
        " fetched_at=excluded.fetched_at, raw=excluded.raw",
        (r["full_name"], owner, name, r.get("stargazers_count", 0), r.get("language") or "",
         1 if r.get("fork") else 0, "|".join(r.get("topics") or []),
         r.get("open_issues_count", 0), now(), json.dumps(r)[:20000]))


def upsert_issue(db, repo, issue):
    if not isinstance(issue, dict) or "number" not in issue:
        return
    body = issue.get("body") or ""
    labels = _labels(issue)
    db.execute(
        "INSERT INTO issues (repo, number, title, labels, has_gfi, has_helpwanted, body_len,"
        " has_code_fence, has_checklist, n_comments, assigned, created_at, is_pr, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(repo, number) DO UPDATE SET title=excluded.title, labels=excluded.labels,"
        " has_gfi=excluded.has_gfi, has_helpwanted=excluded.has_helpwanted, body_len=excluded.body_len,"
        " has_code_fence=excluded.has_code_fence, has_checklist=excluded.has_checklist,"
        " n_comments=excluded.n_comments, assigned=excluded.assigned, fetched_at=excluded.fetched_at",
        (repo, issue["number"], (issue.get("title") or "")[:300], labels,
         1 if "good first issue" in labels else 0, 1 if "help wanted" in labels else 0,
         len(body), 1 if chr(96)*3 in body else 0, 1 if "- [ ]" in body else 0,
         issue.get("comments", 0), 1 if issue.get("assignees") else 0,
         issue.get("created_at", ""), 1 if "pull_request" in issue else 0, now()))



def record_pick(db, repo, number, bot):
    db.execute("INSERT OR REPLACE INTO picks (repo, number, bot, picked_at) VALUES (?,?,?,?)",
               (repo, number, bot, now()))

def record_sample(db, bot, repo, number, role):
    db.execute("INSERT OR REPLACE INTO samples (bot, repo, number, role, run_at) VALUES (?,?,?,?,?)",
               (bot, repo, number, role, now()))


def record_score(db, login, score, components):
    db.execute("INSERT OR REPLACE INTO scores (login, score, components, scored_at) VALUES (?,?,?,?)",
               (login, score, json.dumps(components), now()))


def record_scan(db, kind, args, outcome):
    sid = now() + "-" + kind
    db.execute("INSERT OR REPLACE INTO scans (id, kind, args, at, outcome) VALUES (?,?,?,?,?)",
               (sid, kind, json.dumps(args), now(), str(outcome)[:500]))


def stats(db=None):
    own = db is None
    db = db or connect()
    try:
        out = {}
        for t in ("users", "repos", "issues", "scores", "scans"):
            out[t] = db.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
        out["picks"] = db.execute("SELECT count(*) FROM picks").fetchone()[0]
        out["samples"] = db.execute("SELECT count(*) FROM samples").fetchone()[0]
        out["issues_with_gfi"] = db.execute("SELECT count(*) FROM issues WHERE has_gfi=1").fetchone()[0]
        out["issues_is_pr"] = db.execute("SELECT count(*) FROM issues WHERE is_pr=1").fetchone()[0]
        out["repos_with_stars"] = db.execute("SELECT count(*) FROM repos WHERE stars>0").fetchone()[0]
        out["top_scores"] = db.execute(
            "SELECT login, max(score) s FROM scores GROUP BY login ORDER BY s DESC LIMIT 5").fetchall()
        return out
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(json.dumps(stats(), indent=1))
