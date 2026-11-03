#!/usr/bin/env bash
# Duplicati hook: execute the Bitrix MySQL dump only for a Backup operation.
# Append timestamped output (including Python stderr) to one persistent log.
set -Eeuo pipefail
umask 077

if [[ "${DUPLICATI__OPERATIONNAME:-}" != "Backup" ]]; then
    exit 0
fi

ScriptDir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LogFilename="$ScriptDir/bitrix_mysql_backup.log"
StartSeconds=$(date +%s)
StartedAt=$(date '+%Y-%m-%d %H:%M:%S')

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LogFilename"
}

# Separate consecutive Backup runs without clearing the previous history.
if [[ -s "$LogFilename" ]]; then
    printf '\n' | tee -a "$LogFilename"
fi
log '========== BITRIX MYSQL BACKUP: START =========='
log "Started: $StartedAt"

# With set -e enabled, a failed Python process would prevent us from
# recording its exit status and the end timestamp. Temporarily disable it.
set +e
python3 -u "$ScriptDir/bitrix_mysql_backup.py" 2>&1 | tee -a "$LogFilename"
Results=("${PIPESTATUS[@]}")
set -e

PythonCode=${Results[0]}
TeeCode=${Results[1]}

if (( TeeCode != 0 )); then
    log "ERROR: failed to write the backup log (tee exit code $TeeCode)"
    PythonCode=10
fi

FinishedAt=$(date '+%Y-%m-%d %H:%M:%S')
Elapsed=$(( $(date +%s) - StartSeconds ))
printf -v Duration '%02d:%02d:%02d' \
    "$((Elapsed / 3600))" "$(((Elapsed % 3600) / 60))" "$((Elapsed % 60))"

log "Finished: $FinishedAt"
log "Duration: $Duration"
if (( PythonCode == 0 )); then
    log 'Result: SUCCESS (exit=0)'
else
    log "Result: FAILED (exit=$PythonCode)"
fi
log '========== BITRIX MYSQL BACKUP: END =========='
exit "$PythonCode"
