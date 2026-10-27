#!/usr/bin/env bash
# Duplicati hook: execute the Bitrix MySQL dump only for a Backup operation.
# All log output (including Python stderr) is recorded in real time.
set -Eeuo pipefail
umask 077

if [[ "${DUPLICATI__OPERATIONNAME:-}" != "Backup" ]]; then
    exit 0
fi

ScriptDir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LogFilename="$ScriptDir/bitrix_mysql_backup.log"
StartSeconds=$(date +%s)

# Truncate the previous log only when an actual Backup begins.
: > "$LogFilename"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LogFilename"
}

log "Started: Duplicati Backup"

# With set -e enabled, a failed Python process would prevent us from
# recording its exit status and the end timestamp. Temporarily disable it.
set +e
python3 -u "$ScriptDir/bitrix_mysql_backup.py" 2>&1 | tee -a "$LogFilename"
Results=("${PIPESTATUS[@]}")
set -e

PythonCode=${Results[0]}
TeeCode=${Results[1]}
Elapsed=$(( $(date +%s) - StartSeconds ))

if (( TeeCode != 0 )); then
    log "ERROR: failed to write the backup log (tee exit code $TeeCode)"
    PythonCode=10
fi

printf -v Duration '%02d:%02d:%02d' \
    "$((Elapsed / 3600))" "$(((Elapsed % 3600) / 60))" "$((Elapsed % 60))"
log "Finished: exit=$PythonCode; duration=$Duration"
exit "$PythonCode"
