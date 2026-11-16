# Bootstrap only: all logic lives in the repo (kaggle/run.py). Update = git push, then re-run this kernel.
# One 50-question batch of Test.csv (rows (BATCH-1)*50 .. BATCH*50-1) with Qwen thinking on, for fast judge feedback.
import subprocess, shutil, os
REPO = "https://github.com/jkimunyi-dev/some-stuff.git"
PIN = ""  # a commit to run (empty = latest master)
BATCH = "1"
PROFILE = "submit_think"
D = "/kaggle/working/some-stuff"
shutil.rmtree(D, ignore_errors=True)
subprocess.run(["git", "clone", REPO, D], check=True)
if PIN:
    subprocess.run(["git", "-C", D, "checkout", "-q", PIN], check=True)
print(subprocess.run(["git", "-C", D, "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
os.environ["QA_BATCH"] = BATCH
raise SystemExit(subprocess.run(["python", f"{D}/kaggle/run.py", PROFILE]).returncode)
