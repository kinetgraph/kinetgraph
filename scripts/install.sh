#!/bin/sh
# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0
#
# knt installer -- installs the Kinetgraph CLI (``knt``).
#
#   curl -LsSf https://raw.githubusercontent.com/kinetgraph/kinetgraph/main/scripts/install.sh | sh
#
# The script is POSIX sh and needs nothing but a Python 3.12+
# interpreter. The package is pure Python (``py3-none-any``), so
# there is one artifact for every platform -- no OS or CPU
# detection, no binary download.
#
# Options:
#   --version <v>   Install a specific version (e.g. 0.19.0).
#   --uninstall     Remove the installation.
#   --help          Show this help.
#
# Environment:
#   KNT_HOME        Where the virtual environment lives.
#                   Default: $XDG_DATA_HOME/knt or ~/.local/share/knt
#   KNT_BIN_DIR     Where the ``knt`` shim is linked.
#                   Default: $XDG_BIN_HOME or ~/.local/bin
#   KNT_PYTHON      The interpreter to use. Default: the first
#                   python3.13/python3.12/python3 on the PATH.
#
# The install is user-local: it never touches the system Python
# and never needs root. Re-running the script upgrades in place.

set -eu

PACKAGE="kntgraph"
# The CLI's own feature set. ``[cli]`` is the scaffolder;
# ``[crypto]`` is what the ``knt keys`` command needs (Ed25519).
# The rest of the CLI runs without it, but a CLI whose key
# command is broken is not a CLI.
EXTRA="cli,crypto"
MIN_PYTHON="3.12"
VERSION=""
UNINSTALL="no"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"
    C_RED="$(printf '\033[31m')"
else
    C_RESET=""
    C_BOLD=""
    C_DIM=""
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
fi

log() {
    printf '%s==>%s %s\n' "$C_GREEN" "$C_RESET" "$1"
}

info() {
    printf '    %s%s%s\n' "$C_DIM" "$1" "$C_RESET"
}

warn() {
    printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$1" >&2
}

die() {
    printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$1" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
knt installer -- installs the Kinetgraph CLI.

Usage:
  curl -LsSf https://raw.githubusercontent.com/kinetgraph/kinetgraph/main/scripts/install.sh | sh

Options:
  --version <v>   Install a specific version (e.g. 0.19.0).
  --uninstall     Remove the installation.
  --help          Show this help.

Environment:
  KNT_HOME        Virtual environment directory.
                  Default: $XDG_DATA_HOME/knt or ~/.local/share/knt
  KNT_BIN_DIR     Directory the "knt" shim is linked into.
                  Default: $XDG_BIN_HOME or ~/.local/bin
USAGE
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || die "--version needs a value."
            VERSION="$2"
            shift 2
            ;;
        --version=*)
            VERSION="${1#--version=}"
            shift
            ;;
        --uninstall)
            UNINSTALL="yes"
            shift
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1 (try --help)."
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

if [ -n "${KNT_HOME:-}" ]; then
    HOME_DIR="$KNT_HOME"
elif [ -n "${XDG_DATA_HOME:-}" ]; then
    HOME_DIR="$XDG_DATA_HOME/knt"
else
    HOME_DIR="$HOME/.local/share/knt"
fi

if [ -n "${KNT_BIN_DIR:-}" ]; then
    BIN_DIR="$KNT_BIN_DIR"
elif [ -n "${XDG_BIN_HOME:-}" ]; then
    BIN_DIR="$XDG_BIN_HOME"
else
    BIN_DIR="$HOME/.local/bin"
fi

VENV_DIR="$HOME_DIR/venv"

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [ "$UNINSTALL" = "yes" ]; then
    log "Removing knt"
    if [ -e "$BIN_DIR/knt" ]; then
        rm -f "$BIN_DIR/knt"
        info "removed $BIN_DIR/knt"
    fi
    if [ -d "$VENV_DIR" ]; then
        rm -rf "$VENV_DIR"
        info "removed $VENV_DIR"
    fi
    if command -v uv >/dev/null 2>&1; then
        uv tool uninstall "$PACKAGE" >/dev/null 2>&1 || true
        info "removed the uv tool install"
    fi
    log "Done."
    exit 0
fi

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

find_python() {
    for candidate in python3.13 python3.12 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" \
            >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

supports_venv() {
    "$1" -c "import ensurepip" >/dev/null 2>&1
}

if [ -n "${KNT_PYTHON:-}" ]; then
    PYTHON="$KNT_PYTHON"
    command -v "$PYTHON" >/dev/null 2>&1 ||
        die "KNT_PYTHON=$PYTHON is not an executable."
    "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" \
        >/dev/null 2>&1 ||
        die "KNT_PYTHON=$PYTHON is older than Python $MIN_PYTHON."
else
    PYTHON="$(find_python || true)"
fi

if [ -z "$PYTHON" ]; then
    die "Python $MIN_PYTHON+ is required but was not found.
    Install it (https://www.python.org/downloads/) or let uv manage it:
      curl -LsSf https://astral.sh/uv/install.sh | sh
    then re-run this script."
fi

info "python:   $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

SPEC="$PACKAGE[$EXTRA]"
if [ -n "$VERSION" ]; then
    SPEC="$SPEC==$VERSION"
fi

mkdir -p "$HOME_DIR" "$BIN_DIR"

if command -v uv >/dev/null 2>&1; then
    log "Installing $SPEC with uv"
    info "uv:       $(command -v uv)"
    uv tool install --force "$SPEC"
    # uv owns the layout: the shim lands in the directory uv
    # manages, which is not necessarily the one we were asked
    # for. Ask uv where it put it.
    UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
    if [ -n "$UV_BIN_DIR" ]; then
        BIN_DIR="$UV_BIN_DIR"
        info "installed via uv (shim in $BIN_DIR)"
    else
        info "installed via uv (uv manages the environment)"
    fi
    TOOL_PYTHON="$(uv tool dir 2>/dev/null || true)/kntgraph/bin/python"
else
    log "Installing $SPEC with pip"
    info "uv not found -- falling back to a local virtual environment"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        supports_venv "$PYTHON" || die "this Python cannot create a virtual environment (ensurepip is missing).
    On Debian/Ubuntu: apt install python3-venv
    Or install uv and re-run this script:
      curl -LsSf https://astral.sh/uv/install.sh | sh"
        "$PYTHON" -m venv "$VENV_DIR" || die "the virtual environment could not be created."
    fi
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$SPEC"
    ln -sf "$VENV_DIR/bin/knt" "$BIN_DIR/knt"
    info "installed into $VENV_DIR"
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

INSTALLED=""
if [ -x "$BIN_DIR/knt" ]; then
    INSTALLED="$BIN_DIR/knt"
elif [ -x "$HOME_DIR/venv/bin/knt" ]; then
    INSTALLED="$HOME_DIR/venv/bin/knt"
else
    INSTALLED="$(command -v knt 2>/dev/null || true)"
fi

if [ -z "$INSTALLED" ]; then
    warn "the 'knt' entry point was not found where expected."
    INSTALLED="knt"
fi

# The version: the CLI's own flag first, the package metadata
# second (a release older than the flag still reports).
detect_version() {
    reported="$("$INSTALLED" --version 2>/dev/null || true)"
    if [ -n "$reported" ]; then
        printf '%s' "$reported"
        return 0
    fi
    for interpreter in "${TOOL_PYTHON:-}" "$VENV_DIR/bin/python"; do
        [ -x "$interpreter" ] || continue
        reported="$("$interpreter" -c \
            'import kntgraph; print("knt", kntgraph.__version__)' 2>/dev/null || true)"
        if [ -n "$reported" ]; then
            printf '%s' "$reported"
            return 0
        fi
    done
    printf 'unknown'
}

case ":$PATH:" in
    *":$BIN_DIR:"*)
        ;;
    *)
        warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
        printf '\n    export PATH="%s:$PATH"\n\n' "$BIN_DIR" >&2
        ;;
esac

log "Installed knt"
info "version:  $(detect_version)"
info "binary:   $INSTALLED"

printf '\n'
printf '%sNext:%s\n' "$C_BOLD" "$C_RESET"
printf '  1. cd <your project directory>\n'
printf '  2. knt init project my_platform\n'
printf '\n'
printf 'Read the guide: https://github.com/kinetgraph/kinetgraph/blob/main/docs/cli_guide.md\n'
