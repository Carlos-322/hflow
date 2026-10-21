#!/usr/bin/env bash
# Windows checkout is authoritative; only the dedicated Linux mirror is synchronized.
set -euo pipefail
mode="${1:?Expected setup, check, sync, or run}"
source_root="$(realpath "${2:?Expected the Windows checkout path}")"
shift 2
case "$mode" in setup|check|sync|run) ;; *) echo 'Invalid action' >&2; exit 2;; esac
export PATH="$HOME/.local/bin:$PATH"
root="$HOME/workspaces/ai_Quality_Inspection_Division/HFlow"
repo="$root/repo"
[[ -f "$source_root/uv.lock" && -f "$source_root/src/hflow/checks.py" ]] || { echo 'Not an HFlow checkout' >&2; exit 2; }
[[ ! -L "$root" && ! -L "$repo" ]] || { echo 'Refusing a symlinked runtime directory' >&2; exit 2; }
if [[ ! -f "$root/.windows-source" ]]; then
    if [[ -d "$root" && -n "$(ls -A "$root")" ]]; then
        echo "Unmanaged nonempty directory: $root" >&2; exit 2
    fi
    mkdir -p "$root"
    printf '%s\n' "$source_root" > "$root/.windows-source"
fi
[[ "$(cat "$root/.windows-source")" == "$source_root" ]] || { echo 'Runtime belongs to a different checkout' >&2; exit 2; }
mkdir -p "$repo" "$root/data" "$root/cache" "$root/logs"
[[ "$(realpath "$repo")" == "$HOME/workspaces/ai_Quality_Inspection_Division/HFlow/repo" ]] || { echo 'Unexpected runtime destination' >&2; exit 2; }
exec 9>"$root/.environment.lock"
flock -n 9 || { echo 'Another HFlow environment command is running' >&2; exit 2; }
export PATH="$root/bin:$PATH"
export HFLOW_DATA_ROOT="$root/data"
export XDG_CACHE_HOME="$root/cache"
export UV_CACHE_DIR="$root/cache/uv"
export HFLOW_WINDOWS_SOURCE="$source_root"
export HFLOW_WSL_ROOT="$root"
log="$root/logs/${mode}-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee "$log") 2>&1
printf 'Source: %s\nRuntime: %s\nLog: %s\n' "$source_root" "$repo" "$log"
if [[ "$mode" == setup || "$mode" == sync || "$mode" == run ]]; then
    # --delete is confined to this marked, resolved runtime mirror. Excluded
    # environments/data are protected; raw recordings never enter the mirror.
    rsync -a --delete --exclude='.git/' --exclude='.venv/' --exclude='data/' \
        --exclude='.env' --exclude='.env.*' --exclude='__pycache__/' \
        --exclude='.pytest_cache/' --exclude='.ruff_cache/' --exclude='node_modules/' \
        "$source_root/" "$repo/"
fi
cd "$repo"
case "$mode" in
    setup)
        command -v uv
        command -v cc
        uv sync --locked --python 3.11
        uv run --locked --no-sync python scripts/wsl/verify.py
        ;;
    check)
        [[ -x .venv/bin/python ]] || { echo 'Run setup first' >&2; exit 2; }
        uv run --locked --no-sync python scripts/wsl/verify.py
        ;;
    sync) echo 'Source synchronized. No dependencies changed.' ;;
    run)
        [[ $# -gt 0 ]] || { echo 'Supply a command, for example: run hflow --help' >&2; exit 2; }
        [[ -x .venv/bin/python ]] || { echo 'Run setup first' >&2; exit 2; }
        uv run --locked "$@"
        ;;
esac
