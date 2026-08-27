"""Move your data between machines WITHOUT retyping it — and without it ever touching this
public repo.

Everything personal lives in ~/ResumeRewriter (profile, verified skills, résumé history, Q&A
memory, saved documents). This packs that folder into one .zip you carry yourself (USB, private
cloud folder, password manager attachment) and unpacks it on the next machine.

    python scripts/backup_data.py export                 # -> ~/resumerewriter-backup-<date>.zip
    python scripts/backup_data.py export --out D:\\rr.zip
    python scripts/backup_data.py import ~/resumerewriter-backup-2026-08-17.zip

The zip is gitignored. Never commit it: it contains your address, phone, work history and every
answer you have saved.
"""
import argparse
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config, profile_store  # noqa: E402
from backend.resume import skills_source  # noqa: E402

DATA = config.DATA_DIR

# What to carry. Caches are excluded: they rebuild themselves and are the bulk of the size.
# "generic" is deliberately absent: the generic-résumé fallback was removed 2026-08-17, so that
# folder is a stale artifact, and résumés don't travel in the snapshot anyway.
INCLUDE = ["profile.json", "skills_verified.yaml", "rr.db", "documents"]
EXCLUDE_PARTS = {"__pycache__", ".jobright_profile", "bullet_emb_cache.json", "server.log"}
EXCLUDE_SUFFIX = {".log"}


def _keep(rel: Path) -> bool:
    if any(p in EXCLUDE_PARTS for p in rel.parts):
        return False
    return rel.suffix.lower() not in EXCLUDE_SUFFIX


def _is_untouched_placeholder(rel: str) -> bool:
    """setup.py seeds an empty profile.json and a starter skills_verified.yaml so a fresh install
    opens on a form instead of an error. Those placeholders must not make a restore look like it
    would destroy real data — nothing of the user's is in them yet."""
    target = DATA / rel
    try:
        if rel == "profile.json":
            return profile_store.is_empty(json.loads(target.read_text(encoding="utf-8")))
        if rel == "skills_verified.yaml":
            return skills_source.is_untouched_template()
    except (OSError, ValueError):
        return False
    return False


def do_export(out: Path | None) -> int:
    if not DATA.exists():
        print(f"No data directory at {DATA} — nothing to export.")
        return 1
    out = out or Path.home() / f"resumerewriter-backup-{time.strftime('%Y-%m-%d')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            src = DATA / name
            if not src.exists():
                continue
            if src.is_file():
                z.write(src, name)
                n += 1
            else:
                for f in src.rglob("*"):
                    rel = f.relative_to(DATA)
                    if f.is_file() and _keep(rel):
                        z.write(f, str(rel))
                        n += 1
    size = out.stat().st_size / 1_048_576
    print(f"Exported {n} files -> {out}  ({size:.1f} MB)")
    print("\nKeep this file private. It contains your profile, résumé history and saved answers.")
    print("Copy it to the other machine, then run:")
    print(f"    python scripts/backup_data.py import {out.name}")
    return 0


def do_import(path: Path, force: bool) -> int:
    if not path.exists():
        print(f"No such file: {path}")
        return 1
    DATA.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        clashes = [n for n in names
                   if (DATA / n).exists() and not _is_untouched_placeholder(n)]
        if clashes and not force:
            print(f"{len(clashes)} file(s) already exist in {DATA}, e.g.:")
            for c in clashes[:5]:
                print("   " + c)
            print("\nRe-run with --force to overwrite them, or move your existing data aside first.")
            return 1
        for n in names:
            target = DATA / n
            # Guard against a zip entry escaping the data dir (zip-slip).
            if not str(target.resolve()).startswith(str(DATA.resolve())):
                print(f"Refusing suspicious path in archive: {n}")
                return 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
    print(f"Imported {len(names)} files into {DATA}")
    print("Start the app (./run.sh or run.bat) and your profile will be there.")
    return 0


# Résumé/cover-letter output folders are deliberately NOT part of the repo snapshot: 178 folders
# and 25 MB of PDFs that regenerate per job anyway, and every commit would carry the whole lot
# again. The tracker rows in rr.db keep the history; the files themselves are disposable.
REPO_DATA = Path(__file__).resolve().parent.parent / "data"


def do_sync() -> int:
    """Copy ~/ResumeRewriter into the repo's local data/ mirror (gitignored).

    Useful as a second on-disk copy next to the code. It is NEVER committed — .gitignore and the
    pre-commit hook both block data/. To move machines, use `export` / `import` (a zip you carry).
    """
    REPO_DATA.mkdir(parents=True, exist_ok=True)
    changed, same = [], 0

    def place(src: Path, dst: Path) -> None:
        nonlocal same
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for child in sorted(src.iterdir()):
                if child.name in EXCLUDE_PARTS or child.name.endswith(".bak") \
                        or child.name == ".DS_Store":
                    continue
                place(child, dst / child.name)
            return
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            same += 1
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        changed.append(str(dst.relative_to(REPO_DATA)))

    for name in INCLUDE:
        src = DATA / name
        if src.exists():
            place(src, REPO_DATA / name)

    if changed:
        print(f"Updated {len(changed)} file(s) in local data/ (gitignored — will not be committed):")
        for c in changed[:10]:
            print("  ", c)
        if len(changed) > 10:
            print(f"   … and {len(changed) - 10} more")
    else:
        print(f"data/ is already up to date ({same} file(s) checked).")
    print(f"\nLive data stays in {DATA}. To back up for another machine:")
    print("  python scripts/backup_data.py export")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="pack ~/ResumeRewriter into a zip")
    e.add_argument("--out", type=Path, default=None)
    i = sub.add_parser("import", help="restore a zip into ~/ResumeRewriter")
    i.add_argument("zip", type=Path)
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    sub.add_parser("sync", help="copy ~/ResumeRewriter into local data/ (gitignored mirror)")
    a = ap.parse_args()
    if a.cmd == "sync":
        return do_sync()
    return do_export(a.out) if a.cmd == "export" else do_import(a.zip, a.force)


if __name__ == "__main__":
    sys.exit(main())
