#!/usr/bin/env bash
# Bitrix + Duplicati: interactive first install and subsequent update, no Git.
# Public GitHub repository assumed. Always review remote updates before applying.
set -Eeuo pipefail
umask 077

InstallDir="${BITRIX_BACKUP_INSTALL_DIR:-/usr/scripts/bitrix_mysql_backup}"
GitHubRepo="${BITRIX_BACKUP_GITHUB_REPO:-avmaksimov/bitrix-duplicati-backup}"
GitHubBranch="${BITRIX_BACKUP_BRANCH:-main}"
LocalSource="${BITRIX_BACKUP_SOURCE_DIR:-}"
Files=(bitrix_mysql_backup.py bitrix_mysql_backup.sh README.md README.ru.md .gitignore install.sh)
Chosen=()
TempDir=''

info() { printf '%s\n' "$*"; }
error() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
    local name
    for name in "${Chosen[@]}"; do
        rm -f -- "$InstallDir/$name.new"
    done
    if [[ -n "$TempDir" && -d "$TempDir" ]]; then
        rm -rf -- "$TempDir"
    fi
}
trap cleanup EXIT

for tool in tar sha256sum mktemp cp mv chmod; do
    command -v "$tool" >/dev/null 2>&1 || error "Required command not found: $tool"
done

if [[ -n "$LocalSource" ]]; then
    # Useful for an offline trial before the GitHub repository is published.
    [[ -d "$LocalSource" ]] || error "Source directory not found: $LocalSource"
    SourceDir=$(cd -- "$LocalSource" && pwd)
    info "Local source: $SourceDir"
else
    command -v curl >/dev/null 2>&1 || error 'curl is required to download from GitHub'
    [[ "$GitHubRepo" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || error 'Unsafe GitHub repository name'
    [[ "$GitHubBranch" =~ ^[a-zA-Z0-9_./-]+$ ]] || error 'Unsafe branch name'
    TempDir=$(mktemp -d) || error 'Cannot create temporary directory'
    Archive="$TempDir/source.tar.gz"
    URL="https://github.com/$GitHubRepo/archive/refs/heads/$GitHubBranch.tar.gz"
    info "Downloading: $URL"
    curl --fail --location --show-error --silent --retry 2 --output "$Archive" "$URL" \
        || error 'Cannot download GitHub archive'
    tar -tzf "$Archive" >/dev/null || error 'Downloaded file is not a valid tar.gz archive'
    mkdir -p "$TempDir/unpacked"
    tar -xzf "$Archive" -C "$TempDir/unpacked" || error 'Cannot extract GitHub archive'
    SourceDir=''
    for candidate in "$TempDir"/unpacked/*; do
        if [[ -d "$candidate" ]]; then
            [[ -z "$SourceDir" ]] || error 'Unexpected multiple root directories in archive'
            SourceDir="$candidate"
        fi
    done
    [[ -n "$SourceDir" ]] || error 'No project directory in archive'
fi

for name in "${Files[@]}"; do
    [[ -f "$SourceDir/$name" && ! -L "$SourceDir/$name" ]] \
        || error "Missing or unsafe source file: $name"
done

# Check code before prompting or touching any installed files.
bash -n "$SourceDir/bitrix_mysql_backup.sh" || error 'Invalid Bash hook syntax'
bash -n "$SourceDir/install.sh" || error 'Invalid installer syntax'
command -v python3 >/dev/null 2>&1 || error 'python3 is required for the Bitrix MySQL backup'
python3 - <<'PY' "$SourceDir/bitrix_mysql_backup.py" || error 'Invalid Python script syntax'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_bytes()
compile(source, sys.argv[1], 'exec')
PY

hash_file() {
    local result
    result=$(sha256sum -- "$1") || error "Cannot hash file: $1"
    printf '%s\n' "${result%% *}"
}

info "Destination: $InstallDir"
info 'SHA-256 comparison:'
for name in "${Files[@]}"; do
    source_hash=$(hash_file "$SourceDir/$name")
    destination="$InstallDir/$name"
    if [[ -f "$destination" && ! -L "$destination" ]]; then
        target_hash=$(hash_file "$destination")
        if [[ "$source_hash" == "$target_hash" ]]; then
            info "  SAME  $name"
            continue
        fi
        info "  DIFF  $name (local: ${target_hash:0:12}; GitHub/source: ${source_hash:0:12})"
    elif [[ -e "$destination" || -L "$destination" ]]; then
        error "Unsafe target file: $destination"
    else
        info "  NEW   $name"
    fi

    if [[ ! -r /dev/tty ]]; then
        error 'An interactive terminal is required to approve file updates'
    fi
    printf '  Replace %s? [y/N]: ' "$name" > /dev/tty
    if ! IFS= read -r answer < /dev/tty; then
        error 'Cannot read confirmation from terminal'
    fi
    case "$answer" in
        y|Y|yes|YES|Yes) Chosen+=("$name");;
        *) info "  SKIP  $name";;
    esac
done

if (( ${#Chosen[@]} == 0 )); then
    info 'No updates selected. Nothing changed.'
    exit 0
fi

# On first install, use standard directory permissions; on update, do not
# unexpectedly reset administrator-adjusted ownership, modes or ACLs.
[[ ! -L "$InstallDir" ]] || error "Installation directory must not be a symlink: $InstallDir"
if [[ ! -d "$InstallDir" ]]; then
    [[ ! -e "$InstallDir" ]] || error "Installation path is not a directory: $InstallDir"
    mkdir -p -m 755 -- "$InstallDir" || error 'Cannot create installation directory'
fi
[[ ! -L "$InstallDir/backups" ]] || error 'backups/ must not be a symlink'
if [[ ! -d "$InstallDir/backups" ]]; then
    [[ ! -e "$InstallDir/backups" ]] || error 'backups/ exists but is not a directory'
    mkdir -m 700 -- "$InstallDir/backups" || error 'Cannot create private backups directory'
fi

# Stage *all* approved changes before modifying any active program file.
info 'Preparing all selected files (.new)...'
for name in "${Chosen[@]}"; do
    cp -- "$SourceDir/$name" "$InstallDir/$name.new" || error "Cannot stage: $name"
    case "$name" in
        *.sh) chmod 755 -- "$InstallDir/$name.new";;
        *) chmod 644 -- "$InstallDir/$name.new";;
    esac
    [[ "$(hash_file "$InstallDir/$name.new")" == "$(hash_file "$SourceDir/$name")" ]] \
        || error "Staged file failed SHA-256 check: $name"
done

info 'Applying changes...'
for name in "${Chosen[@]}"; do
    mv -f -- "$InstallDir/$name.new" "$InstallDir/$name" || error "Cannot replace: $name"
    info "  UPDATED $name"
done
info 'Installation/update completed. Existing SQL dumps and logs were not touched.'
if [[ ! -f "$InstallDir/bitrix_mysql_backup.sh" || ! -f "$InstallDir/bitrix_mysql_backup.py" ]]; then
    info 'WARNING: One or more required program files were skipped. Installation is incomplete.'
fi
info "Duplicati --run-script-before-required: $InstallDir/bitrix_mysql_backup.sh"
info "Duplicati backup source: $InstallDir/backups"
