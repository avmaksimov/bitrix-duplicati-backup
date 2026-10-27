#!/usr/bin/env python3
"""Consistent Bitrix InnoDB SQL dump: one importable SQL file per table.

Runs directly: python3 bitrix_mysql_backup.py [--output-root DIRECTORY]
Duplicati calls it through bitrix_mysql_backup.sh on operation Backup.

Requires local mysql/mysqldump clients, Python 3, and Bitrix dbconn.php.
"""

import argparse
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DBCONN = "/home/bitrix/www/bitrix/php_interface/dbconn.php"
DEFAULT_SOCKET = "/var/lib/mysqld/mysqld.sock"
ERROR_EXIT = 10

# Same section markers as in the previously tested Bash/Python implementation.
TABLE_RE = re.compile(rb'^-- Table structure for table `((?:``|[^`])+)`\s*$')
VIEW_RE = re.compile(rb'^-- (?:Temporary|Final) view structure for view `((?:``|[^`])+)`\s*$')
OBJECT_RE = re.compile(rb'^-- Dumping (?:routines|events) for database\b')
FOOTER_RE = re.compile(rb'^/\*![0-9]{5} SET TIME_ZONE=@OLD_TIME_ZONE\b')
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_]+$')


class BackupError(Exception):
    pass


class Logger:
    """Python writes to the console; the Bash wrapper captures it in a log."""

    def log(self, message):
        print('{} {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message), flush=True)


def parse_php_dbconn(filename):
    try:
        source = filename.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        source = filename.read_text(encoding='latin1')
    settings = {}
    for name in ('DBLogin', 'DBPassword', 'DBName'):
        match = re.search(r'^\s*\$' + name + r'\s*=\s*"((?:\\.|[^"\\])*)"\s*;', source, re.M)
        if match is None:
            raise BackupError('Missing double-quoted ${} in {}'.format(name, filename))
        # Handle PHP double-quote escapes for ", \\ and \$ without evaluating PHP code.
        settings[name] = re.sub(r'\\(["\\$])', lambda m: m.group(1), match.group(1))
    if not settings['DBLogin'] or not settings['DBName']:
        raise BackupError('Empty database username or name')
    if not SAFE_NAME_RE.fullmatch(settings['DBName']):
        raise BackupError('Database name is not safe for a directory name')
    utf = bool(re.search(r'\bdefine\s*\(\s*[\'\"]BX_UTF[\'\"]\s*,\s*true\b', source, re.I))
    return settings['DBLogin'], settings['DBPassword'], settings['DBName'], ('utf8mb4' if utf else 'cp1251')


def quote_option(value):
    # mysql option-file quoted values, written into a mode-0600 file.
    return '"{}"'.format(value.replace('\\', '\\\\').replace('"', '\\"')
                       .replace('\n', '\\n').replace('\r', '\\r'))


def create_credentials(root, username, password, socket):
    fd, path = tempfile.mkstemp(prefix='.bitrix_mysql_db.', suffix='.cnf', dir=str(root))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('[client]\n')
            stream.write('user={}\n'.format(quote_option(username)))
            stream.write('password={}\n'.format(quote_option(password)))
            stream.write('socket={}\n'.format(quote_option(socket)))
        os.chmod(path, 0o600)
        return Path(path)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def run_checked(command, *, capture=False):
    """Run a MySQL client, reporting errors without leaking credentials."""
    if capture:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            print(result.stderr.decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
            raise BackupError('Command failed with exit code {}: {}'.format(result.returncode, command[0]))
        return result.stdout
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=None)
    if result.returncode:
        raise BackupError('Command failed with exit code {}: {}'.format(result.returncode, command[0]))
    return b''


def list_objects(config, database, charset, log):
    query = ("SELECT TABLE_NAME, TABLE_TYPE, COALESCE(ENGINE, '') "
             "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME")
    output = run_checked([
        'mysql', '--defaults-file=' + str(config), '--default-character-set=' + charset,
        '--batch', '--raw', '--skip-column-names', '--database=' + database,
        '--execute=' + query
    ], capture=True)
    full, schema_only, views = [], [], []
    for raw in output.decode('utf-8').splitlines():
        parts = raw.split('\t')
        if len(parts) != 3:
            raise BackupError('Unexpected response format from mysql table listing')
        table, kind, engine = parts
        if not SAFE_NAME_RE.fullmatch(table) or table in ('__views', '__objects'):
            raise BackupError('Unsupported table/view name: ' + table)
        if kind == 'VIEW':
            views.append(table)
            continue
        if kind != 'BASE TABLE' or engine != 'InnoDB':
            raise BackupError('Non-InnoDB or unsupported table: {} ({} / {})'.format(table, kind, engine))
        if (table.startswith('b_stat') or
                (table.startswith('b_search_') and table not in ('b_search_custom_rank', 'b_search_phrase')) or
                table == 'b_ticket_search'):
            schema_only.append(table)
        else:
            full.append(table)
    if not full and not schema_only:
        raise BackupError('No base tables found')
    log.log('Tables: full={}, schema-only={}, views={}'.format(len(full), len(schema_only), len(views)))
    return full, schema_only


def split_stream(stream, stage, expected):
    """Split mysqldump's table sections; retain header/trailer in every file.

    This intentionally preserves the tested marker-based algorithm. A missing
    footer or table section is a fatal error; no incomplete set is published.
    """
    expected = set(expected)
    seen = set()
    created = set()
    header = bytearray()
    footer = bytearray()
    current = None
    current_file = None
    in_footer = False

    def switch(name):
        nonlocal current, current_file
        if current_file is not None:
            current_file.close()
            current_file = None
        destination = stage / name
        if name not in created:
            current_file = destination.open('wb')
            current_file.write(header)
            created.add(name)
        else:
            current_file = destination.open('ab')
        current = name

    try:
        for line in stream:
            if in_footer:
                footer.extend(line)
                if len(footer) > 65536:
                    raise BackupError('SQL footer unexpectedly large')
                continue
            if FOOTER_RE.match(line):
                in_footer = True
                footer.extend(line)
                continue
            table_match = TABLE_RE.match(line)
            if table_match:
                name = table_match.group(1).replace(b'``', b'`').decode('utf-8')
                if name not in expected:
                    raise BackupError('Unexpected table section: ' + name)
                if name in seen:
                    raise BackupError('Duplicate table section: ' + name)
                seen.add(name)
                switch(name + '.sql')
            elif VIEW_RE.match(line):
                switch('__views.sql')
            elif OBJECT_RE.match(line):
                switch('__objects.sql')
            if current_file is None:
                header.extend(line)
                if len(header) > 1024 * 1024:
                    raise BackupError('SQL header unexpectedly large')
            else:
                current_file.write(line)
    finally:
        if current_file is not None:
            current_file.close()

    if not in_footer:
        raise BackupError('Cannot find SQL footer: incomplete or unsupported dump format')
    if seen != expected:
        missing = sorted(expected - seen)
        raise BackupError('Missing table sections ({}): {}'.format(len(missing), ', '.join(missing[:10])))
    for name in created:
        with (stage / name).open('ab') as handle:
            handle.write(footer)
            handle.flush()
            os.fsync(handle.fileno())
    return len(created), len(seen)


def dump_consistent(config, charset, database, full, schema_only, stage, log):
    base = ['mysqldump', '--defaults-file=' + str(config),
            '--default-character-set=' + charset, '--no-tablespaces',
            '--add-drop-table', '--triggers', '--skip-add-locks', '--skip-dump-date']
    command = base + [
        '--single-transaction', '--quick', '--skip-lock-tables',
        '--extended-insert', '--no-autocommit', '--routines', '--events'
    ]
    command += ['--ignore-table={}.{}'.format(database, name) for name in schema_only]
    command.append(database)
    log.log('Creating consistent InnoDB snapshot...')
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None)
    try:
        sql_files, table_files = split_stream(proc.stdout, stage, full)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        raise BackupError('mysqldump failed with exit code {}'.format(rc))
    log.log('Split dump into {} SQL files ({} full tables)'.format(sql_files, table_files))
    for table in schema_only:
        destination = stage / (table + '.sql')
        command = base + [
            '--no-data', '--skip-routines', '--skip-events',
            '--result-file=' + str(destination), database, table
        ]
        run_checked(command)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BackupError('Empty schema-only SQL file: ' + table)


def validate(stage, full, schema_only):
    for table in full + schema_only:
        file = stage / (table + '.sql')
        if not file.is_file() or file.stat().st_size == 0:
            raise BackupError('Missing or empty SQL dump: ' + table)
    return len(full) + len(schema_only)


def publish(stage, destination, root):
    previous = root / ('.{}.previous.{}'.format(destination.name, os.getpid()))
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise BackupError('Unsafe destination (not a real directory): {}'.format(destination))
    if previous.exists() or previous.is_symlink():
        raise BackupError('Unexpected old snapshot directory: {}'.format(previous))
    moved_old = False
    try:
        if destination.exists():
            destination.rename(previous)
            moved_old = True
        stage.rename(destination)
    except BaseException as exc:
        # Reconstruct state from filesystem in case a signal interrupted a rename.
        if not destination.exists() and previous.exists():
            try:
                previous.rename(destination)
            except OSError as rollback_error:
                raise BackupError('CRITICAL: rollback failed; old dump remains at {}: {}'.format(
                    previous, rollback_error)) from exc
        raise BackupError('Failed to publish SQL snapshot: {}'.format(exc)) from exc
    # Failure to remove the old directory is an error (Duplicati must not back up it).
    if moved_old:
        try:
            shutil.rmtree(previous)
        except Exception as exc:
            raise BackupError('New dump published, but cannot remove previous directory {}: {}'.format(previous, exc)) from exc


def run(args):
    old_umask = os.umask(0o077)
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    log = Logger()
    stage = None
    config = None
    try:
        for binary in ('mysql', 'mysqldump'):
            if shutil.which(binary) is None:
                raise BackupError('Required executable not found: ' + binary)
        if not Path(args.dbconn).is_file():
            raise BackupError('Cannot read Bitrix DB config: ' + args.dbconn)
        username, password, database, charset = parse_php_dbconn(Path(args.dbconn))
        config = create_credentials(output_root, username, password, args.socket)
        log.log('Database: {}; charset: {}; output root: {}'.format(database, charset, output_root))
        full, schema_only = list_objects(config, database, charset, log)
        destination = output_root / (database + '.sql')
        stage = Path(tempfile.mkdtemp(prefix='.' + database + '.sql.tmp.', dir=str(output_root)))
        dump_consistent(config, charset, database, full, schema_only, stage, log)
        count = validate(stage, full, schema_only)
        publish(stage, destination, output_root)
        stage = None
        log.log('Done. Validated SQL tables: {}'.format(count))
        return 0
    except (OSError, BackupError, UnicodeError, KeyboardInterrupt) as exc:
        log.log('ERROR: {}'.format(exc))
        log.log('FAILED: existing SQL directory preserved unless the publication phase had a filesystem failure')
        return ERROR_EXIT
    finally:
        if config is not None:
            try:
                config.unlink()
            except OSError:
                pass
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        os.umask(old_umask)


def interrupted(signum, frame):
    raise BackupError('Interrupted by signal {}'.format(signum))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', default=str(SCRIPT_DIR / 'backups'),
                        help='Parent directory for <database>.sql; use a separate directory for testing')
    parser.add_argument('--dbconn', default=os.environ.get('DBCONN_FILE', DEFAULT_DBCONN))
    parser.add_argument('--socket', default=os.environ.get('DB_SOCKET', DEFAULT_SOCKET))
    args = parser.parse_args()
    lock_name = SCRIPT_DIR / '.bitrix_mysql_backup.lock'
    with open(lock_name, 'a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('ERROR: Another Bitrix MySQL backup is running', file=sys.stderr)
            return ERROR_EXIT
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        return run(args)


if __name__ == '__main__':
    sys.exit(main())
