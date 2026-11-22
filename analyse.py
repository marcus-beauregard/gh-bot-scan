#!/usr/bin/env python3
"""analyse.py — every number from the database, restricted to the designed sample.

The denominator is the sample, not everything ever fetched: a rate over "issues
we happened to see" is not a rate. Picks are cases, controls are the rest.
"""
import argparse
import store

BASE = ("FROM samples s JOIN issues i ON i.repo = s.repo AND i.number = s.number "
        "WHERE i.is_pr = 0")


def rate(db, where, params=()):
    row = db.execute("SELECT count(*), sum(CASE WHEN s.role='picked' THEN 1 ELSE 0 END) " + BASE + where, params).fetchone()
    n, picked = row[0] or 0, row[1] or 0
    return n, picked, (100.0 * picked / n if n else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot")
    a = ap.parse_args()
    db = store.connect()
    extra = " AND s.bot = ?" if a.bot else ""
    p = (a.bot,) if a.bot else ()
    n, picked, pct = rate(db, extra, p)
    print("sample: n=%d picked=%d (%.1f%%)" % (n, picked, pct))
    for label, where in [
        ("good first issue", " AND i.has_gfi = 1"),
        ("no good first issue", " AND i.has_gfi = 0"),
        ("fenced code block", " AND i.has_code_fence = 1"),
        ("no fence", " AND i.has_code_fence = 0"),
        ("checkbox list", " AND i.has_checklist = 1"),
        ("no checkbox list", " AND i.has_checklist = 0"),
        ("help wanted only", " AND i.has_helpwanted = 1 AND i.has_gfi = 0"),
    ]:
        n, picked, pct = rate(db, where + extra, p)
        print("%-22s n=%-5d picked=%-4d rate=%5.1f%%" % (label, n, picked, pct))
    db.close()


if __name__ == "__main__":
    main()
