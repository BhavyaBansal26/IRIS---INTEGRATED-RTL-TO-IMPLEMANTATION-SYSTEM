#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bhavya Bansal / BCDC (Bansal Chip Design Corporation)
# =============================================================================
#  I.R.I.S. -- Integrated RTL-to-Implementation System   (Mark I, by BCDC)
#  One-file installer for the complete open-source RTL -> GDSII stack
# =============================================================================
#  What this does (idempotent -- safe to re-run):
#    1. apt deps for the GUI (python3-tk, curl, git, xz ...)       [Ubuntu/Debian]
#    2. Nix package manager (single-user) if it isn't already there
#    3. A PINNED toolchain built from nixpkgs into ~/.bcdc/toolchain:
#         yosys  openroad  klayout  magic  netgen  iverilog  verilator  gtkwave
#       (+ ngspice xschem with --analog)
#       -> one PATH entry, no nix-shell per stage, same versions on every
#          machine that uses the same flake.lock
#    4. Optional: OSS CAD Suite (sby, eqy, nextpnr, nightly yosys ...)
#    5. Copies PROGRAM.py into ~/.bcdc/app and installs the `iris` command
#       (`bcdc` stays as an alias)
#
#  Usage:
#    bash install_bcdc.sh                 # standard install
#    bash install_bcdc.sh --analog        # + ngspice, xschem
#    bash install_bcdc.sh --oss-cad-suite # + YosysHQ OSS CAD Suite
#    bash install_bcdc.sh --apt-klayout   # + distro KLayout as GUI fallback
#    bash install_bcdc.sh --offline DIR   # install from a `bcdc pack` bundle
#    bash install_bcdc.sh --update        # move to the newest nixpkgs
#    bash install_bcdc.sh --uninstall
#
#  After install:   iris            (GUI)
#                   iris doctor     (check every tool)
#                   iris shell      (bash with all tools on PATH)
#                   iris run cfg.json [--stages ...]   (headless flow)
# =============================================================================
set -Eeuo pipefail

BCDC_VERSION="1.4.0"
PRODUCT_MARK="Mark I"
BCDC_HOME="${BCDC_HOME:-$HOME/.bcdc}"
# nixpkgs comes from the nixos.org channel tarball (NOT github:, which hits the
# GitHub API and fails with "API rate limit exceeded" on shared/college IPs).
NIXPKGS_CHANNEL="${BCDC_NIXPKGS_CHANNEL:-https://channels.nixos.org/nixos-unstable/nixexprs.tar.xz}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_LINK_DIR="$HOME/.local/bin"
MIN_FREE_GB=8

WITH_ANALOG=0
WITH_OSS=0
WITH_APT_KLAYOUT=0
OFFLINE_DIR=""
ASSUME_YES=0
DO_UNINSTALL=0
SKIP_APT=0
DO_UPDATE=0

# ---------------------------------------------------------------- helpers ----
c_g=$'\033[1;32m'; c_y=$'\033[1;33m'; c_r=$'\033[1;31m'; c_c=$'\033[1;36m'; c_0=$'\033[0m'
step() { echo -e "\n${c_c}==>${c_0} $*"; }
ok()   { echo -e "  ${c_g}[ok]${c_0} $*"; }
warn() { echo -e "  ${c_y}[warn]${c_0} $*"; }
die()  { echo -e "\n${c_r}[error]${c_0} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --analog)         WITH_ANALOG=1 ;;
    --oss-cad-suite)  WITH_OSS=1 ;;
    --apt-klayout)    WITH_APT_KLAYOUT=1 ;;
    --offline)        OFFLINE_DIR="$(cd "${2:?--offline needs a directory}" && pwd)"; shift ;;
    --skip-apt)       SKIP_APT=1 ;;
    -y|--yes)         ASSUME_YES=1 ;;
    --uninstall)      DO_UNINSTALL=1 ;;
    --update)         DO_UPDATE=1 ;;
    -h|--help)        usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  read -r -p "  $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]
}

NIX_FLAGS=(--extra-experimental-features "nix-command flakes")

load_nix_env() {
  # Pick up an existing Nix install (single-user or daemon) in this shell
  local f
  for f in "$HOME/.nix-profile/etc/profile.d/nix.sh" \
           /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh; do
    # shellcheck disable=SC1090
    [[ -r "$f" ]] && { set +u; . "$f"; set -u; }
  done
  have nix
}

# -------------------------------------------------------------- uninstall ----
if [[ $DO_UNINSTALL -eq 1 ]]; then
  step "Uninstalling BCDC from $BCDC_HOME"
  confirm "Remove $BCDC_HOME and the bcdc command? (Nix itself is kept)" || exit 0
  rm -f "$BIN_LINK_DIR/bcdc" "$BIN_LINK_DIR/iris" "$BIN_LINK_DIR/grace"
  rm -rf "$BCDC_HOME"
  if load_nix_env; then
    nix-collect-garbage >/dev/null 2>&1 && ok "nix garbage collected" || true
  fi
  ok "BCDC removed. To remove Nix too, see https://nixos.org/manual/nix/stable/installation/uninstall"
  exit 0
fi

# -------------------------------------------------------------- preflight ----
step "IRIS $PRODUCT_MARK installer (v$BCDC_VERSION) -- by BCDC"
[[ "$(uname -s)" == "Linux" ]] || die "Linux only (run inside WSL2 on Windows)."
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  NIX_SYSTEM="x86_64-linux";  OSS_ARCH="linux-x64" ;;
  aarch64) NIX_SYSTEM="aarch64-linux"; OSS_ARCH="linux-arm64" ;;
  *) die "unsupported CPU architecture: $ARCH" ;;
esac
IS_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
DISTRO_ID="unknown"; DISTRO_VER=""
if [[ -r /etc/os-release ]]; then . /etc/os-release; DISTRO_ID="${ID:-unknown}"; DISTRO_VER="${VERSION_ID:-}"; fi
ok "arch=$ARCH  distro=$DISTRO_ID $DISTRO_VER  wsl=$IS_WSL"
if [[ $EUID -eq 0 && "${BCDC_ALLOW_ROOT:-0}" != "1" ]]; then
  die "run as your normal user, not root (sudo is used only where needed). Containers: BCDC_ALLOW_ROOT=1"
fi
SUDO="sudo"; [[ $EUID -eq 0 ]] && SUDO=""

free_gb=$(( $(df -Pk "$HOME" | awk 'NR==2{print $4}') / 1024 / 1024 ))
if (( free_gb < MIN_FREE_GB )); then
  warn "only ${free_gb} GB free in \$HOME -- toolchain needs ~${MIN_FREE_GB} GB."
  confirm "Continue anyway?" || exit 1
else
  ok "${free_gb} GB free"
fi

mkdir -p "$BCDC_HOME"/{app,bin,nix,downloads,logs} "$BIN_LINK_DIR"
LOG="$BCDC_HOME/logs/install_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
ok "log: $LOG"

# ------------------------------------------------------------ 1. apt deps ----
step "1/5  System packages"
if [[ $SKIP_APT -eq 1 ]]; then
  warn "skipped (--skip-apt)"
elif have apt-get; then
  APT_PKGS=(curl ca-certificates git xz-utils tar zstd python3 python3-tk python3-venv
            xclip libxcb-cursor0 libgl1 libegl1 fontconfig)
  [[ $WITH_APT_KLAYOUT -eq 1 ]] && APT_PKGS+=(klayout)
  missing=()
  for p in "${APT_PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if (( ${#missing[@]} )); then
    echo "  installing: ${missing[*]}"
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" \
      || warn "some apt packages failed -- continuing (GUI may need them)"
  fi
  ok "apt packages present"
else
  warn "no apt-get ($DISTRO_ID). Install manually: curl git xz python3 python3-tk"
fi
python3 -c "import tkinter" 2>/dev/null && ok "python3 + tkinter" \
  || warn "python3 tkinter missing -- the GUI will not start (headless 'bcdc run' still works)"

# ---------------------------------------------------------------- 2. Nix -----
step "2/5  Nix package manager"
if load_nix_env; then
  ok "found $(nix --version)"
else
  [[ -n "$OFFLINE_DIR" && -f "$OFFLINE_DIR/nix-installer.tar.xz" ]] && \
    warn "offline Nix install not automated -- extract $OFFLINE_DIR/nix-installer.tar.xz and run ./install --no-daemon"
  confirm "Nix is not installed. Install it now (single-user, /nix owned by $USER)?" \
    || die "Nix is required for the pinned toolchain."
  # Single-user mode works in WSL without systemd and needs no daemon.
  curl -fsSL https://nixos.org/nix/install -o "$BCDC_HOME/downloads/nix-install.sh"
  sh "$BCDC_HOME/downloads/nix-install.sh" --no-daemon --yes
  load_nix_env || die "Nix installed but not found on PATH -- open a new terminal and re-run."
  ok "installed $(nix --version)"
fi

# ------------------------------------------------------ 3. BCDC toolchain ----
step "3/5  BCDC toolchain (pinned nixpkgs)"
PKG_FILE="$BCDC_HOME/nix/packages.txt"
if [[ ! -f "$PKG_FILE" && -f "$SRC_DIR/packages.txt" ]]; then
  cp "$SRC_DIR/packages.txt" "$PKG_FILE"; ok "using shipped packages.txt"
fi
if [[ ! -f "$PKG_FILE" ]]; then
  cat > "$PKG_FILE" <<'EOF'
# One nixpkgs attribute per line. Edit + `bcdc update` to add tools.
# ---- synthesis / PnR / signoff ----
yosys
openroad
klayout
magic-vlsi
netgen-vlsi
# ---- verification ----
iverilog
verilator
gtkwave
EOF
fi
if [[ $WITH_ANALOG -eq 1 ]]; then
  for p in ngspice xschem; do grep -qx "$p" "$PKG_FILE" || echo "$p" >> "$PKG_FILE"; done
fi

# ---- Pin nixpkgs to ONE immutable release tarball -------------------------
# channels.nixos.org/<channel>/nixexprs.tar.xz redirects to an immutable
# releases.nixos.org/.../nixexprs.tar.xz. We record that final URL in
# nix/nixpkgs-url so the version never drifts. Copy nixpkgs-url + flake.lock
# next to install_bcdc.sh to give another PC the identical toolchain.
PIN_FILE="$BCDC_HOME/nix/nixpkgs-url"
if [[ $DO_UPDATE -eq 1 ]]; then
  rm -f "$PIN_FILE" "$BCDC_HOME/nix/flake.lock"; ok "update: re-resolving nixpkgs"
fi
if [[ -n "${BCDC_NIXPKGS_URL:-}" ]]; then
  NIXPKGS_URL="$BCDC_NIXPKGS_URL"
elif [[ -f "$PIN_FILE" ]]; then
  NIXPKGS_URL="$(cat "$PIN_FILE")"
elif [[ -f "$SRC_DIR/nixpkgs-url" ]]; then
  NIXPKGS_URL="$(cat "$SRC_DIR/nixpkgs-url")"; ok "using shipped nixpkgs-url"
  [[ -f "$SRC_DIR/flake.lock" ]] && cp "$SRC_DIR/flake.lock" "$BCDC_HOME/nix/flake.lock"
else
  NIXPKGS_URL="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$NIXPKGS_CHANNEL" || true)"
  if [[ -z "$NIXPKGS_URL" || "$NIXPKGS_URL" == "$NIXPKGS_CHANNEL" ]]; then
    warn "could not resolve an immutable release URL -- using the channel URL directly"
    NIXPKGS_URL="$NIXPKGS_CHANNEL"
  fi
fi
echo "$NIXPKGS_URL" > "$PIN_FILE"
ok "nixpkgs: $NIXPKGS_URL"

cat > "$BCDC_HOME/nix/flake.nix" <<EOF
{
  description = "BCDC -- pinned open-source RTL-to-GDS toolchain";
  # flake = false: plain source tarball, imported below (no GitHub API calls)
  inputs.nixpkgs = { url = "$NIXPKGS_URL"; flake = false; };
  outputs = { self, nixpkgs }:
    let
      lib     = import "\${nixpkgs}/lib";
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll  = f: lib.genAttrs systems (s: f (import "\${nixpkgs}" {
                  system = s; config = { }; overlays = [ ]; }));
      lines   = s: builtins.filter (x: builtins.isString x && x != "")
                     (builtins.split "\n" s);
      names   = builtins.filter (l: builtins.substring 0 1 l != "#")
                     (lines (builtins.readFile ./packages.txt));
    in {
      packages = forAll (pkgs: {
        default = pkgs.buildEnv {
          name  = "bcdc-toolchain";
          paths = map (n: lib.getAttrFromPath (lib.splitString "." n) pkgs) names;
          ignoreCollisions = true;
        };
      });
    };
}
EOF

TC_LINK="$BCDC_HOME/toolchain"
if [[ -n "$OFFLINE_DIR" ]]; then
  [[ -f "$OFFLINE_DIR/STORE_PATH" && -d "$OFFLINE_DIR/cache" ]] \
    || die "$OFFLINE_DIR is not a 'bcdc pack' bundle (need STORE_PATH + cache/)"
  sp="$(cat "$OFFLINE_DIR/STORE_PATH")"
  echo "  importing $sp from offline bundle ..."
  nix "${NIX_FLAGS[@]}" copy --no-check-sigs --from "file://$OFFLINE_DIR/cache" "$sp"
  nix-store --realise "$sp" --add-root "$TC_LINK" >/dev/null
  cp -f "$OFFLINE_DIR"/flake.lock "$BCDC_HOME/nix/" 2>/dev/null || true
else
  echo "  building (downloads prebuilt binaries from cache.nixos.org; first run ~2-4 GB) ..."
  [[ -L "$TC_LINK" ]] && ln -sfn "$(readlink -f "$TC_LINK")" "$BCDC_HOME/toolchain.prev"
  trap - ERR
  if ! nix "${NIX_FLAGS[@]}" build -L "path:$BCDC_HOME/nix#packages.$NIX_SYSTEM.default" -o "$TC_LINK" \
        2>&1 | tee "$BCDC_HOME/logs/nix-build.log"; then
    if grep -q "rate limit" "$BCDC_HOME/logs/nix-build.log"; then
      die "GitHub API rate limit. Add a token: echo 'access-tokens = github.com=<your_PAT>' >> ~/.config/nix/nix.conf  and re-run."
    fi
    die "nix build failed -- see $BCDC_HOME/logs/nix-build.log (network? disk space?). Re-running is safe."
  fi
  trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR
fi
[[ -x "$TC_LINK/bin/openroad" ]] || die "toolchain built but openroad missing in $TC_LINK/bin"
ok "toolchain -> $(readlink -f "$TC_LINK")"

# ---------------------------------------------------- 4. OSS CAD Suite -------
step "4/5  OSS CAD Suite (optional)"
OSS_DIR="$BCDC_HOME/oss-cad-suite"
if [[ $WITH_OSS -eq 1 ]]; then
  api="https://api.github.com/repos/YosysHQ/oss-cad-suite-build/releases/latest"
  url="$(curl -fsSL "$api" | grep -o "\"browser_download_url\": *\"[^\"]*oss-cad-suite-${OSS_ARCH}-[0-9]*\.tgz\"" \
         | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')" || true
  [[ -n "$url" ]] || die "could not find an OSS CAD Suite ${OSS_ARCH} asset (GitHub API rate-limit?)"
  tgz="$BCDC_HOME/downloads/$(basename "$url")"
  [[ -f "$tgz" ]] || curl -fL --progress-bar "$url" -o "$tgz"
  rm -rf "$OSS_DIR"; tar -xzf "$tgz" -C "$BCDC_HOME"
  ok "OSS CAD Suite -> $OSS_DIR ($(basename "$tgz"))"
else
  [[ -d "$OSS_DIR" ]] && ok "already present at $OSS_DIR" || ok "skipped (use --oss-cad-suite)"
fi

# ------------------------------------------------- 5. env, app, launcher -----
step "5/5  Environment + iris command"

cat > "$BCDC_HOME/env.sh" <<'EOF'
# ---- BCDC environment: `source ~/.bcdc/env.sh` (the bcdc command does this) ----
export BCDC_HOME="${BCDC_HOME:-$HOME/.bcdc}"
case ":$PATH:" in *":$BCDC_HOME/toolchain/bin:"*) ;; *)
  export PATH="$BCDC_HOME/bin:$BCDC_HOME/toolchain/bin:$PATH" ;;
esac
# OSS CAD Suite: appended (extras like sby/eqy/nextpnr); the pinned yosys wins
# unless BCDC_PREFER_OSS_YOSYS=1. Its own `environment` script is NOT sourced
# because it replaces python/tcl for the whole shell.
if [ -d "$BCDC_HOME/oss-cad-suite/bin" ]; then
  if [ "${BCDC_PREFER_OSS_YOSYS:-0}" = "1" ]; then
    export PATH="$BCDC_HOME/oss-cad-suite/bin:$PATH"
  else
    export PATH="$PATH:$BCDC_HOME/oss-cad-suite/bin"
  fi
fi
export YOSYS_EXE="$(command -v yosys 2>/dev/null)"
export OPENROAD_EXE="$(command -v openroad 2>/dev/null)"
export KLAYOUT_EXE="$(command -v klayout 2>/dev/null)"
# WSLg / X display for KLayout + OpenROAD GUI
if grep -qi microsoft /proc/version 2>/dev/null && [ -z "${DISPLAY:-}" ]; then
  export DISPLAY=:0
fi
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
EOF
[[ -f "$BCDC_HOME/env.local.sh" ]] || cat > "$BCDC_HOME/env.local.sh" <<'EOF'
# Your own additions (sourced after env.sh), e.g.:
# export MY_PDK_ROOT=$HOME/pdks/my_foundry_kit
EOF
echo '[ -f "$BCDC_HOME/env.local.sh" ] && . "$BCDC_HOME/env.local.sh"' >> "$BCDC_HOME/env.sh"

# ---- app ----
APP_SRC=""
for cand in "$SRC_DIR/iris.py" "$SRC_DIR/PROGRAM.py"; do
  [[ -f "$cand" ]] && { APP_SRC="$cand"; break; }
done
if [[ -n "$APP_SRC" ]]; then
  cp -f "$APP_SRC" "$BCDC_HOME/app/PROGRAM.py"
  ok "app: $(basename "$APP_SRC") -> $BCDC_HOME/app/PROGRAM.py"
else
  warn "iris.py not found next to installer -- copy it to $BCDC_HOME/app/PROGRAM.py yourself"
fi
# private PDK profiles (NDA kits) shipped next to the installer, never in git
if [[ -f "$SRC_DIR/pdk_profiles.local.json" && ! -f "$BCDC_HOME/pdk_profiles.local.json" ]]; then
  cp "$SRC_DIR/pdk_profiles.local.json" "$BCDC_HOME/pdk_profiles.local.json"
  ok "private PDK profiles installed"
fi
cp -f "$0" "$BCDC_HOME/app/install_bcdc.sh" 2>/dev/null || true
[[ -f "$SRC_DIR/README.md" ]] && cp -f "$SRC_DIR/README.md" "$BCDC_HOME/app/README.md"
echo "$BCDC_VERSION" > "$BCDC_HOME/VERSION"
echo "$PRODUCT_MARK" > "$BCDC_HOME/MARK"

# ---- launcher ----
cat > "$BCDC_HOME/bin/bcdc" <<'EOF'
#!/usr/bin/env bash
# BCDC launcher -- every subcommand runs inside the BCDC environment
BCDC_HOME="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export BCDC_HOME
. "$BCDC_HOME/env.sh"
PY="${BCDC_PYTHON:-/usr/bin/python3}"; [ -x "$PY" ] || PY="$(command -v python3)"
APP="$BCDC_HOME/app/PROGRAM.py"
NIXF=(--extra-experimental-features "nix-command flakes")
for f in "$HOME/.nix-profile/etc/profile.d/nix.sh" /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh; do
  [ -r "$f" ] && . "$f"
done

ver() {  # name, command...
  local name="$1"; shift
  local p; p="$(command -v "$name" 2>/dev/null)"
  if [ -z "$p" ]; then printf "  \033[1;31m%-10s MISSING\033[0m\n" "$name"; return 1; fi
  local v; v="$(timeout 20 "$@" 2>&1 | grep -m1 -iE '[0-9]+\.[0-9]+|[0-9]{2}Q[0-9]' | cut -c1-60)"
  printf "  \033[1;32m%-10s\033[0m %-60s %s\n" "$name" "${v:-(ok)}" "$p"
}

case "${1:-gui}" in
  gui)
    shift 2>/dev/null || true
    exec "$PY" "$APP" "$@" ;;
  run)
    shift; exec "$PY" "$APP" --headless "$@" ;;
  gen)
    shift; exec "$PY" "$APP" --generate-only "$@" ;;
  doctor)
    echo "IRIS $(cat "$BCDC_HOME/MARK" 2>/dev/null) (v$(cat "$BCDC_HOME/VERSION" 2>/dev/null)) -- Integrated RTL-to-Implementation System, by BCDC"
    echo "home=$BCDC_HOME"
    echo "toolchain: $(readlink -f "$BCDC_HOME/toolchain")"
    fail=0
    ver yosys     yosys -V                      || fail=1
    ver openroad  openroad -version             || fail=1
    ver klayout   sh -c 'QT_QPA_PLATFORM=offscreen klayout -v 2>/dev/null' || fail=1
    ver magic     magic --version               || fail=1
    ver netgen    sh -c 'echo quit | netgen -batch 2>&1 | head -3' || fail=1
    ver iverilog  iverilog -V                   || fail=1
    ver verilator verilator --version           || fail=1
    ver gtkwave   gtkwave --version             || true
    for opt in ngspice xschem sby eqy; do command -v $opt >/dev/null && ver $opt $opt --version; done
    "$PY" -c "import tkinter" 2>/dev/null && echo "  python3 tkinter ok ($PY)" \
      || { echo "  python3 tkinter MISSING (sudo apt install python3-tk)"; fail=1; }
    [ -n "${DISPLAY:-}" ] && echo "  DISPLAY=$DISPLAY" || echo "  DISPLAY not set (GUI viewers unavailable)"
    [ $fail -eq 0 ] && echo "ALL CORE TOOLS OK" || echo "SOME TOOLS MISSING -- re-run install_bcdc.sh"
    exit $fail ;;
  shell)
    echo "IRIS shell -- all tools on PATH. 'exit' to leave."
    exec bash --rcfile <(printf '[ -f ~/.bashrc ] && . ~/.bashrc\n. "%s/env.sh"\nPS1="(iris) $PS1"\n' "$BCDC_HOME") -i ;;
  view)
    shift; f="${1:?usage: bcdc view <file.gds|.def|.oas>}"
    klayout "$f" 2>/dev/null || { [ -x /usr/bin/klayout ] && exec /usr/bin/klayout "$f"; \
      echo "KLayout GUI failed. Try: bash install_bcdc.sh --apt-klayout"; exit 1; } ;;
  update)    # newest nixpkgs; old toolchain kept for `bcdc rollback`
    exec bash "$BCDC_HOME/app/install_bcdc.sh" --update --skip-apt --yes ;;
  rebuild)   # after editing ~/.bcdc/nix/packages.txt, keep the same pinned versions
    nix "${NIXF[@]}" build -L "path:$BCDC_HOME/nix#default" -o "$BCDC_HOME/toolchain" ;;
  rollback)
    [ -L "$BCDC_HOME/toolchain.prev" ] || { echo "no previous toolchain"; exit 1; }
    ln -sfn "$(readlink -f "$BCDC_HOME/toolchain.prev")" "$BCDC_HOME/toolchain"
    nix-store --add-root "$BCDC_HOME/toolchain" -r "$(readlink -f "$BCDC_HOME/toolchain")" >/dev/null
    echo "Rolled back to $(readlink -f "$BCDC_HOME/toolchain")" ;;
  pack)      # offline bundle for machines without internet
    out="$(readlink -f "${2:-$PWD/bcdc-offline}")"; mkdir -p "$out"
    sp="$(readlink -f "$BCDC_HOME/toolchain")"
    nix "${NIXF[@]}" copy --to "file://$out/cache" "$sp" || exit 1
    echo "$sp" > "$out/STORE_PATH"
    cp "$BCDC_HOME/nix/flake.lock" "$BCDC_HOME/nix/packages.txt" "$BCDC_HOME/nix/nixpkgs-url" "$out/" 2>/dev/null
    cp "$APP" "$BCDC_HOME/app/install_bcdc.sh" "$out/"
    echo "Bundle at $out  ($(du -sh "$out" | cut -f1))"
    echo "On target:  bash $out/install_bcdc.sh --offline $out" ;;
  release)   # shareable installer bundle (small; tools are rebuilt on the target)
    ver_s="$(cat "$BCDC_HOME/VERSION")"; name="iris-v$ver_s"
    dest="$(readlink -f "${2:-$PWD}")"; tmp="$(mktemp -d)"; d="$tmp/$name"; mkdir -p "$d"
    cp "$BCDC_HOME/app/install_bcdc.sh" "$d/install_iris.sh" && cp "$APP" "$d/" || exit 1
    [ -f "$BCDC_HOME/app/README.md" ] && cp "$BCDC_HOME/app/README.md" "$d/"
    for f in nixpkgs-url flake.lock packages.txt; do
      [ -f "$BCDC_HOME/nix/$f" ] && cp "$BCDC_HOME/nix/$f" "$d/"
    done
    [ -f "$d/nixpkgs-url" ] || echo "  warning: no nixpkgs-url pin -- re-run the latest install_bcdc.sh first"
    printf 'tar xzf %s.tar.gz && cd %s && bash install_bcdc.sh && bcdc doctor\n' "$name" "$name" > "$d/INSTALL.txt"
    tar czf "$dest/$name.tar.gz" -C "$tmp" "$name" && rm -rf "$tmp"
    (cd "$dest" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
    echo "Release: $dest/$name.tar.gz"; ls -1 "$dest/$name.tar.gz"*
    tar tzf "$dest/$name.tar.gz" ;;
  version) cat "$BCDC_HOME/VERSION" ;;
  uninstall) exec bash "$BCDC_HOME/app/install_bcdc.sh" --uninstall ;;
  help|-h|--help)
    cat <<H
IRIS $(cat "$BCDC_HOME/MARK" 2>/dev/null) -- Integrated RTL-to-Implementation System   (by BCDC)

iris [gui] [cfg.json]      launch the flow GUI
iris run cfg.json [...]    headless flow   (--stages synth,floorplan,... --work DIR --out DIR)
iris gen cfg.json --out D  only generate the scripts
iris doctor                check every tool + version
iris shell                 bash with the toolchain on PATH
iris view file.gds         open layout in KLayout
iris update | rebuild | rollback
iris release [DIR]         small shareable .tar.gz (installer + app + version pins)
iris pack [DIR]            offline bundle for another machine
iris uninstall
(\`bcdc\` works as an alias for every command)
H
    ;;
  *) echo "unknown command '$1' (iris help)"; exit 1 ;;
esac
EOF
chmod +x "$BCDC_HOME/bin/bcdc"
ln -sfn "$BCDC_HOME/bin/bcdc" "$BIN_LINK_DIR/bcdc"
rm -f "$BIN_LINK_DIR/grace" "$BCDC_HOME/bin/grace"   # old product name
ln -sfn "$BCDC_HOME/bin/bcdc" "$BIN_LINK_DIR/iris"
ln -sfn "$BCDC_HOME/bin/bcdc" "$BCDC_HOME/bin/iris"

# ~/.local/bin on PATH?
case ":$PATH:" in *":$BIN_LINK_DIR:"*) ;; *)
  if ! grep -q 'BCDC: local bin' "$HOME/.bashrc" 2>/dev/null; then
    printf '\n# BCDC: local bin\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
  fi
  warn "added ~/.local/bin to PATH in ~/.bashrc -- run: source ~/.bashrc" ;;
esac

# ------------------------------------------------------------------ done -----
trap - ERR
echo
"$BCDC_HOME/bin/bcdc" doctor || true
echo -e "\n${c_g}IRIS $PRODUCT_MARK installed.${c_0}  Launch with:  ${c_c}iris${c_0}   (help: iris help)"
echo    "  Same versions on another PC: copy $BCDC_HOME/nix/{nixpkgs-url,flake.lock} next to install_bcdc.sh"
