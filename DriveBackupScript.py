#!/usr/bin/env python3
"""
Robust Photo Backup Script with Resume Capability

- Verifies every copy with SHA-256 (hashed during the copy, so the source is read once)
- Copies to a .part file and renames, so an interruption never leaves a truncated file
- Refuses to mass-delete if the source looks wrong (empty/unmounted/remapped drive)
- Deleted files go to a trash folder by default, not straight to oblivion
- State file is written atomically and batched, so Ctrl-C can't corrupt it
- Logs newest-first, with a size cap, and survives a hard kill
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()  # reads the .env file in the current directory

# CONFIGURATION - Edit these paths for your backup
SOURCE_DIR = os.getenv("SOURCE_DIR")                     # Directory to backup FROM
BACKUP_DIR = os.getenv("BACKUP_DIR")             # Directory to backup TO         # File to track backup progress
LOG_DIR= os.getenv("LOG_DIR")
STATE_FILE = os.path.join(LOG_DIR,"backup_state.json") 
LOG_FILE = os.path.join(LOG_DIR,"backup_log.txt")              # File to save backup logs

CHUNK_SIZE = 1024 * 1024        # 1 MiB — much faster than 8 KiB on spinning disks
STATE_SAVE_EVERY = 100          # Flush state every N processed files
MAX_COPY_ATTEMPTS = 3           # Real retries on hash mismatch
DELETE_THRESHOLD = 0.10         # Abort if >10% of the backup would be deleted...
MIN_DELETE_GUARD = 25           # ...but only once that's at least this many files
MAX_LOG_BYTES = 5 * 1024 * 1024 # Trim history beyond this
MTIME_TOLERANCE = 2.0           # FAT timestamps have 2-second granularity
TRASH_DIR_NAME = ".backup_trash"
TRASH_RETAIN_DAYS = 90          # Auto-purge trashed files older than this (0 = keep forever)
STATE_VERSION = 2


def long_path(p: Path) -> str:
    """Windows caps paths at 260 chars unless you use the \\\\?\\ prefix."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + os.path.abspath(s)
    return s


class Logger:
    """Console + file logger. Newest session ends up at the top of the log file.

    Lines are streamed to a .session sidecar file as they happen, so a hard kill
    (or a power cut) doesn't lose the log. The sidecar is merged into the main
    log on close, or on the next startup if we died before that.
    """

    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self.session_file = self.log_file.with_suffix(self.log_file.suffix + ".session")
        self._fh = None

    def open(self):
        self._recover_orphaned_session()
        self._fh = open(self.session_file, "w", encoding="utf-8")
        self.log("=" * 60)
        self.log(f"Backup started at {datetime.now():%Y-%m-%d %H:%M:%S}")
        self.log("=" * 60)

    def log(self, message: str):
        try:
            print(message)
        except (BrokenPipeError, OSError):
            pass  # stdout closed (e.g. piped into head) — keep writing to the file
        if self._fh:
            self._fh.write(message + "\n")
            self._fh.flush()

    def close(self):
        if not self._fh:
            return
        self.log("=" * 60)
        self.log(f"Backup ended at {datetime.now():%Y-%m-%d %H:%M:%S}")
        self.log("=" * 60 + "\n")
        self._fh.close()
        self._fh = None
        self._merge_session()

    def _recover_orphaned_session(self):
        """A leftover session file means the last run was killed. Don't lose it."""
        if self.session_file.exists() and self.session_file.stat().st_size > 0:
            print("Recovering log from a previous interrupted run...")
            self._merge_session()

    def _merge_session(self):
        try:
            session = self.session_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if not session.strip():
            self.session_file.unlink(missing_ok=True)
            return

        history = ""
        if self.log_file.exists():
            try:
                history = self.log_file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"Warning: could not read existing log: {e}")

        # Cap history so the file doesn't grow without bound; cut on a line boundary.
        if len(history) > MAX_LOG_BYTES:
            history = history[:MAX_LOG_BYTES].rsplit("\n", 1)[0]
            history += "\n\n[... older entries truncated ...]\n"

        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write(session)
                f.write(history)
            self.session_file.unlink(missing_ok=True)
        except OSError as e:
            print(f"Error writing log file: {e} (session kept at {self.session_file})")


class PhotoBackup:
    def __init__(self, source_dir: str, backup_dir: str,
                 state_file: str = STATE_FILE, logger: Optional[Logger] = None):
        self.source_dir = Path(source_dir).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.state_file = Path(state_file)
        self.trash_root = self.backup_dir / TRASH_DIR_NAME
        self.logger = logger
        self._dirty = 0
        self.state = self._load_state()

    # -- logging -------------------------------------------------------------

    def _log(self, message: str):
        if self.logger:
            self.logger.log(message)
        else:
            print(message)

    # -- state ---------------------------------------------------------------

    def _fresh_state(self) -> Dict:
        return {
            "version": STATE_VERSION,
            "completed_files": {},
            "last_run": None,
            "source_dir": str(self.source_dir),
            "backup_dir": str(self.backup_dir),
        }

    def _load_state(self) -> Dict:
        if not self.state_file.exists():
            return self._fresh_state()
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._log("Warning: corrupted state file, starting fresh")
            return self._fresh_state()

        # A state file from a different pair of directories is worse than no state
        # file: it would let us "skip" files that were never copied to THIS backup.
        if (state.get("source_dir") != str(self.source_dir)
                or state.get("backup_dir") != str(self.backup_dir)):
            self._log("Warning: state file was written for a different source/backup "
                      "pair. Ignoring it and rescanning.")
            return self._fresh_state()

        return self._migrate_state(state)

    def _migrate_state(self, state: Dict) -> Dict:
        """Upgrade an older state file in place rather than forcing a full re-backup.

        v1 (the original script) recorded mtime + size + backed_up per file, which is
        exactly what the skip check needs — so every already-copied file still gets
        skipped. The only thing missing is sha256, which older runs never computed.
        Those entries stay verifiable-later: --backfill-hashes fills them in, and any
        file that changes from now on records its hash on the next copy.
        """
        version = state.get("version", 1)
        if version == STATE_VERSION:
            return state
        if version > STATE_VERSION:
            self._log(f"Warning: state file is from a newer version ({version}). "
                      "Ignoring it and rescanning.")
            return self._fresh_state()

        known = len(state.get("completed_files", {}))
        state["version"] = STATE_VERSION
        state.setdefault("completed_files", {})
        state.setdefault("last_run", None)
        self._log(f"Migrated state file from v{version} to v{STATE_VERSION}: "
                  f"{known} already-backed-up file(s) carried over, no re-copy needed.")
        missing_hashes = sum(1 for i in state["completed_files"].values()
                             if not i.get("sha256"))
        if missing_hashes:
            self._log(f"  {missing_hashes} of them have no recorded checksum (the old "
                      f"script didn't save one). Run --backfill-hashes once to make "
                      f"--verify cover them.")
        self._dirty = STATE_SAVE_EVERY  # persist the upgrade promptly
        return state

    def _save_state(self, force: bool = False):
        """Atomic write, batched. Ctrl-C mid-dump can no longer corrupt the file."""
        self._dirty += 1
        if not force and self._dirty < STATE_SAVE_EVERY:
            return
        self._dirty = 0
        self.state["last_run"] = datetime.now().isoformat()
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)   # atomic on POSIX and Windows
        except OSError as e:
            self._log(f"Warning: could not save state: {e}")

    # -- hashing & copying ---------------------------------------------------

    def _hash_file(self, path: Path) -> Optional[str]:
        sha = hashlib.sha256()
        try:
            with open(long_path(path), "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    sha.update(chunk)
            return sha.hexdigest()
        except OSError as e:
            self._log(f"Error hashing {path}: {e}")
            return None

    def _copy_with_verification(self, src: Path, dst: Path) -> Optional[str]:
        """Copy src -> dst, verify, and return the SHA-256 (or None on failure).

        Hashes while copying, so the source is read once instead of twice. Writes to
        a .part file and renames, so an interrupted run never leaves a half-written
        file sitting at the real destination path.
        """
        part = dst.with_suffix(dst.suffix + ".part")
        for attempt in range(1, MAX_COPY_ATTEMPTS + 1):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                sha = hashlib.sha256()
                with open(long_path(src), "rb") as fin, open(long_path(part), "wb") as fout:
                    while chunk := fin.read(CHUNK_SIZE):
                        sha.update(chunk)
                        fout.write(chunk)
                    fout.flush()
                    os.fsync(fout.fileno())
                shutil.copystat(long_path(src), long_path(part))

                written = self._hash_file(part)
                if written == sha.hexdigest():
                    os.replace(long_path(part), long_path(dst))
                    return written

                self._log(f"  ! Hash mismatch (attempt {attempt}/{MAX_COPY_ATTEMPTS})")
            except OSError as e:
                self._log(f"  ! Copy error (attempt {attempt}/{MAX_COPY_ATTEMPTS}): {e}")
            finally:
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
        return None

    # -- scanning ------------------------------------------------------------

    def _scan(self, directory: Path, exclude: Tuple[str, ...] = ()) -> Dict[str, Path]:
        """Walk a tree, logging (not swallowing) permission errors.

        os.walk with onerror lets us see unreadable directories. rglob either raises
        or silently skips them depending on the Python version, and it follows
        directory symlinks, which can loop.
        """
        files: Dict[str, Path] = {}
        if not directory.exists():
            return files

        def on_error(err: OSError):
            self._log(f"Warning: cannot read {getattr(err, 'filename', '?')}: {err}")

        for root, dirnames, filenames in os.walk(directory, onerror=on_error,
                                                 followlinks=False):
            root_path = Path(root)
            dirnames[:] = [d for d in dirnames if d not in exclude]
            for name in filenames:
                if name.endswith(".part"):
                    continue
                full = root_path / name
                files[str(full.relative_to(directory))] = full
        return files

    def _unchanged(self, rel_path: str, src_file: Path, dst_file: Path) -> bool:
        info = self.state["completed_files"].get(rel_path)
        if not info or not dst_file.exists():
            return False
        try:
            st = src_file.stat()
        except OSError:
            return False
        return (abs(info.get("mtime", -1) - st.st_mtime) <= MTIME_TOLERANCE
                and info.get("size") == st.st_size)

    # -- deletion ------------------------------------------------------------

    def _remove(self, rel_path: str, use_trash: bool, stamp: str) -> bool:
        dst_file = self.backup_dir / rel_path
        try:
            if use_trash:
                target = self.trash_root / stamp / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(long_path(dst_file), long_path(target))
            else:
                dst_file.unlink()
            self.state["completed_files"].pop(rel_path, None)
            return True
        except OSError as e:
            self._log(f"    x Error removing {rel_path}: {e}")
            return False

    # -- main entry points ---------------------------------------------------

    def verify(self) -> int:
        """Re-hash the backup against the hashes recorded at copy time."""
        self._log(f"Verifying backup at {self.backup_dir}")
        self._log("-" * 60)
        bad = missing = checked = 0
        for rel_path, info in sorted(self.state["completed_files"].items()):
            dst = self.backup_dir / rel_path
            if not dst.exists():
                self._log(f"  MISSING: {rel_path}")
                missing += 1
                continue
            stored = info.get("sha256")
            if not stored:
                continue
            checked += 1
            if self._hash_file(dst) != stored:
                self._log(f"  CORRUPT: {rel_path}")
                bad += 1

        self._log("\n" + "=" * 60)
        self._log(f"Verified:  {checked}")
        self._log(f"Missing:   {missing}")
        self._log(f"Corrupt:   {bad}")
        self._log("=" * 60)
        if bad or missing:
            self._log("\nRe-run with --reset to force a full re-copy of the bad files.")
            return 1
        return 0

    def run_backup(self, dry_run: bool = False, verbose: bool = True,
                   no_delete: bool = False, force: bool = False,
                   use_trash: bool = True,
                   trash_retain_days: int = TRASH_RETAIN_DAYS) -> int:
        self._log(f"Starting backup from {self.source_dir} to {self.backup_dir}")
        self._log(f"Dry run: {dry_run} | Verbose: {verbose} | "
                  f"Deletions: {'off' if no_delete else ('to trash' if use_trash else 'permanent')}")
        self._log("-" * 60)

        if not self.source_dir.exists():
            self._log(f"Error: source directory does not exist: {self.source_dir}")
            return 1

        # Backing a directory up into itself would recurse forever.
        if self.backup_dir == self.source_dir or self.source_dir in self.backup_dir.parents:
            self._log("Error: backup directory is inside the source directory.")
            return 1

        if not dry_run:
            self._sweep_partials()
            self._prune_trash(trash_retain_days)

        source_files = self._scan(self.source_dir)
        backup_files = self._scan(self.backup_dir, exclude=(TRASH_DIR_NAME,))

        if not source_files:
            self._log("Error: source directory is empty. Refusing to run — if this is "
                      "really what you want, there is nothing to back up anyway.")
            return 1

        stats = {"copied": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
        total = len(source_files)

        # --- copy / update ---------------------------------------------------
        for idx, (rel_path, src_file) in enumerate(sorted(source_files.items()), 1):
            dst_file = self.backup_dir / rel_path

            if self._unchanged(rel_path, src_file, dst_file):
                if verbose:
                    self._log(f"[{idx}/{total}] Skipping (unchanged): {rel_path}")
                stats["skipped"] += 1
                continue

            is_new = not dst_file.exists()
            verb = "copy" if is_new else "update"

            if dry_run:
                self._log(f"[{idx}/{total}] Would {verb}: {rel_path}")
                stats["copied" if is_new else "updated"] += 1
                continue

            self._log(f"[{idx}/{total}] {verb.capitalize()}ing: {rel_path}")
            digest = self._copy_with_verification(src_file, dst_file)
            if digest:
                st = src_file.stat()
                self.state["completed_files"][rel_path] = {
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "sha256": digest,
                    "backed_up": datetime.now().isoformat(),
                }
                self._save_state()
                stats["copied" if is_new else "updated"] += 1
            else:
                self._log(f"  x Failed after {MAX_COPY_ATTEMPTS} attempts")
                stats["errors"] += 1

        if not dry_run:
            self._save_state(force=True)

        # --- deletions -------------------------------------------------------
        to_delete = sorted(set(backup_files) - set(source_files))

        if to_delete and no_delete:
            self._log(f"\n{len(to_delete)} extra file(s) in backup, left alone (--no-delete)")
        elif to_delete:
            ratio = len(to_delete) / len(backup_files)
            tripped = ratio > DELETE_THRESHOLD and len(to_delete) >= MIN_DELETE_GUARD
            if tripped and not force:
                self._log(f"\nABORT: {len(to_delete)} of {len(backup_files)} backed-up "
                          f"files ({ratio:.0%}) would be deleted.")
                self._log("That usually means the source drive is unmounted, remapped, "
                          "or the wrong volume.")
                self._log("Files already copied this run are safe. Check the source, then "
                          "re-run with --force if the deletions are genuinely intended.")
                self._summary(stats, dry_run)
                return 1

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log(f"\n{len(to_delete)} file(s) to remove from backup:")
            for rel_path in to_delete:
                self._log(f"  - {rel_path}")
                if dry_run:
                    stats["deleted"] += 1
                elif self._remove(rel_path, use_trash, stamp):
                    stats["deleted"] += 1
                else:
                    stats["errors"] += 1

            if not dry_run and use_trash:
                self._log(f"\nRemoved files are recoverable from: "
                          f"{self.trash_root / stamp}")

        if not dry_run:
            self._cleanup_empty_dirs()
            self._save_state(force=True)

        self._summary(stats, dry_run)
        return 0 if stats["errors"] == 0 else 1

    def _summary(self, stats: Dict[str, int], dry_run: bool):
        self._log("\n" + "=" * 60)
        self._log("BACKUP SUMMARY")
        self._log("=" * 60)
        for label, key in (("Files copied:", "copied"), ("Files updated:", "updated"),
                           ("Files deleted:", "deleted"), ("Files skipped:", "skipped"),
                           ("Errors:", "errors")):
            self._log(f"{label:<16} {stats[key]}")
        self._log("=" * 60)
        if dry_run:
            self._log("\nThis was a DRY RUN - no changes were made")

    def _prune_trash(self, retain_days: int):
        """Permanently remove trashed batches older than retain_days."""
        if retain_days <= 0 or not self.trash_root.exists():
            return
        cutoff = datetime.now() - timedelta(days=retain_days)
        for batch in sorted(self.trash_root.iterdir()):
            if not batch.is_dir():
                continue
            try:
                stamp = datetime.strptime(batch.name, "%Y%m%d_%H%M%S")
            except ValueError:
                # Not one of ours (or renamed) — fall back to the folder's mtime.
                stamp = datetime.fromtimestamp(batch.stat().st_mtime)
            if stamp >= cutoff:
                continue
            count = sum(1 for _ in batch.rglob("*") if _.is_file())
            try:
                shutil.rmtree(batch)
                age = (datetime.now() - stamp).days
                self._log(f"Purged trash from {batch.name} ({count} file(s), "
                          f"{age} days old, past the {retain_days}-day limit)")
            except OSError as e:
                self._log(f"Could not purge trash {batch.name}: {e}")

    def backfill_hashes(self) -> int:
        """One-time pass for backups made by the old script, which saved no checksums.

        Hashes the source and the backup copy and compares. Matching files get their
        checksum recorded, so --verify covers them from now on. Mismatched files are
        dropped from the state so the next run re-copies them. Nothing is copied here.
        """
        entries = {k: v for k, v in self.state["completed_files"].items()
                   if not v.get("sha256")}
        if not entries:
            self._log("Every tracked file already has a checksum. Nothing to backfill.")
            return 0

        self._log(f"Backfilling checksums for {len(entries)} file(s). "
                  f"This reads both drives once and copies nothing.")
        self._log("-" * 60)
        filled = mismatched = missing = 0

        for idx, rel_path in enumerate(sorted(entries), 1):
            src, dst = self.source_dir / rel_path, self.backup_dir / rel_path
            if not src.exists() or not dst.exists():
                self._log(f"[{idx}/{len(entries)}] Missing, will be handled on the next "
                          f"run: {rel_path}")
                self.state["completed_files"].pop(rel_path, None)
                missing += 1
                continue

            src_hash, dst_hash = self._hash_file(src), self._hash_file(dst)
            if src_hash and src_hash == dst_hash:
                self.state["completed_files"][rel_path]["sha256"] = src_hash
                filled += 1
            else:
                self._log(f"[{idx}/{len(entries)}] MISMATCH, queued for re-copy: {rel_path}")
                self.state["completed_files"].pop(rel_path, None)
                mismatched += 1
            self._save_state()

        self._save_state(force=True)
        self._log("\n" + "=" * 60)
        self._log(f"Checksums recorded:  {filled}")
        self._log(f"Mismatched:          {mismatched}")
        self._log(f"Missing:             {missing}")
        self._log("=" * 60)
        if mismatched or missing:
            self._log("\nRun a normal backup to re-copy the files listed above.")
        return 0

    def _sweep_partials(self):
        """A SIGKILL or power cut mid-copy leaves a .part file. Nothing reads them,
        but they waste space, so clear them out at the start of each run."""
        if not self.backup_dir.exists():
            return
        for stale in self.backup_dir.rglob("*.part"):
            try:
                stale.unlink()
                self._log(f"Cleaned up partial file from an interrupted run: "
                          f"{stale.relative_to(self.backup_dir)}")
            except OSError:
                pass

    def _cleanup_empty_dirs(self):
        if not self.backup_dir.exists():
            return
        for dirpath in sorted(self.backup_dir.rglob("*"), reverse=True):
            if not dirpath.is_dir() or dirpath == self.trash_root:
                continue
            if self.trash_root in dirpath.parents:
                continue
            try:
                if not any(dirpath.iterdir()):
                    dirpath.rmdir()
                    self._log(f"Removed empty directory: {dirpath.relative_to(self.backup_dir)}")
            except OSError as e:
                self._log(f"Could not remove {dirpath}: {e}")

    def reset_state(self):
        self.state = self._fresh_state()
        self._save_state(force=True)
        self._log("Backup state reset successfully")


def main():
    parser = argparse.ArgumentParser(
        description="Backup photos with integrity checking and resume capability")
    parser.add_argument("source", nargs="?", default=SOURCE_DIR,
                        help=f"Source directory (default: {SOURCE_DIR})")
    parser.add_argument("backup", nargs="?", default=BACKUP_DIR,
                        help=f"Backup destination (default: {BACKUP_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    parser.add_argument("--reset", action="store_true",
                        help="Reset backup state (force full re-backup)")
    parser.add_argument("--verify", action="store_true",
                        help="Re-hash the backup against recorded hashes and exit")
    parser.add_argument("--quiet", action="store_true",
                        help="Only log changes and summary (skip unchanged files)")
    parser.add_argument("--no-delete", action="store_true",
                        help="Never remove files from the backup")
    parser.add_argument("--permanent-delete", action="store_true",
                        help="Delete outright instead of moving to the trash folder")
    parser.add_argument("--force", action="store_true",
                        help="Allow deletions past the safety threshold")
    parser.add_argument("--trash-retain-days", type=int, default=TRASH_RETAIN_DAYS,
                        help=f"Auto-purge trashed files older than N days "
                             f"(default: {TRASH_RETAIN_DAYS}, 0 = keep forever)")
    parser.add_argument("--backfill-hashes", action="store_true",
                        help="Record checksums for files backed up by the old script")
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--log-file", default=LOG_FILE)
    args = parser.parse_args()

    logger = Logger(args.log_file)
    exit_code = 0
    try:
        logger.open()
        logger.log(f"Python: {sys.version.split()[0]} on {sys.platform}")
        logger.log(f"Source: {args.source}")
        logger.log(f"Backup: {args.backup}")
        logger.log("")

        backup = PhotoBackup(args.source, args.backup, args.state_file, logger)

        if args.reset:
            backup.reset_state()
            logger.log("State reset. Run again without --reset to perform the backup.")
        elif args.verify:
            exit_code = backup.verify()
        elif args.backfill_hashes:
            exit_code = backup.backfill_hashes()
        else:
            exit_code = backup.run_backup(
                dry_run=args.dry_run,
                verbose=not args.quiet,
                no_delete=args.no_delete,
                force=args.force,
                use_trash=not args.permanent_delete,
                trash_retain_days=args.trash_retain_days,
            )
    except KeyboardInterrupt:
        logger.log("\n\nInterrupted. Progress has been saved — run again to resume.")
        try:
            backup._save_state(force=True)
        except Exception:
            pass
        exit_code = 130
    except Exception as e:
        logger.log(f"\nFatal error: {e}")
        logger.log(traceback.format_exc())
        logger.log("Progress has been saved. Run the script again to resume.")
        exit_code = 1
    finally:
        logger.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()