#!/usr/bin/env bash
#
# Download a standalone Python runtime (python-build-standalone, maintained by
# Astral), install the teammate-sync wheel + all deps into it, and stage it at
# desktop/resources/python-runtime/ so electron-builder bundles it.
#
# The result is a fully self-contained interpreter — the shipped .app/.exe does
# NOT require the user to have Python installed.
#
# Usage:
#   bash scripts/bundle-python.sh                 # auto-detect host platform/arch
#   PBS_TARGET=aarch64-apple-darwin bash ...      # cross-target override
#
# Env:
#   PY_VERSION    CPython version to bundle (default 3.12.8)
#   PBS_DATE      python-build-standalone release tag date (default below)
#   PBS_TARGET    rust-style target triple (auto-detected if unset)

set -euo pipefail

PY_VERSION="${PY_VERSION:-3.12.8}"
PBS_DATE="${PBS_DATE:-20241219}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"        # desktop/
REPO_ROOT="$(cd "$HERE/.." && pwd)"             # repo root (has pyproject.toml)
OUT_DIR="$HERE/resources/python-runtime"

# ── Detect target triple ────────────────────────────────────────────────────
detect_target() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Darwin)
      case "$arch" in
        arm64) echo "aarch64-apple-darwin" ;;
        x86_64) echo "x86_64-apple-darwin" ;;
        *) echo "unsupported-darwin-$arch" ;;
      esac ;;
    Linux)
      case "$arch" in
        x86_64) echo "x86_64-unknown-linux-gnu" ;;
        aarch64) echo "aarch64-unknown-linux-gnu" ;;
        *) echo "unsupported-linux-$arch" ;;
      esac ;;
    *) echo "unsupported-os-$os" ;;
  esac
}

TARGET="${PBS_TARGET:-$(detect_target)}"
if [[ "$TARGET" == unsupported-* ]]; then
  echo "ERROR: unsupported platform: $TARGET" >&2
  echo "Set PBS_TARGET manually (e.g. x86_64-pc-windows-msvc on Windows)." >&2
  exit 1
fi

ASSET="cpython-${PY_VERSION}+${PBS_DATE}-${TARGET}-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_DATE}/${ASSET}"

echo "==> Target:        $TARGET"
echo "==> Python:        $PY_VERSION (pbs ${PBS_DATE})"
echo "==> Download:      $URL"
echo "==> Staging to:    $OUT_DIR"

# ── Clean + download + extract ──────────────────────────────────────────────
rm -rf "$OUT_DIR"
mkdir -p "$HERE/resources"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Downloading standalone Python…"
curl -fSL "$URL" -o "$TMP/python.tar.gz"

echo "==> Extracting…"
tar -xzf "$TMP/python.tar.gz" -C "$TMP"
# The archive extracts to a top-level "python/" directory.
mv "$TMP/python" "$OUT_DIR"

PYBIN="$OUT_DIR/bin/python3"
if [[ ! -x "$PYBIN" ]]; then
  # Windows layout
  PYBIN="$OUT_DIR/python.exe"
fi
echo "==> Bundled interpreter: $PYBIN"

# ── Install teammate-sync (+ deps) into the bundled runtime ──────────────────
# The bundled interpreter builds the package in-process via its own pip +
# the setuptools backend, so we don't depend on any system Python having
# `build`. Prefer a prebuilt wheel in dist/ if one exists; else install from
# the repo source directory directly.
echo "==> Upgrading bundled pip…"
"$PYBIN" -m pip install --upgrade pip >/dev/null

WHEEL="$(ls -t "$REPO_ROOT"/dist/teammate_sync-*.whl 2>/dev/null | head -1 || true)"
if [[ -n "$WHEEL" ]]; then
  echo "==> Installing prebuilt wheel: $WHEEL"
  "$PYBIN" -m pip install "$WHEEL" >/dev/null
else
  echo "==> No prebuilt wheel; installing from source ($REPO_ROOT)…"
  "$PYBIN" -m pip install "$REPO_ROOT" >/dev/null
fi

# ── Trim to shrink bundle size ──────────────────────────────────────────────
echo "==> Trimming runtime (tests, caches, pyc)…"
find "$OUT_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT_DIR" -type d -name "test" -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT_DIR" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> Verifying teammate_sync imports inside the bundle…"
"$PYBIN" -c "import teammate_sync; import teammate_sync.cli; print('  ok — teammate_sync importable in bundled runtime')"

SIZE="$(du -sh "$OUT_DIR" | awk '{print $1}')"
echo "==> Done. Bundled runtime size: $SIZE"
echo "==> electron-builder will include resources/python-runtime/ as extraResources."
