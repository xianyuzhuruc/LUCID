#!/bin/sh
# Install tmux without root privileges.
set -eu

scope="${1:-local}"
project_dir="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
runtime_root="${LUCID_RUNTIME_DIR:-$project_dir/.lucid-runtime}"
env_dir="$runtime_root/env"
micromamba_dir="$runtime_root/micromamba"
export PATH="$env_dir/bin:$PATH"

log() {
    printf '[LUCID deps] %s\n' "$*" >&2
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

tmux_works() {
    [ -x "$1" ] && "$1" -V >/dev/null 2>&1
}

find_working_tmux() {
    if tmux_works "$env_dir/bin/tmux"; then return 0; fi
    old_ifs="$IFS"
    IFS=:
    for dir in $PATH; do
        IFS="$old_ifs"
        [ -n "$dir" ] || continue
        candidate="$dir/tmux"
        [ "$candidate" = "$env_dir/bin/tmux" ] && continue
        if tmux_works "$candidate"; then return 0; fi
        IFS=:
    done
    IFS="$old_ifs"
    return 1
}

quarantine_broken_runtime_tmux() {
    tmux_path="$env_dir/bin/tmux"
    if [ -e "$tmux_path" ] && ! tmux_works "$tmux_path"; then
        mv "$tmux_path" "$tmux_path.broken.$(date +%s)" 2>/dev/null || rm -f "$tmux_path"
    fi
}

needs_install() {
    if find_working_tmux; then return 1; fi
    return 0
}

download_file() {
    url="$1"
    output="$2"
    if has_cmd curl; then
        curl -fsSL "$url" -o "$output"
        return
    fi
    if has_cmd wget; then
        wget -q "$url" -O "$output"
        return
    fi
    if has_cmd python3; then
        python3 - "$url" "$output" <<'PY'
import sys
import urllib.request

urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
        return
    fi
    log "curl, wget, or python3 is required to download the rootless tmux runtime"
    exit 1
}

micromamba_platform() {
    os_name="$(uname -s)"
    arch_name="$(uname -m)"
    case "$os_name:$arch_name" in
        Linux:x86_64|Linux:amd64) printf '%s\n' "linux-64" ;;
        Linux:aarch64|Linux:arm64) printf '%s\n' "linux-aarch64" ;;
        Darwin:x86_64|Darwin:amd64) printf '%s\n' "osx-64" ;;
        Darwin:aarch64|Darwin:arm64) printf '%s\n' "osx-arm64" ;;
        *)
            log "unsupported platform for rootless runtime: $os_name $arch_name"
            exit 1
            ;;
    esac
}

extract_micromamba() {
    archive="$1"
    output_dir="$2"
    if has_cmd tar; then
        tar -xjf "$archive" -C "$output_dir" bin/micromamba
        return
    fi
    if has_cmd python3; then
        python3 - "$archive" "$output_dir" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:bz2") as archive:
    archive.extract("bin/micromamba", sys.argv[2])
PY
        return
    fi
    log "tar or python3 is required to unpack micromamba"
    exit 1
}

ensure_micromamba() {
    if [ -x "$micromamba_dir/bin/micromamba" ]; then
        printf '%s\n' "$micromamba_dir/bin/micromamba"
        return
    fi
    platform="$(micromamba_platform)"
    tmp_dir="$(mktemp -d)"
    archive="$tmp_dir/micromamba.tar.bz2"
    mkdir -p "$micromamba_dir/bin"
    download_file "https://micro.mamba.pm/api/micromamba/$platform/latest" "$archive"
    extract_micromamba "$archive" "$tmp_dir"
    mv "$tmp_dir/bin/micromamba" "$micromamba_dir/bin/micromamba"
    chmod 755 "$micromamba_dir/bin/micromamba"
    rm -rf "$tmp_dir"
    printf '%s\n' "$micromamba_dir/bin/micromamba"
}

install_runtime() {
    log "installing rootless runtime in $runtime_root"
    mamba="$(ensure_micromamba)"
    mkdir -p "$runtime_root"
    if [ -d "$env_dir/conda-meta" ]; then
        MAMBA_ROOT_PREFIX="$micromamba_dir/root" "$mamba" install -y -p "$env_dir" --override-channels -c conda-forge tmux
        return
    fi
    MAMBA_ROOT_PREFIX="$micromamba_dir/root" "$mamba" create -y -p "$env_dir" --override-channels -c conda-forge tmux
}

verify_install() {
    missing=""
    if ! find_working_tmux; then missing="$missing tmux"; fi
    if [ -n "$missing" ]; then
        log "rootless runtime did not provide:$missing"
        exit 1
    fi
}

quarantine_broken_runtime_tmux

if needs_install; then
    install_runtime
else
    log "rootless tmux already available for $scope"
fi

verify_install
