"""Kaggle runtime driver: set up -> Ollama + Qwen -> run Train+dev items (approved sources only) -> scores in /kaggle/working.

Edit RUNS below (via git push) to change what a kernel run does. Test sources are never read here.
"""
import glob, json, os, subprocess, sys, time, zipfile, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path("/kaggle/working")
MODELS = ["qwen2.5:3b-instruct-q8_0", "qwen2.5:3b-instruct"]  # first that pulls wins (Q8 = higher precision, same parameter count)
RUNS = [  # (name, extra args): real end-to-end Qwen runs only
    ("qwen_dev", ["--split", "dev"]),
]


def sh(cmd, check=False, **kw):
    print("$", cmd, flush=True)
    r = subprocess.run(cmd, shell=True, **kw)
    if check and r.returncode:
        sys.exit(f"failed: {cmd}")
    return r


t0 = time.time()
sh("nvidia-smi --query-gpu=name,memory.total --format=csv; nproc; free -g | head -2; df -h /kaggle/working | tail -1")
train = glob.glob("/kaggle/input/**/Train.csv", recursive=True)
assert train, "dataset not mounted"
DATA = Path(train[0]).parent
print("DATA", DATA, os.listdir(DATA))

# --- python deps (kernel image already has most)
sh(f"pip -q install -r {REPO}/requirements.txt pytest 2>&1 | tail -2")

# --- extract sources
EX = Path("/tmp/extracted")  # NOT /kaggle/working: that would ship 437MB of sources as kernel output
if not (EX / "sources").is_dir():
    z = next(p for p in DATA.iterdir() if p.stat().st_size > 50_000_000 and zipfile.is_zipfile(p))
    zipfile.ZipFile(z).extractall(EX)
SRC = EX / "sources"
print(len(os.listdir(SRC)), "source files")


# --- ollama
sh("apt-get -qq install -y zstd pciutils lshw > /dev/null 2>&1")
sh("curl -fsSL https://ollama.com/install.sh | sh 2>&1 | tail -3")
subprocess.Popen("nohup ollama serve > /kaggle/working/ollama.log 2>&1 &", shell=True)
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2)
        break
    except Exception:
        time.sleep(2)
MODEL = next((m for m in MODELS if sh(f"ollama pull {m} > /tmp/pull.log 2>&1").returncode == 0), None)
assert MODEL, "no model could be pulled"
show = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/show", data=json.dumps({"model": MODEL}).encode())).read())
print("MODEL", show["details"], show["model_info"].get("general.parameter_count"))
(OUT / "MODEL.json").write_text(json.dumps({"model": MODEL, "details": show["details"], "parameter_count": show["model_info"].get("general.parameter_count")}, indent=1))

# --- runs
for name, args in RUNS:
    sh(f"cd {REPO} && python -u -m qa.run_dev --sources {SRC} --data {DATA} --model {MODEL} {' '.join(args)} --out /kaggle/working/{name}.json 2>&1")
print("done in", round(time.time() - t0), "s")
