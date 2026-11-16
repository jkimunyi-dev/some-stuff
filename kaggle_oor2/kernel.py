# OUT-OF-RULES EXPERIMENTS ONLY (branch out-of-rules). Never used for a Zindi submission.
# Bootstrap only: all logic lives in the repo (kaggle/run.py).
import subprocess, shutil, os
REPO = "https://github.com/jkimunyi-dev/some-stuff.git"
PIN = "ed30949"   # X2: teacher bank, 2B model
BATCH = "3"
PROFILE = "submit"
MODEL = "qwen3.5:2b"
EXTRA_ENV = {"QA_TEACHER": "1"}
D = "/kaggle/working/some-stuff"
shutil.rmtree(D, ignore_errors=True)
subprocess.run(["git", "clone", REPO, D], check=True)
subprocess.run(["git", "-C", D, "checkout", "-q", PIN], check=True)
print(subprocess.run(["git", "-C", D, "log", "--oneline", "-1"], capture_output=True, text=True).stdout)
os.environ.update(EXTRA_ENV)
if BATCH:
    os.environ["QA_BATCH"] = BATCH
raise SystemExit(subprocess.run(["python", f"{D}/kaggle/run.py", PROFILE, MODEL]).returncode)
