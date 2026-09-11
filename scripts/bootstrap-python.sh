#!/bin/sh
# Bootstrap a project-local Python 3.11 without using the host Python.
set -eu

project_dir="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
runtime_root="${LUCID_RUNTIME_DIR:-$project_dir/.lucid-runtime}"
python_dir="$runtime_root/python"
python_bin="$python_dir/bin/python"
micromamba_dir="$runtime_root/micromamba"
micromamba_bin="$micromamba_dir/bin/micromamba"

log() {
    printf '[LUCID Python] %s\n' "$*" >&2
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

python_311_works() {
    [ -x "$python_bin" ] && "$python_bin" -c \
        'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' \
        >/dev/null 2>&1
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
            log "unsupported platform: $os_name $arch_name"
            exit 1
            ;;
    esac
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
    log "curl or wget is required to download the bundled Python runtime"
    exit 1
}

ensure_micromamba() {
    if [ -x "$micromamba_bin" ]; then
        return
    fi
    if ! has_cmd tar; then
        log "tar is required to unpack the bundled Python runtime"
        exit 1
    fi

    platform="$(micromamba_platform)"
    tmp_dir="$(mktemp -d)"
    archive="$tmp_dir/micromamba.tar.bz2"
    trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

    log "downloading Python 3.11 bootstrap for $platform"
    download_file "https://micro.mamba.pm/api/micromamba/$platform/latest" "$archive"
    tar -xjf "$archive" -C "$tmp_dir" bin/micromamba
    mkdir -p "$micromamba_dir/bin"
    mv "$tmp_dir/bin/micromamba" "$micromamba_bin"
    chmod 755 "$micromamba_bin"
    rm -rf "$tmp_dir"
    trap - EXIT HUP INT TERM
}

install_python() {
    ensure_micromamba
    mkdir -p "$runtime_root" "$micromamba_dir/root"
    log "installing project-local Python 3.11 in $python_dir"
    if [ -d "$python_dir/conda-meta" ]; then
        MAMBA_ROOT_PREFIX="$micromamba_dir/root" "$micromamba_bin" \
            install -y -p "$python_dir" --override-channels -c conda-forge "python=3.11" >&2
    else
        MAMBA_ROOT_PREFIX="$micromamba_dir/root" "$micromamba_bin" \
            create -y -p "$python_dir" --override-channels -c conda-forge "python=3.11" >&2
    fi
}

if ! python_311_works; then
    install_python
fi

if ! python_311_works; then
    log "project-local Python 3.11 installation failed"
    exit 1
fi

printf '%s\n' "$python_bin"
