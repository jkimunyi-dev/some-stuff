# Bootstrap only: all logic lives in the repo (kaggle/run.py). Update = git push, then re-run this kernel.
# Second batch slot. PIN: a commit to run (empty = latest master); pinning keeps an A/B experiment on exactly the intended code.
# BATCH: 1-based 50-row slice of Test.csv (empty = all 600).
import subprocess, shutil, os
REPO = "https://github.com/jkimunyi-dev/some-stuff.git"
PIN = "c452d97"
BATCH = "2"
PROFILE = "submit"
D = "/kaggle/working/some-stuff"
shutil.rmtree(D, ignore_errors=True)
subprocess.run(["git", "clone", REPO, D], check=True)
if PIN:
    subprocess.run(["git", "-C", D, "checkout", "-q", PIN], check=True)
print(subprocess.run(["git", "-C", D, "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
if BATCH:
    os.environ["QA_BATCH"] = BATCH
raise SystemExit(subprocess.run(["python", f"{D}/kaggle/run.py", PROFILE]).returncode)
