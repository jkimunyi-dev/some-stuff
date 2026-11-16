# Bootstrap only: all logic lives in the repo (kaggle/run.py). Update = git push, then re-run this kernel.
# PIN: a commit to run (empty = latest master). Pinning keeps an A/B experiment on exactly the intended code.
import subprocess, shutil, os
REPO = "https://github.com/jkimunyi-dev/some-stuff.git"
PIN = "eadc832"
D = "/kaggle/working/some-stuff"
shutil.rmtree(D, ignore_errors=True)
subprocess.run(["git", "clone", REPO, D], check=True)
if PIN:
    subprocess.run(["git", "-C", D, "checkout", "-q", PIN], check=True)
print(subprocess.run(["git", "-C", D, "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
raise SystemExit(subprocess.run(["python", f"{D}/kaggle/run.py", "submit_think"]).returncode)
