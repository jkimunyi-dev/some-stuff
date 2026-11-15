"""Kaggle runtime driver: set up -> Ollama + Qwen -> run Train+dev items (approved sources only) -> scores in /kaggle/working.

Edit RUNS below (via git push) to change what a kernel run does. Test sources are never read here.
"""
import glob, json, os, subprocess, sys, time, zipfile, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path("/kaggle/working")
MODELS = ["qwen3.5:2b"]  # user choice; must be <= 3B total parameters (checked below)
PROFILES = {  # selected by the kernel's first argument; real end-to-end Qwen runs only
    "full": [("qwen_dev", ["--split", "dev", "--workers", "4"]), ("qwen_dev_think", ["--split", "dev", "--think", "--workers", "4"])],
    "fast": [("qwen_dev", ["--split", "dev", "--workers", "4"])],
    "confirm": [("qwen_dev", ["--split", "dev", "--workers", "4"]), ("qwen_holdout", ["--split", "holdout", "--workers", "4"])],
}
RUNS = PROFILES[sys.argv[1] if len(sys.argv) > 1 else "full"]


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
subprocess.Popen("OLLAMA_NUM_PARALLEL=4 nohup ollama serve > /kaggle/working/ollama.log 2>&1 &", shell=True)  # 4 concurrent requests
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2)
        break
    except Exception:
        time.sleep(2)
meta = {}
for MODEL in MODELS:
    if sh(f"ollama pull {MODEL} > /tmp/pull.log 2>&1").returncode != 0:
        print("PULL FAILED", MODEL, open("/tmp/pull.log").read()[-300:])
        continue
    show = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/show", data=json.dumps({"model": MODEL}).encode())).read())
    pc = show["model_info"].get("general.parameter_count")
    print("MODEL", MODEL, show["details"], pc, show.get("capabilities"))
    tags = json.loads(urllib.request.urlopen("http://127.0.0.1:11434/api/tags").read())
    ver = json.loads(urllib.request.urlopen("http://127.0.0.1:11434/api/version").read()).get("version")
    digest = next((m.get("digest") for m in tags.get("models", []) if m.get("name") == MODEL), None)
    meta[MODEL] = {"details": show["details"], "parameter_count": pc, "capabilities": show.get("capabilities"), "digest": digest, "ollama_version": ver}
    print("DIGEST", digest, "OLLAMA", ver)
    if not pc or pc > 3_200_000_000:
        print("SKIP: over the 3B class", MODEL)
        continue
    tag = MODEL.replace(":", "_").replace(".", "")
    for name, args in RUNS:
        sh(f"cd {REPO} && python -u -m qa.run_dev --sources {SRC} --data {DATA} --model {MODEL} {' '.join(args)} --out /kaggle/working/{name}_{tag}.json 2>&1")
    sh(f"ollama stop {MODEL}")
(OUT / "MODEL.json").write_text(json.dumps(meta, indent=1))
print("done in", round(time.time() - t0), "s")
