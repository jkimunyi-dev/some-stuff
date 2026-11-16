"""Cloudflare R2 (S3-compatible) sync for development runtimes: one private bucket holds the challenge data and run outputs.

    python -m qa.r2 push-data DATA_DIR            # local -> r2://BUCKET/data/  (+ data/MANIFEST.json with sha256)
    python -m qa.r2 pull-data DEST_DIR            # r2 -> DEST_DIR (skips files whose sha256 already matches)
    python -m qa.r2 push-run RUN_NAME FILE...     # outputs -> r2://BUCKET/runs/RUN_NAME/
    python -m qa.r2 pull-run RUN_NAME DEST_DIR    # r2 -> local
    python -m qa.r2 ls [PREFIX]

Credentials (never stored in the repo): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, read from the
environment, Kaggle secrets or Colab secrets. The bucket must stay PRIVATE: challenge data may not be redistributed.
Development convenience only: the submission's reproduction uses the organiser-supplied data, not R2.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def secret(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name)
    except Exception:
        pass
    try:
        from google.colab import userdata
        return userdata.get(name)
    except Exception:
        return None


def configured() -> bool:
    return all(secret(k) for k in KEYS)


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=f"https://{secret('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
                        aws_access_key_id=secret("R2_ACCESS_KEY_ID"), aws_secret_access_key=secret("R2_SECRET_ACCESS_KEY"),
                        region_name="auto", config=Config(retries={"max_attempts": 5, "mode": "standard"}))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def push_data(src: Path) -> dict:
    c, bucket = _client(), secret("R2_BUCKET")
    man = {}
    for p in sorted(x for x in Path(src).iterdir() if x.is_file()):
        man[p.name] = {"sha256": _sha(p), "bytes": p.stat().st_size}
        print(f"upload {p.name} ({p.stat().st_size / 1e6:.1f} MB)", flush=True)
        c.upload_file(str(p), bucket, f"data/{p.name}")
    c.put_object(Bucket=bucket, Key="data/MANIFEST.json", Body=json.dumps(man, indent=1).encode())
    return man


def pull_data(dest: Path) -> Path:
    c, bucket = _client(), secret("R2_BUCKET")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    man = json.loads(c.get_object(Bucket=bucket, Key="data/MANIFEST.json")["Body"].read())
    for name, m in man.items():
        p = dest / name
        if p.exists() and p.stat().st_size == m["bytes"] and _sha(p) == m["sha256"]:
            continue
        print(f"download {name} ({m['bytes'] / 1e6:.1f} MB)", flush=True)
        c.download_file(bucket, f"data/{name}", str(p))
        if _sha(p) != m["sha256"]:
            raise RuntimeError(f"checksum mismatch for {name}")
    print(f"data ready in {dest} ({len(man)} files, checksums verified)", flush=True)
    return dest


def push_run(run: str, files: list) -> None:
    c, bucket = _client(), secret("R2_BUCKET")
    for f in files:
        f = Path(f)
        if f.is_file():
            c.upload_file(str(f), bucket, f"runs/{run}/{f.name}")
            print(f"r2: runs/{run}/{f.name}", flush=True)


def pull_run(run: str, dest: Path) -> None:
    c, bucket = _client(), secret("R2_BUCKET")
    Path(dest).mkdir(parents=True, exist_ok=True)
    for o in c.list_objects_v2(Bucket=bucket, Prefix=f"runs/{run}/").get("Contents", []):
        c.download_file(bucket, o["Key"], str(Path(dest) / Path(o["Key"]).name))
        print("got", o["Key"])


def ls(prefix: str = "") -> None:
    c, bucket = _client(), secret("R2_BUCKET")
    for o in c.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
        print(f"{o['Size'] / 1e6:10.1f} MB  {o['LastModified']:%Y-%m-%d %H:%M}  {o['Key']}")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if not configured():
        sys.exit("R2 credentials missing: set " + ", ".join(KEYS))
    {"push-data": lambda: push_data(Path(args[0])), "pull-data": lambda: pull_data(Path(args[0])),
     "push-run": lambda: push_run(args[0], args[1:]), "pull-run": lambda: pull_run(args[0], Path(args[1])),
     "ls": lambda: ls(args[0] if args else "")}[cmd]()
