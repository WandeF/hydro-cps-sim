#!/usr/bin/env bash
# Ubuntu 22.04/24.04 x86_64. Install only the Hydro-CPS command-line runtime.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$PROJECT_ROOT")"
ENV_NAME=hydro-cps
JOBS=2
DRY_RUN=0
CONDA_BIN="${CONDA_EXE:-conda}"
usage() {
  cat <<'EOF'
Usage: bash install.sh [options]
  --parent DIR    Clone dependencies here (default: project parent directory)
  --env NAME      Conda environment (default: hydro-cps)
  --jobs N        Parallel compiler jobs (default: 2)
  --dry-run       Show the installation plan without changing files
  -h, --help      Show help

Requires Ubuntu 22.04/24.04 x86_64, Conda, network access and sudo.
Installs system build dependencies; builds the OpenPLC command-line runtime and
ns-3; installs DHALSIM-epynet and Python dependencies; updates example path keys.
Existing repositories must use a pinned or supported historical revision.
Source edits are kept; generated OpenPLC build/driver files are refreshed.
No OpenPLC web service is installed. Existing Conda environments are reused;
this installer does not automatically remove unrelated packages from them.
Set CONDA_EXE if conda is not on PATH.
EOF
}
fail() { echo "[ERROR] $*" >&2; exit 1; }
value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "Missing value for $1"; }
while (($#)); do
  case "$1" in
    --parent) value "$@"; PARENT_DIR="$2"; shift 2 ;;
    --env) value "$@"; ENV_NAME="$2"; shift 2 ;;
    --jobs) value "$@"; JOBS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || fail '--jobs must be a positive integer'
[[ "$ENV_NAME" =~ ^[A-Za-z0-9_-]+$ && "$ENV_NAME" != base ]] || fail 'Invalid environment name (base is not allowed)'
[[ $EUID -ne 0 ]] || fail 'Run as a normal user; privileged steps use sudo'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'The bundled EPANET library requires Linux x86_64'
PARENT_DIR="$(realpath -m "$PARENT_DIR")"
EPYNET_URL=https://github.com/afmurillo/DHALSIM-epynet.git
EPYNET_REV=4cb24f3735f6f79d95b52a7cf71a8fcaaa5ed172
OPENPLC_URL=https://github.com/WandeF/OpenPLC_v3.git
OPENPLC_REV=64aa73e79692ec4a7ffd929e392b65e47eff6df8
NS3_URL=https://github.com/WandeF/ns-3-dev-git.git
NS3_REV=76771a9b5a04d02be0612ff7aac0a212ee20aae9
EPYNET="$PARENT_DIR/DHALSIM-epynet"
OPENPLC="$PARENT_DIR/OpenPLC_v3"
NS3="$PARENT_DIR/ns-3-dev-git"
printf 'Environment: %s\nDependency directory: %s\n' "$ENV_NAME" "$PARENT_DIR"
printf '%s @ %s\n' "$EPYNET_URL" "$EPYNET_REV" "$OPENPLC_URL" "$OPENPLC_REV" "$NS3_URL" "$NS3_REV"
if ((DRY_RUN)); then
  echo 'Plan: apt dependencies -> clone/reuse pinned repos -> Conda/Python -> OpenPLC native libraries -> ns-3 -> example paths -> smoke tests'
  exit 0
fi
source /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 22.04 || "$VERSION_ID" == 24.04 ) ]] || fail 'Supported systems: Ubuntu 22.04 and 24.04'
CONDA_BIN="$(command -v "$CONDA_BIN")" || fail 'Install Conda first, or set CONDA_EXE to its executable'
# Fail before changing the system if an existing repository is incompatible.
check_repo() {
  local dir="$1" url="$2" rev="$3" legacy="${4:-$3}" origin actual
  if [[ -e "$dir" ]]; then
    [[ -d "$dir/.git" ]] || fail "$dir already exists and is not a Git checkout"
    origin="$(git -C "$dir" remote get-url origin)"
    [[ "${origin%.git}" == "${url%.git}" ]] || fail "Unexpected repository origin: $dir ($origin)"
    actual="$(git -C "$dir" rev-parse HEAD)"
    [[ "$actual" == "$rev" || "$actual" == "$legacy" ]] || fail "$dir is at a different revision; use a separate --parent directory"
  fi
}
check_repo "$EPYNET" "$EPYNET_URL" "$EPYNET_REV"
# These historical local revisions have identical runtime/build sources.
check_repo "$OPENPLC" "$OPENPLC_URL" "$OPENPLC_REV" 65901c1a89d4dbd767780d97805eef911f6ca7f5
check_repo "$NS3" "$NS3_URL" "$NS3_REV" 39a4dcf21f840614db193e3ae916c69885cb6a29
sudo -v
sudo apt-get update
sudo apt-get install -y --no-install-recommends git ca-certificates build-essential \
  autoconf automake libtool pkg-config bison flex cmake ninja-build \
  python3 libasio-dev iproute2 iptables procps util-linux
mkdir -p "$PARENT_DIR"
clone_repo() {
  local dir="$1" url="$2" rev="$3"
  if [[ ! -d "$dir/.git" ]]; then
    # Publish the checkout only after a successful fetch, so retrying a failed
    # download never leaves an unusable dependency directory behind.
    (
      staging="$(mktemp -d "$PARENT_DIR/.hydro-clone.XXXXXX")"
      trap 'rm -rf -- "$staging"' EXIT
      git init -q "$staging"
      git -C "$staging" remote add origin "$url"
      git -C "$staging" fetch --depth 1 origin "$rev"
      git -C "$staging" checkout --detach FETCH_HEAD
      mv -T "$staging" "$dir"
    )
  fi
  # EtherCAT is the only OpenPLC submodule; it is not used by this runtime.
}
clone_repo "$EPYNET" "$EPYNET_URL" "$EPYNET_REV"
clone_repo "$OPENPLC" "$OPENPLC_URL" "$OPENPLC_REV"
clone_repo "$NS3" "$NS3_URL" "$NS3_REV"
# Protect shared compilation products against a concurrent run_all.
exec 8>"$NS3/.hydro-cps-run.lock"; flock -n 8 || fail 'ns-3 is in use'
exec 9>"$OPENPLC/.hydro-cps-run.lock"; flock -n 9 || fail 'OpenPLC is in use'
# Distinguish a missing environment from a broken existing one: never recreate
# an existing environment merely because its Python cannot start.
env_exists="$("$CONDA_BIN" env list --json | /usr/bin/python3 -c \
  'import json, pathlib, sys; print(int(any(pathlib.Path(p).name == sys.argv[1] for p in json.load(sys.stdin)["envs"])))' "$ENV_NAME")"
if [[ "$env_exists" == 0 ]]; then
  "$CONDA_BIN" create -y -n "$ENV_NAME" --override-channels -c conda-forge python=3.10 pip
fi
PYTHON_BIN="$("$CONDA_BIN" run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 10), "Python 3.10 is required"'
"$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt"
"$PYTHON_BIN" -m pip install --no-deps -e "$EPYNET"
"$PYTHON_BIN" -m pip check
# Native tools use system compilers/CMake, not an activated Conda toolchain.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONHOME PYTHONPATH CMAKE_PREFIX_PATH CONDA_BUILD_SYSROOT
(
  cd "$OPENPLC/utils/matiec_src"
  autoreconf -i
  ./configure
  make -j "$JOBS"
  cp iec2c "$OPENPLC/webserver/iec2c"
)
g++ "$OPENPLC/utils/st_optimizer_src/st_optimizer.cpp" -o "$OPENPLC/webserver/st_optimizer"
g++ -std=c++11 "$OPENPLC/utils/glue_generator_src/glue_generator.cpp" -o "$OPENPLC/webserver/core/glue_generator"
(
  cd "$OPENPLC/utils/libmodbus_src"
  ./autogen.sh
  ./configure
  make -j "$JOBS"
  sudo make install
)
cmake -S "$OPENPLC/utils/dnp3_src" -B "$OPENPLC/utils/dnp3_src/build-hydro" \
  -DCMAKE_BUILD_TYPE=Release -DDNP3_ALL=OFF -DWERROR=OFF
cmake --build "$OPENPLC/utils/dnp3_src/build-hydro" -j "$JOBS"
sudo cmake --install "$OPENPLC/utils/dnp3_src/build-hydro"
make -C "$OPENPLC/utils/snap7_src/build/linux" -j "$JOBS"
sudo make -C "$OPENPLC/utils/snap7_src/build/linux" install
sudo ldconfig
(
  cd "$OPENPLC/webserver/scripts"
  printf '\n' > ethercat
  bash change_hardware_layer.sh blank_linux
  bash compile_program.sh blank_program.st
)
(
  cd "$NS3"
  ./ns3 configure --build-profile=default --disable-examples --disable-tests \
    --disable-python-bindings --disable-sudo \
    '--enable-modules=core;network;internet;csma;bridge;tap-bridge;point-to-point'
  ./ns3 build -j "$JOBS"
)
# Preserve YAML comments, PLC mappings and experimental settings. Back up each
# changed file in ignored output/ before replacing only its five top-level paths.
"$PYTHON_BIN" - "$PROJECT_ROOT" "$PARENT_DIR" <<'PY'
import datetime, json, re, shutil, sys
from pathlib import Path
import yaml
root, parent = map(Path, sys.argv[1:])
backup = root / 'output' / 'install-backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
for path in sorted((root / 'examples' / 'c_town').glob('*.yaml')):
    original = path.read_text()
    cfg = yaml.safe_load(original)
    paths = dict(root_path=parent, openplc_path=parent/'OpenPLC_v3', ns3_path=parent/'ns-3-dev-git',
                 inp_file=path.parent/Path(cfg['inp_file']).name, output_path=path.parent/'output')
    updated = original
    for key, value in paths.items():
        updated, count = re.subn(r'^' + key + r':[^\n]*$', lambda _: f'{key}: {json.dumps(str(value))}', updated, flags=re.M)
        if count != 1:
            raise SystemExit(f'Expected one {key} in {path}')
    if updated != original:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup/path.name)
        path.write_text(updated)
from epynet.epanet2 import EPANET2
model = EPANET2()
# Upstream ENdeleteproject passes a pointer-to-pointer to a handle API.
# Use the C API directly for this short library-load smoke test.
import ctypes
model._lib.EN_deleteproject.argtypes = [ctypes.c_void_p]
if model._lib.EN_deleteproject(model.ph):
    raise SystemExit('EPANET project cleanup failed')
print('EPANET shared library loaded successfully.')
PY
(cd "$PROJECT_ROOT" && "$PYTHON_BIN" -m unittest discover -s tests)
printf '\nInstallation complete.\nconda activate %s\nbash scripts/run_all.sh --logic-wait 0.3\n' "$ENV_NAME"
