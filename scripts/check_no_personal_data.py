"""Guard: fail if anything personal would be committed to this PUBLIC repo.

Real data lives only in ~/ResumeRewriter (outside the repo). This script is the backstop for a
stray paste — a phone number in a comment, an exported profile dropped into the tree, a database
file force-added.

    python scripts/check_no_personal_data.py            # scan tracked + staged files
    python scripts/check_no_personal_data.py --all      # also scan untracked files

Exit code 0 = clean, 1 = something personal found. Wire it in as a pre-commit hook with:
    python scripts/check_no_personal_data.py --install-hook
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that legitimately discuss these topics without containing real data.
SKIP_FILES = {
    "scripts/check_no_personal_data.py",
    "README.md",
}
SKIP_DIRS = (".venv/", "node_modules/", ".git/", "__pycache__/", "tests/")

# Patterns for data that must never be public. Deliberately narrow to avoid crying wolf.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("personal email", re.compile(
        r"[a-zA-Z0-9._%+-]+@(?:gmail|yahoo|outlook|hotmail|icloud|proton(?:mail)?|utexas|edu)\.[a-z]{2,}",
        re.I)),
    ("US phone number", re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
    ("street address", re.compile(r"\b\d{3,5}\s+[A-Z][A-Za-z]*\s+(?:St|Street|Ave|Avenue|Rd|Road|"
                                  r"Blvd|Dr|Drive|Ln|Lane|Ct|Court|Way|Pkwy)\b")),
    ("API key / token", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
                                   r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# Whole files that must never be tracked, whatever their contents.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(profile\.json|rr\.db|skills_verified\.yaml|qa_memory\.json|tracker\.json|"
    r"site_memory\.json|bullet_emb_cache\.json|.*\.sqlite3?|resumerewriter[-_]backup.*\.zip)$", re.I)


def _git(*args: str) -> list[str]:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def files_to_scan(include_untracked: bool) -> list[str]:
    # Tracked files + staged adds/modifies (NOT deletions — untracking data/ must be allowed).
    files = set(_git("ls-files"))
    files |= set(_git("diff", "--cached", "--name-only", "--diff-filter=ACMR"))
    if include_untracked:
        files |= set(_git("ls-files", "--others", "--exclude-standard"))
    out = []
    for f in sorted(files):
        if f in SKIP_FILES or any(f.startswith(d) for d in SKIP_DIRS):
            continue
        if (ROOT / f).is_file():
            out.append(f)
    return out


def scan(include_untracked: bool = False) -> list[str]:
    problems = []
    for f in files_to_scan(include_untracked):
        if FORBIDDEN_NAMES.search(f) or f == "data" or f.startswith("data/"):
            problems.append(f"{f}: personal data file must never be tracked (see .gitignore)")
            continue
        try:
            text = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, rx in PATTERNS:
            for m in rx.finditer(text):
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{f}:{line}: {label} -> {m.group(0)[:60]}")
    return problems


HOOK = """#!/bin/sh
# Blocks a commit that would put personal data into this public repo.
python3 scripts/check_no_personal_data.py || exit 1
"""


def install_hook() -> None:
    hooks = ROOT / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    p = hooks / "pre-commit"
    p.write_text(HOOK, encoding="utf-8")
    p.chmod(0o755)
    print(f"Installed pre-commit hook at {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also scan untracked files")
    ap.add_argument("--install-hook", action="store_true", help="install as a git pre-commit hook")
    args = ap.parse_args()
    if args.install_hook:
        install_hook()
        return 0
    problems = scan(args.all)
    if problems:
        print("PERSONAL DATA WOULD BE COMMITTED TO A PUBLIC REPO:\n")
        for p in problems:
            print("  " + p)
        print("\nRemove it, or add the file to .gitignore, then re-run.")
        return 1
    print("Clean: no personal data found in tracked/staged files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
