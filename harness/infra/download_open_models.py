"""Download the fully open model set (open data + open training code + open weights), verified.

Built for a flaky network: curl resumes partial files (`-C -`) and retries every error including DNS, unlike
`hf download`, which discards partial files on failure. Every LFS file is checked against the SHA-256 that
Hugging Face publishes, so the weights we serve are exactly the published ones.

    python3 harness/infra/download_open_models.py            # all, in order of use
    python3 harness/infra/download_open_models.py olmoe      # one
"""
import fnmatch
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MODELS = [  # (short name, repo, dir under ~/models)
    ("olmoe", "allenai/OLMoE-1B-7B-0125-Instruct", "olmoe-1b7b-instruct"),      # B: classify
    ("apertus", "swiss-ai/Apertus-8B-Instruct-2509", "apertus-8b-instruct"),    # C: cross-check
    ("olmo3", "allenai/Olmo-3-7B-Instruct", "olmo3-7b-instruct"),               # A candidate
    ("olmo2", "allenai/OLMo-2-1124-13B-Instruct", "olmo2-13b-instruct"),        # A candidate
]
PATTERNS = ["*.json", "*.safetensors", "*.txt", "tokenizer*", "*.model", "*.jinja", "README.md", "LICENSE*"]
ROOT = Path.home() / "models"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def listing(repo: str) -> list[dict]:
    for attempt in range(1, 61):
        try:
            with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}?blobs=true", timeout=30) as r:
                return json.load(r)["siblings"]
        except Exception as e:  # noqa: BLE001 - DNS/network flaps: keep trying
            log(f"listing {repo} failed ({type(e).__name__}); retry {attempt}")
            time.sleep(min(10 * attempt, 60))
    raise SystemExit(f"could not list {repo}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(repo: str, sib: dict, dest: Path) -> None:
    name, size = sib["rfilename"], sib.get("size")
    want = (sib.get("lfs") or {}).get("sha256")
    out = dest / name
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and size is not None and out.stat().st_size == size and (not want or sha256(out) == want):
        log(f"  ok (cached) {name}")
        return
    url = f"https://huggingface.co/{repo}/resolve/main/{name}"
    for attempt in range(1, 201):
        rc = subprocess.call(["curl", "-sSfL", "-C", "-", "--retry", "20", "--retry-all-errors", "--retry-delay", "5",
                              "--connect-timeout", "20", "-o", str(out), url])
        have = out.stat().st_size if out.exists() else 0
        if rc == 0 and (size is None or have == size):
            break
        if size is not None and have > size:   # corrupt partial: start over
            out.unlink()
        log(f"  {name}: curl rc={rc}, {have}/{size} bytes; retry {attempt}")
        time.sleep(min(5 * attempt, 60))
    else:
        raise SystemExit(f"gave up on {name}")
    if want and sha256(out) != want:
        out.unlink()
        raise SystemExit(f"SHA-256 mismatch on {name}; deleted, re-run to fetch again")
    log(f"  ok {name} ({(size or 0) / 1e9:.2f} GB{', sha256 verified' if want else ''})")


def main() -> None:
    picked = [m for m in MODELS if len(sys.argv) < 2 or m[0] in sys.argv[1:]]
    for short, repo, d in picked:
        dest = ROOT / d
        files = [s for s in listing(repo) if any(fnmatch.fnmatch(s["rfilename"], p) for p in PATTERNS)]
        log(f"{short}: {repo} -> {dest} ({len(files)} files, "
            f"{sum(s.get('size') or 0 for s in files) / 1e9:.1f} GB)")
        for s in files:
            fetch(repo, s, dest)
        log(f"{short}: DONE")
    log("ALL DONE")


if __name__ == "__main__":
    main()
