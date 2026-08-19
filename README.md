# Drive Backup

A robust one-way backup script that mirrors a source directory into a backup
directory, verifying every copy with SHA-256 and resuming cleanly after an
interruption. Written for photo libraries but works on any file tree.

It's designed to survive the things that break naive backup scripts — a Ctrl-C
mid-copy, a power cut, an unmounted source drive, or a corrupted state file — and
to never mass-delete your backup because the source happened to disappear.

---

## Design principles

- **Every copy is verified.** Files are hashed with SHA-256 *while* being copied
  (so the source is read once), and the written copy is re-hashed to confirm it
  matches before the copy is accepted.
- **Interruptions never corrupt anything.** Copies are written to a `.part` file
  and atomically renamed; the state file is written atomically and in batches; the
  log streams to a sidecar that's recovered on the next run.
- **Deletions are guarded.** If more than 10% of the backup (and at least 25
  files) would be deleted, the run aborts — that usually means the source drive is
  unmounted or remapped, not that you deleted thousands of files.
- **Deletions are recoverable.** Removed files go to a trash folder with a
  retention window by default, not straight to oblivion.
- **Resumable.** A state file tracks what's already been copied, so re-running
  after a stop picks up where it left off instead of re-hashing everything.

---

## Features

- **Incremental** — skips files whose modification time and size are unchanged
  since the last successful copy.
- **Integrity verification** — `--verify` re-hashes the backup against the
  checksums recorded at copy time and reports missing or corrupt files.
- **Resume after interruption** — Ctrl-C, a crash, or a power cut leaves progress
  saved; just run again.
- **One-way mirror** — files removed from the source are removed from the backup
  (to trash by default), keeping the two in sync.
- **Mass-delete safety net** — the threshold guard prevents wiping a backup when
  the source looks wrong; override with `--force` when the deletions are genuine.
- **Trash with retention** — deleted files land in a timestamped batch under
  `.backup_trash/` and are auto-purged after the retention window (90 days by
  default).
- **Crash-safe logging** — newest session first, size-capped, streamed to a
  sidecar so a hard kill never loses the log.
- **State migration & hash backfill** — upgrades state files from an older version
  of the script, and `--backfill-hashes` records checksums for files an older
  version copied without them.
- **Self-protection** — refuses to back a directory up into itself, and refuses to
  run against an empty source.
- **Windows-aware** — handles the 260-character path limit via the `\\?\` prefix.

---

## Requirements

- Python 3.8+ (uses the walrus operator and `pathlib`)
- [`python-dotenv`](https://pypi.org/project/python-dotenv/)

```bash
pip install python-dotenv
```

Everything else is from the Python standard library.

---

## Configuration

Settings are read from a `.env` file in the script's directory. The source and
backup paths can also be passed as positional command-line arguments.

```env
SOURCE_DIR=/path/to/photos          # directory to back up FROM
BACKUP_DIR=/path/to/backup/photos   # directory to back up TO
LOG_DIR=/path/to/logs               # where the state file and log live
```

| Variable | Required | Description |
|---|---|---|
| `SOURCE_DIR` | Yes* | Directory to back up from. |
| `BACKUP_DIR` | Yes* | Directory to back up to. |
| `LOG_DIR` | Yes | Directory for `backup_state.json` and `backup_log.txt`. |

\* `SOURCE_DIR` / `BACKUP_DIR` can be supplied on the command line instead of in
`.env`. `LOG_DIR` has no default — set it, or the state and log paths can't be
built.

Derived files (all placed in `LOG_DIR`):

- `backup_state.json` — resume/progress state.
- `backup_log.txt` — the run log.
- `backup_log.txt.session` — the in-progress sidecar (merged into the log on
  close, or recovered on the next start).

---

## Usage

```bash
# Preview everything without making changes
python DriveBackupScript.py --dry-run

# Run the backup (uses .env paths, or pass them explicitly)
python DriveBackupScript.py
python DriveBackupScript.py /path/to/source /path/to/backup

# Verify the backup against recorded checksums
python DriveBackupScript.py --verify

# Record checksums for files copied by an older version of the script
python DriveBackupScript.py --backfill-hashes
```

### Command-line options

| Flag | Description |
|---|---|
| `source` / `backup` (positional) | Override the `.env` source and backup paths. |
| `--dry-run` | Show what would be copied, updated, or deleted without changing anything. |
| `--reset` | Reset the state file, forcing a full re-backup on the next run. |
| `--verify` | Re-hash the backup against recorded hashes and exit. |
| `--quiet` | Log only changes and the summary, skipping unchanged-file lines. |
| `--no-delete` | Never remove files from the backup, even if gone from the source. |
| `--permanent-delete` | Delete outright instead of moving to the trash folder. |
| `--force` | Allow deletions past the safety threshold. |
| `--trash-retain-days N` | Auto-purge trashed files older than N days (`0` = keep forever). |
| `--backfill-hashes` | Record checksums for files backed up by the old script. |
| `--state-file PATH` | Override the state file location. |
| `--log-file PATH` | Override the log file location. |

---

## How a run works

1. **Sweep & prune** — leftover `.part` files from an interrupted run are cleared,
   and expired trash batches are purged.
2. **Scan** — the source and backup trees are walked (the trash folder is
   excluded).
3. **Copy / update** — for each source file, if its mtime + size match the recorded
   state it's skipped; otherwise it's copied with verification (up to 3 attempts on
   a hash mismatch) and its checksum is recorded.
4. **Delete** — files present in the backup but no longer in the source are
   removed, subject to the safety guard and sent to trash unless
   `--permanent-delete` is set.
5. **Tidy up** — empty directories are removed and the final state is saved.

The run exits `0` on success and `1` if any errors occurred (or the mass-delete
guard tripped); an interruption exits `130` with progress saved.

---

## The mass-delete safety guard

Before deleting anything, the script checks how much of the backup would be
removed. If that's **more than 10% of the backup AND at least 25 files**, it
aborts with a warning rather than proceeding — the usual cause is a source drive
that's unmounted, remapped, or the wrong volume entirely.

Files already copied that run stay safe. Once you've confirmed the source is
correct, re-run with `--force` to allow the deletions.

---

## Verification & recovery

- **`--verify`** walks every tracked file, re-hashes the backup copy, and reports
  anything missing or corrupt. Follow up with `--reset` to force a re-copy of the
  affected files.
- **`--backfill-hashes`** is a one-time pass for backups made by an older version
  that didn't record checksums. It hashes source and backup, records the checksum
  where they match, and drops mismatched entries so the next run re-copies them.
  It copies nothing itself.
- **Trash recovery** — deleted files sit in `BACKUP_DIR/.backup_trash/<timestamp>/`
  until purged. Copy them back out manually if needed.

---

## Notes & caveats

- **This is a mirror, not an archive.** Deleting a file from the source will
  remove it from the backup (to trash) on the next run. Use `--no-delete` if you
  want a purely additive backup.
- **The state file is tied to a specific source/backup pair.** Point it at a
  different pair and it's ignored and rescanned, so it can't wrongly "skip" files
  never copied to that destination.
- **The backup can't live inside the source.** The script refuses this to avoid
  infinite recursion.
- **An empty source aborts the run** — a safety measure against an unmounted drive
  presenting as empty.
- **`--permanent-delete` skips the trash.** Deleted files are gone immediately with
  no retention window; use with care.

---
