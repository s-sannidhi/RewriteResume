"""One-command setup. Works on Windows, macOS and Linux.

    python setup.py

Creates the virtualenv, installs dependencies, downloads the Playwright browser used for PDF
rendering, checks Ollama and offers to pull the model, creates your data folder with a starter profile
and skills file, and can restore a backup so you never retype your profile.

Safe to re-run: every step detects what is already done and skips it.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = "gemma3:12b"
EMBED_MODEL = "nomic-embed-text"
MIN_PY = (3, 10)

# Approximate download sizes, so nobody stares at a silent terminal wondering if it died.
MODEL_GB = {"gemma3:12b": 8.1, "gemma3:4b": 3.3, "nomic-embed-text": 0.3}
# gemma3:12b wants roughly this much RAM to run comfortably.
MODEL_MIN_RAM_GB = {"gemma3:12b": 12, "gemma3:4b": 6}

OK, WARN, BAD = "[ ok ]", "[note]", "[FAIL]"


def say(tag: str, msg: str) -> None:
    print(f"{tag} {msg}", flush=True)


# ------------------------------------------------------------------ console helpers
def _unicode_ok() -> bool:
    """Windows consoles still default to cp1252 in places, where printing a block character
    raises UnicodeEncodeError mid-progress-bar. Check before using one."""
    try:
        "█".encode(sys.stdout.encoding or "utf-8")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


FULL, EMPTY = ("█", "░") if _unicode_ok() else ("#", "-")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(sec: float) -> str:
    if sec < 0 or sec > 86400:
        return "--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def progress_bar(label: str, done: float, total: float, started: float, width: int = 26) -> None:
    """One-line progress with percentage, size, speed and ETA. Rewrites in place with \\r."""
    pct = (done / total) if total else 0.0
    filled = int(width * pct)
    elapsed = max(time.time() - started, 0.001)
    speed = done / elapsed
    eta = (total - done) / speed if speed > 0 else -1
    line = (f"\r  {label:<18} [{FULL * filled}{EMPTY * (width - filled)}] {pct * 100:5.1f}%  "
            f"{human_bytes(done)}/{human_bytes(total)}  {human_bytes(speed)}/s  ETA {human_time(eta)}   ")
    sys.stdout.write(line[:150])
    sys.stdout.flush()


def total_ram_gb() -> float | None:
    """Physical RAM, without adding a dependency."""
    try:
        if IS_WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MS()
            st.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024 ** 3
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
            return int(out.stdout.strip()) / 1024 ** 3
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / 1024 ** 3
    except Exception:
        return None


def free_disk_gb() -> float | None:
    try:
        return shutil.disk_usage(Path.home()).free / 1024 ** 3
    except Exception:
        return None


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), **kw)


# --------------------------------------------------------------------------- steps
def check_python() -> bool:
    if sys.version_info < MIN_PY:
        say(BAD, f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required, this is "
                 f"{sys.version_info.major}.{sys.version_info.minor}. Install a newer Python "
                 f"from python.org and re-run.")
        return False
    say(OK, f"Python {sys.version_info.major}.{sys.version_info.minor} on "
            f"{platform.system()} {platform.machine()}")
    return True


def make_venv() -> bool:
    if venv_python().exists():
        say(OK, f"virtualenv already exists ({VENV.name})")
        return True
    say(WARN, "creating virtualenv…")
    r = run([sys.executable, "-m", "venv", str(VENV)])
    if r.returncode != 0 or not venv_python().exists():
        say(BAD, "could not create the virtualenv. On Debian/Ubuntu: sudo apt install python3-venv")
        return False
    say(OK, "virtualenv created")
    return True


def install_deps() -> bool:
    say(WARN, "installing dependencies (a minute or two the first time)…")
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    r = run([py, "-m", "pip", "install", "-r", "requirements.txt", "-q"])
    if r.returncode != 0:
        say(BAD, "pip install failed — scroll up for the reason")
        return False
    say(OK, "dependencies installed")
    return True


def install_browser() -> bool:
    """Playwright renders the résumé PDF; it needs its own Chromium."""
    say(WARN, "installing the Playwright browser (~150 MB, first time only)…")
    r = run([str(venv_python()), "-m", "playwright", "install", "chromium"])
    if r.returncode != 0:
        say(WARN, "playwright install failed. PDF rendering will not work until you run:\n"
                  f"       {venv_python()} -m playwright install chromium")
        return False
    say(OK, "Playwright browser ready")
    return True


def _ollama_tags() -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as resp:
            import json
            return [m["name"] for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def check_ollama(assume_yes: bool) -> bool:
    tags = _ollama_tags()
    if tags is None:
        say(WARN, "Ollama is not running. It powers all the writing (nothing is sent to a cloud).")
        say(WARN, "  Install from https://ollama.com/download, start it, then re-run this script")
        say(WARN, f"  or just run:  ollama pull {MODEL} && ollama pull {EMBED_MODEL}")
        return False
    say(OK, "Ollama is running")
    for model in (MODEL, EMBED_MODEL):
        if any(t.startswith(model.split(":")[0]) for t in tags):
            say(OK, f"model present: {model}")
            continue
        if not shutil.which("ollama"):
            say(WARN, f"missing model {model}; run:  ollama pull {model}")
            continue
        if assume_yes or input(f"  pull {model} now? (several GB) [y/N] ").strip().lower() == "y":
            say(WARN, f"pulling {model}…")
            if run(["ollama", "pull", model]).returncode == 0:
                say(OK, f"pulled {model}")
            else:
                say(WARN, f"pull failed; run manually:  ollama pull {model}")
        else:
            say(WARN, f"skipped {model} — the app needs it:  ollama pull {model}")
    return True


def seed_data_files() -> bool:
    """A fresh clone has no ~/ResumeRewriter — the data folder lives outside the repo and is never
    committed — so the app used to open on an error instead of a form. Create the folder with an
    empty profile and a starter skills file. Existing data is never touched."""
    r = run([str(venv_python()), "-c",
             "import sys; sys.path.insert(0,'.');"
             "from backend import config, profile_store;"
             "from backend.resume import skills_source as s;"
             "new_profile = not config.PROFILE_PATH.exists();"
             "profile_store.load();"
             "new_skills = s.ensure_template();"
             "print(config.DATA_DIR); print(new_profile); print(s.SKILLS_PATH.name);"
             "print(new_skills)"],
            capture_output=True, text=True)
    out = (r.stdout or "").strip().splitlines()
    if len(out) != 4:
        say(WARN, "could not prepare your data folder; the app will create it on first run")
        for line in (r.stderr or "").strip().splitlines()[-2:]:
            say(WARN, f"  {line}")
        return True
    data_dir, new_profile, skills_name, new_skills = out[0], out[1] == "True", out[2], out[3] == "True"
    say(OK, f"data folder ready: {data_dir}")
    say(OK, "created an empty profile.json — fill it in on the website's Resume tab"
        if new_profile else "profile.json already there (your data was left untouched)")
    say(OK, f"wrote a starter {skills_name} — edit it; the Skills section reads only from there"
        if new_skills else f"verified-skills file present ({skills_name})")
    return True


def restore_backup(path: str | None) -> bool:
    if not path:
        return True
    say(WARN, f"restoring your data from {path}…")
    r = run([str(venv_python()), "scripts/backup_data.py", "import", path, "--force"])
    if r.returncode != 0:
        say(BAD, "restore failed")
        return False
    say(OK, "data restored — nothing to retype")
    return True


def restore_repo_data(force: bool) -> bool:
    """If a local (gitignored) data/ mirror exists, copy it into ~/ResumeRewriter.

    Public clones have no data/ — they get an empty profile from seed_data_files() instead.
    Existing ~/ResumeRewriter files are never overwritten unless --force-data is passed.
    """
    src = ROOT / "data"
    if not src.is_dir():
        return True                       # public clone: nothing to restore
    out = run([str(venv_python()), "-c",
               "import sys; sys.path.insert(0, '.'); from backend import config;"
               "print(config.DATA_DIR)"],
              capture_output=True, text=True)
    if out.returncode != 0:
        say(WARN, "couldn't locate the data folder — skipping local data/ mirror")
        return True
    dest = Path(out.stdout.strip().splitlines()[-1])
    dest.mkdir(parents=True, exist_ok=True)

    copied, kept = [], []

    def place(s_path: Path, d_path: Path) -> None:
        if s_path.is_dir():
            d_path.mkdir(parents=True, exist_ok=True)
            for child in s_path.iterdir():
                place(child, d_path / child.name)
            return
        if d_path.exists() and not force:
            kept.append(d_path.name)
            return
        d_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s_path, d_path)
        copied.append(d_path.name)

    for item in sorted(src.iterdir()):
        if item.name == ".DS_Store":
            continue
        place(item, dest / item.name)

    if copied:
        say(OK, f"restored from local data/ mirror ({', '.join(sorted(set(copied))[:6])}"
                f"{'…' if len(set(copied)) > 6 else ''})")
    if kept:
        say(WARN, f"kept the data already on this machine ({len(set(kept))} file(s)); "
                  "re-run with --force-data to overwrite from data/")
    return True


def install_git_hook() -> None:
    if not (ROOT / ".git").exists():
        return
    run([str(venv_python()), "scripts/check_no_personal_data.py", "--install-hook"],
        capture_output=True, text=True)
    say(OK, "installed the pre-commit guard against committing personal data")


def finish() -> None:
    start = "run.bat" if IS_WIN else "./run.sh"
    print("\n" + "=" * 68)
    print("  Setup complete.")
    print("=" * 68)
    print(f"\n  1. Start the server:      {start}")
    print("  2. Open the website:      http://127.0.0.1:8765/app/")
    print("  3. Fill in your profile:  the Resume tab (stored in ~/ResumeRewriter/)")
    print("  4. Load the extension:    chrome://extensions → Developer mode →")
    print(f"                            'Load unpacked' → select {ROOT / 'extension'}")
    print("\n  Your personal data lives ONLY in ~/ResumeRewriter — never in this git repo.")
    print("  Move machines with a zip you carry yourself:")
    print("      python scripts/backup_data.py export")
    print("      python setup.py --restore ~/resumerewriter-backup-YYYY-MM-DD.zip")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", metavar="ZIP", help="restore a backup made by backup_data.py")
    ap.add_argument("--force-data", action="store_true",
                    help="overwrite existing ~/ResumeRewriter files with a local data/ mirror")
    ap.add_argument("--yes", "-y", action="store_true", help="don't prompt (pull models silently)")
    ap.add_argument("--skip-browser", action="store_true", help="skip the Playwright download")
    a = ap.parse_args()

    print("\nResume Rewriter — setup\n" + "-" * 30)
    if not check_python():
        return 1
    if not make_venv():
        return 1
    if not install_deps():
        return 1
    if not a.skip_browser:
        install_browser()
    check_ollama(a.yes)
    seed_data_files()
    # Optional local data/ mirror first, then an explicit zip if the user passed one.
    restore_repo_data(a.force_data)
    if not restore_backup(a.restore):
        return 1
    install_git_hook()
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
