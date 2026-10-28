# Bitrix MySQL backup for Duplicati

[English README](README.md) · [Русский README](README.ru.md)

`bitrix-duplicati-backup` is a small backup helper for **self-hosted Bitrix24 / BitrixVM**. Before a Duplicati backup, it dumps InnoDB data from a **single consistent MySQL transaction** and writes **one importable SQL file per table**. Keeping tables separate can improve Duplicati deduplication and makes selective restores easier.

This is a **Bitrix-specific** tool, not a general-purpose MySQL backup framework. Its source may be adapted as an example; configurable databases, credentials, backup services and table rules would require further work or a separate fork.

## Repository layout

| File / directory | Role |
| --- | --- |
| `bitrix_mysql_backup.sh` | Duplicati hook: Backup-only operation guard; start/end logging; real-time `tee` |
| `bitrix_mysql_backup.py` | MySQL snapshot, SQL stream splitter, validation and publishing |
| `install.sh` | One interactive install/update command using `curl`, with no Git required |
| `README.md`, `README.ru.md` | English and Russian documentation |
| `.gitignore` | Excludes SQL, logs and temporary files |
| `backups/` | Locally generated database dumps, excluded from Git |

Installation layout (**choose any directory**):

```text
<project-directory>/
├── install.sh
├── bitrix_mysql_backup.sh
├── bitrix_mysql_backup.py
├── bitrix_mysql_backup.log       # created only by Backup through Bash
├── README.md
├── README.ru.md
├── .gitignore
└── backups/
    └── bitrix_88.sql/           # DB name comes from Bitrix dbconn.php
        ├── b_user.sql
        └── ...
```

## Requirements

- Linux with Bash, Python 3, `mysql`, `mysqldump`; `curl`, `tar`, `sha256sum`, and standard shell utilities for the installer.
- Read access to `/home/bitrix/www/bitrix/php_interface/dbconn.php` and connectivity to `/var/lib/mysqld/mysqld.sock`.
- **InnoDB** for all base tables; a different storage engine causes the dump to fail safely.
- The account running the hook must be able to read Bitrix configuration and write the log and SQL files. The installer creates private `backups/` permissions (`700`); verify service-user access if Duplicati runs as a different user.
- Disk space for old and new SQL snapshots simultaneously while creating a backup.
- A **public GitHub repository** for the unauthenticated `curl` installer. Private repositories are not currently supported.

## First installation and updates

Run the installer manually, **not as part of the Duplicati backup job**. By default, it installs into **the directory containing `install.sh`**, regardless of the current working directory. Choose any suitable path, such as `/opt/bitrix_mysql_backup` or `/usr/scripts/bitrix_mysql_backup`.

For example, create a directory and download the installer into it:

```bash
mkdir -p /opt/bitrix_mysql_backup
cd /opt/bitrix_mysql_backup
curl -fL --output install.sh \
  https://raw.githubusercontent.com/avmaksimov/bitrix-duplicati-backup/main/install.sh
bash install.sh
```

For future updates, run the same installer:

```bash
bash /opt/bitrix_mysql_backup/install.sh
```

`/opt/bitrix_mysql_backup` is **only an example**, not a required location. If you already extracted the project into the chosen directory, you may run it there; the installer also ensures `.sh` executable permissions if files are already identical.

Both commands fetch the `main` branch archive using `curl` and compare files by **SHA-256**. New files (`NEW`) are installed automatically without prompting. Existing changed files (`DIFF`) prompt `Replace? [y/N]` (Enter skips). Identical files, including a previously downloaded `install.sh`, are not prompted or replaced. If `install.sh` itself differs from the GitHub version, the installer asks before replacing it. Different checksums indicate **different content**, not necessarily that the remote file is newer.

All accepted files are staged next to their targets as `.new`, verified using SHA-256, then replaced individually with `mv`. Shell scripts receive mode `755`, all other tracked files `644`. SQL backups and logs are never overwritten by the installer. There are no copies of old scripts and no automatic rollback; **the whole group of files is not replaced transactionally**. Update only when no Duplicati job is running.

Git is **not required** and the installer never installs operating-system packages. Override the repository, branch or destination when needed:

```bash
BITRIX_BACKUP_GITHUB_REPO=OWNER/bitrix-duplicati-backup \
BITRIX_BACKUP_BRANCH=main \
BITRIX_BACKUP_INSTALL_DIR=/another/directory \
  bash install.sh
```

For **local/offline testing**, point to an unpacked source directory:

```bash
BITRIX_BACKUP_SOURCE_DIR=/tmp/bitrix-duplicati-backup bash /tmp/bitrix-duplicati-backup/install.sh
```

This uses the same comparison/staging/replacement procedure without downloading the archive.

## Configure Duplicati

Use these job advanced options:

```text
--run-script-before-required = /opt/bitrix_mysql_backup/bitrix_mysql_backup.sh
--run-script-timeout = 1h
```

Add `<project-directory>/backups/` (or its `<database>.sql` child) to the **backup source paths**. The example above uses `/opt/bitrix_mysql_backup`; replace it with your own absolute path. The installer sets `.sh` files executable automatically. A required hook prevents Duplicati from backing up an invalid or stale SQL set if preparation fails.

The wrapper skips every operation other than `Backup`, including `Restore`. Code updates are never performed while preparing a backup.

## Manual checks

```bash
cd /opt/bitrix_mysql_backup  # change to your chosen directory

# Does not dump anything: this is not Backup
DUPLICATI__OPERATIONNAME=Restore ./bitrix_mysql_backup.sh

# Runs the real dump and writes the log
DUPLICATI__OPERATIONNAME=Backup ./bitrix_mysql_backup.sh

echo "Exit code: $?"
tail -n 40 ./bitrix_mysql_backup.log
```

To create a separate test set without overwriting active SQL dumps:

```bash
python3 ./bitrix_mysql_backup.py \
  --output-root /tmp/bitrix-backup-test
```

Direct Python execution prints to the console only and creates **no persistent log**. The Bash hook uses `tee` to display and log Python output in real time, truncating the previous log at the start of each actual Backup. Python's exit code is forwarded to Duplicati, rather than `tee`'s.

## Dump algorithm

1. Read the DB name, user, password and encoding from Bitrix `dbconn.php`. Credentials are never printed. A private temporary MySQL options file (`*.cnf`, mode `600`) is removed in `finally` on normal or handled exits.
2. Enumerate MySQL objects and require InnoDB for all base tables.
3. Run **one** `mysqldump --single-transaction --quick` for the full-data tables, streaming SQL directly into individual files without a large intermediate whole-database dump.
4. Save schema only for `b_stat*`, most `b_search_*`, and `b_ticket_search`. Preserve data for `b_search_custom_rank` and `b_search_phrase`.
5. Write an individual `.sql` for each table, and optional `__objects.sql` / `__views.sql` for other database objects.
6. Check expected table names, nonempty SQL files and MySQL exit codes.
7. Create the new snapshot under a staging directory in `backups/`; after validation, move the old set aside briefly, replace it with the new set, and remove the previous set. A failure before publishing leaves the previous set intact.

**Limitations:** the transaction provides consistency for participating InnoDB table data provided no concurrent schema changes (`ALTER`/`DROP`/`TRUNCATE`, etc.). The separately produced schema-only tables are not from that same timestamp. Search indexes and statistics are intentionally omitted. Validation checks that files exist and have plausible structure; **it does not test SQL restore**.

A sudden machine crash or `SIGKILL` can leave temporary `.cnf` files or staging directories; check for stale artifacts when recovering from interrupted runs. Do not archive the whole project directory with Duplicati unless logs and temporary files are explicitly excluded.

## Restoration

Individual `.sql` files are intended for import into a chosen MySQL database. Full restoration requires consideration of table dependencies, triggers, `__views.sql` / `__objects.sql`, and consistency with uploaded Bitrix files outside MySQL. Test restoration independently before relying on the backups in production.

## License

Before public distribution, choose and add a `LICENSE` (MIT is one possible choice if reuse and forks are desired). A public repository without an explicit license does not automatically grant redistribution rights.
