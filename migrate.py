#!/usr/bin/env python3
"""migrate.py — fill the store from the JSON cache. No API calls."""
import json, os, sys
import store

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
db = store.connect()
n = 0
for fn in os.listdir(CACHE):
    if not fn.endswith(".json"):
        continue
    path = "/" + fn[:-5]
    # the cache key is sanitised; recover the shape from the leading segment
    try:
        data = json.load(open(os.path.join(CACHE, fn)))
    except Exception:
        continue
    guess = path.replace("_", "/")
    try:
        store.remember(guess, data, db=db)
        n += 1
    except Exception as e:
        pass
db.commit()
print("cache files read:", n)
print(json.dumps(store.stats(db), indent=1))
db.close()
