# Updates and recovery

Stop the MemCal UI, agent sessions and background writers before updating. Older
running versions cannot participate in the new maintenance lock. Restart hosts
afterward so they load the new code and integration assets.

```sh
memcal update
```

The default target is the newest non-yanked, final `x.y.z` release on PyPI, not
GitHub's development branch. Dependencies and imports are checked in a temporary
staging directory first. Package installations install that exact release without
bypassing externally-managed Python protections. Git installations fetch its
matching `vVERSION` tag, require a clean tracked tree and a fast-forward ancestry,
and switch to the release in detached-HEAD mode. Development worktrees are refused.
Use `memcal update --dev` only when you explicitly want development source.

Before installation, MemCal takes maintenance, collector and Dream locks and
writes a private snapshot under `MEMCAL_HOME/backups/`. SQLite backup captures
committed WAL contents; other store files and configuration are copied. Snapshots
include checksums, the previous version, Python, dependency inventory and source
commit where applicable. Symlinks are refused rather than followed. Snapshots are
not encrypted: they contain private messages and credentials just like the store.
Keep them private, account for their disk usage, and remove obsolete snapshots
manually after checking that the update is healthy.

After installation, MemCal migrates the store, refreshes the saved brief and
managed Hermes/OpenClaw integration copies, and refreshes an installed macOS
scheduler. Modified integration files are preserved and reported as an error;
resolve those local edits before refreshing again. A partial refresh is not a
successful update.

## Recover without overwriting data

An update is **not an atomic rollback of Python, dependencies, host configuration
and data**. If installation or refresh fails, the snapshot remains available.
Do not run older code against a store that newer code may have migrated.

```sh
memcal update --restore-backup /path/to/backups/SNAPSHOT --restore-to /path/to/recovered-store
```

The recovery destination must not exist. Checksums and the complete inventory
are validated before any copy; the original store is untouched. Recovery prints
the previous version. Install that version in a separate Python environment and
point it at the recovered store, for example:

```sh
python3 -m venv /path/to/recovery-env
/path/to/recovery-env/bin/python -m pip install 'memcal==PREVIOUS_VERSION'
MEMCAL_HOME=/path/to/recovered-store /path/to/recovery-env/bin/memcal doctor
```

Use the version printed by recovery, not the literal `PREVIOUS_VERSION` placeholder.
For an unreleased development installation, use the recorded source commit and
installation inventory instead. Verify data and configuration before switching
hosts or restarting scheduled work. Integration links and scheduler settings may
need to be refreshed for the recovered installation; copying data alone does not
revert those external settings. Keep the original and snapshot until recovery is
verified. If the installed CLI no longer imports, run the recovery command from
a separate installation containing this recovery utility.
