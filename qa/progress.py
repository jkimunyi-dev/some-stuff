"""Live run progress: push a small counters-only JSON to a GitHub branch so a run can be watched while it is going.

Kernel side: Progress(token).update(rows, total) every N questions -> PUT progress.json on branch 'progress' via the GitHub
contents API (branch created from master on first use). Contains only counts and timings: no Test IDs, no question text.
Local side: `python -m qa.progress` prints the latest snapshot (public repo: no token needed to read).
"""
from __future__ import annotations

import base64
import collections
import json
import os
import time
import urllib.request

REPO = "jkimunyi-dev/some-stuff"
BRANCH = "progress"
PATH = "progress.json"
API = f"https://api.github.com/repos/{REPO}"


def _req(method, url, token=None, body=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "qa-progress"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, method=method, headers=h, data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


def category(err: str | None) -> str:
    if not err:
        return "answered"
    for k, v in [("needs a source of kind", "op/source mismatch"), ("no identifier", "identifier not found"), ("formats", "format check"),
                 ("no record has", "value not found"), ("records where", "value not found"), ("features where", "value not found"),
                 ("exceeded", "time cap"), ("crash", "crash"), ("bind failed", "bind invalid"), ("plan failed", "plan other")]:
        if k in err:
            return v
    return "other"


class Progress:
    def __init__(self, token: str | None, label: str = "submit"):
        self.token, self.label, self.t0, self.sha, self.ok = token, label, time.time(), None, bool(token)
        if self.ok:
            try:
                try:
                    _req("GET", f"{API}/git/ref/heads/{BRANCH}", token)
                except urllib.error.HTTPError:
                    base = _req("GET", f"{API}/git/ref/heads/master", token)["object"]["sha"]
                    _req("POST", f"{API}/git/refs", token, {"ref": f"refs/heads/{BRANCH}", "sha": base})
                try:
                    self.sha = _req("GET", f"{API}/contents/{PATH}?ref={BRANCH}", token).get("sha")
                except urllib.error.HTTPError:
                    self.sha = None
            except Exception as e:  # progress is best-effort; never break a run
                print("progress disabled:", type(e).__name__, e, flush=True)
                self.ok = False

    def update(self, rows: list[dict], total: int, note: str = "") -> None:
        if not self.ok:
            return
        snap = {"label": self.label, "updated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "elapsed_min": round((time.time() - self.t0) / 60, 1), "done": len(rows), "total": total,
                "answered": sum(1 for r in rows if not r.get("error")),
                "by_category": dict(collections.Counter(category(r.get("error")) for r in rows)),
                "by_domain": {d: {"done": n, "answered": sum(1 for r in rows if r.get("domain") == d and not r.get("error"))}
                              for d, n in collections.Counter(r.get("domain") for r in rows).items()},
                "by_op": dict(collections.Counter(r.get("op") or "none" for r in rows if not r.get("error"))),
                "max_calls": max((r["ledger"]["calls"] for r in rows), default=0),
                "max_tokens": max((r["ledger"]["tokens"] for r in rows), default=0), "note": note}
        body = {"message": f"progress {len(rows)}/{total}", "branch": BRANCH,
                "content": base64.b64encode(json.dumps(snap, indent=1).encode()).decode()}
        if self.sha:
            body["sha"] = self.sha
        try:
            self.sha = _req("PUT", f"{API}/contents/{PATH}", self.token, body)["content"]["sha"]
        except Exception as e:
            print("progress push failed:", type(e).__name__, e, flush=True)


def token_from_env_or_kaggle() -> str | None:
    t = os.environ.get("GH_TOKEN")
    if t:
        return t
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret("GH_TOKEN")
    except Exception:
        return None


if __name__ == "__main__":  # local watcher: latest snapshot (no token needed for a public repo)
    d = _req("GET", f"{API}/contents/{PATH}?ref={BRANCH}")
    print(base64.b64decode(d["content"]).decode())
