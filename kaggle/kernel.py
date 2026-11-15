# Bootstrap only: all logic lives in the repo (kaggle/run.py). Update = git push, then re-run this kernel.
import subprocess, shutil, os
REPO = "https://github.com/jkimunyi-dev/some-stuff.git"
D = "/kaggle/working/some-stuff"
shutil.rmtree(D, ignore_errors=True)
subprocess.run(["git", "clone", "--depth", "1", REPO, D], check=True)
print(subprocess.run(["git", "-C", D, "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
raise SystemExit(subprocess.run(["python", f"{D}/kaggle/run.py"]).returncode)
