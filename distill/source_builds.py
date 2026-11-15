"""Building an emulator from source, for the projects that ship no Linux binary of their own.

Only ares needs this today. The idea is one button, one script: it works out which distribution family it is running
on, installs that family's build dependencies, checks out the newest release tag, compiles, and copies the finished
program into the emulator's folder so Distill's usual "find the executable" step picks it up.

The script runs in a terminal window of its own rather than inside Distill, for two reasons: installing packages asks
for the administrator password (nothing in Distill may own a password prompt), and a full build takes long enough that
it should not look like a frozen window.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SCRIPT_NAME = "build-from-source.sh"

# How each distribution family installs packages, and the name it gives the pieces this build needs.
RECIPES: dict[str, dict] = {
    "ares": {
        "title": "ares",
        "repo": "https://github.com/ares-emulator/ares",
        "min_cmake": (3, 28),
        "deps": {
            # ares builds against GTK3 by default (Qt6 is experimental), plus X11/GL and the audio drivers.
            "debian": ("sudo apt update && sudo apt install -y build-essential cmake pkg-config curl git ninja-build "
                       "libgtk-3-dev libcanberra-gtk3-module libgl-dev libasound2-dev libao-dev libopenal-dev "
                       "libpulse-dev libudev-dev"),
            "fedora": ("sudo dnf install -y cmake pkg-config curl git gcc gcc-c++ ninja-build gtk3-devel libX11-devel "
                       "libXrandr-devel mesa-libGL-devel openal-soft-devel alsa-lib-devel pulseaudio-libs-devel "
                       "libao-devel"),
            "arch": ("sudo pacman -S --needed --noconfirm base-devel cmake ninja pkgconf git curl gtk3 libcanberra "
                     "libx11 libxrandr mesa openal alsa-lib libpulse libao"),
        },
        # SDL3 is optional and not packaged on older releases; trying it must not sink the whole install.
        "optional": {
            "debian": "sudo apt install -y libsdl3-dev || true",
            "fedora": "sudo dnf install -y SDL3-devel || true",
            "arch": "sudo pacman -S --needed --noconfirm sdl3 || true",
        },
        "cmake_args": ("-G Ninja -DCMAKE_BUILD_TYPE=Release -DARES_BUILD_OFFICIAL=ON -DENABLE_IPO=OFF "
                       "-DARES_BUILD_LOCAL=OFF"),
        "output": "rundir",
        "binary": "bin/ares",
        "commands": ("cmake", "ninja", "gcc", "g++", "pkg-config", "git", "curl"),
        "pkg_config": ("gtk+-3.0", "x11", "xrandr", "gl", "openal", "alsa", "libpulse", "ao"),
        "notes": "GTK3 is required. If CMake is older than 3.28 the script stops and says so.",
    },
}


def family_of(os_release: str) -> str:
    """'debian', 'fedora', 'arch' or '' from the text of /etc/os-release."""
    text = os_release.lower()
    ids = re.findall(r"^(?:id|id_like)=(.*)$", text, re.MULTILINE)  # text is lowercased above
    words = {w.strip().strip('"') for chunk in ids for w in chunk.split()}
    for family, members in (("arch", {"arch", "archlinux", "manjaro", "endeavouros", "cachyos"}),
                            ("debian", {"debian", "ubuntu", "linuxmint", "pop", "zorin", "elementary", "kali"}),
                            ("fedora", {"fedora", "rhel", "centos", "rocky", "almalinux", "bazzite", "nobara"})):
        if words & members:
            return family
    return ""


def this_family() -> str:
    try:
        return family_of(Path("/etc/os-release").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def script_for(key: str, target: Path, version: str = "") -> str:
    """The shell script that builds this project into `target`, for the distribution it is run on."""
    recipe = RECIPES[key]
    version = "" if version == "source" else version
    return f"""#!/bin/bash
# Builds {recipe['title']} from source for this distribution, then puts it where Distill looks for it.
# Written by Distill. Safe to read; nothing here is hidden.
set -u

TARGET={shell_quote(str(target))}
REPO={shell_quote(recipe['repo'])}
WANT={shell_quote(version)}
SRC="$TARGET/src"
BUILD="$TARGET/build"

say() {{ printf '\\n== %s\\n' "$*"; }}
die() {{ printf '\\n!! %s\\n' "$*"; printf '\\nPress Enter to close.\\n'; read -r _; exit 1; }}

# ---- which distribution is this? -------------------------------------------------------------
FAMILY=""
. /etc/os-release 2>/dev/null || true
for word in ${{ID:-}} ${{ID_LIKE:-}}; do
  case "$word" in
    arch|archlinux|manjaro|endeavouros|cachyos) FAMILY=arch ;;
    debian|ubuntu|linuxmint|pop|zorin|elementary|kali) FAMILY=debian ;;
    fedora|rhel|centos|rocky|almalinux|bazzite|nobara) FAMILY=fedora ;;
  esac
  [ -n "$FAMILY" ] && break
done
[ -n "$FAMILY" ] || die "This doesn't look like a Debian, Fedora or Arch system, so the package names are unknown."
say "{recipe['title']}: building on ${{PRETTY_NAME:-$FAMILY}}"

# Atomic Fedora uses already-layered dependencies; installing them requires a separate reboot.
ATOMIC=0
if [ "$FAMILY" = fedora ] && [ -e /run/ostree-booted ]; then
  ATOMIC=1
  missing=""
  for tool in {' '.join(recipe['commands'])}; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if command -v pkg-config >/dev/null 2>&1; then
    for module in {' '.join(recipe['pkg_config'])}; do
      pkg-config --exists "$module" || missing="$missing $module"
    done
  fi
  if [ -n "$missing" ]; then
    say "This atomic Fedora system needs more layered build dependencies:$missing"
    echo "Run this, reboot, then press the button again:"
    echo
    echo "  sudo rpm-ostree install cmake ninja-build gcc gcc-c++ pkg-config git curl gtk3-devel libX11-devel libXrandr-devel mesa-libGL-devel openal-soft-devel alsa-lib-devel pulseaudio-libs-devel libao-devel"
    die "Missing dependencies; nothing was changed."
  fi
  say "Using the build dependencies already layered on this atomic Fedora system."
fi

# ---- dependencies ---------------------------------------------------------------------------
if [ "$ATOMIC" = 0 ]; then
say "Installing build dependencies (this asks for your password)"
case "$FAMILY" in
  debian) {recipe['deps']['debian']} ;;
  fedora) {recipe['deps']['fedora']} ;;
  arch)   {recipe['deps']['arch']} ;;
esac || die "Installing the build dependencies failed."

# Optional pieces are tried and forgotten: SDL3 is missing from older releases, and ares works without it.
case "$FAMILY" in
  debian) {recipe['optional']['debian']} ;;
  fedora) {recipe['optional']['fedora']} ;;
  arch)   {recipe['optional']['arch']} ;;
esac
fi

need=$(cmake --version 2>/dev/null | head -n1 | awk '{{print $3}}')
[ -n "$need" ] || die "CMake is not installed."
if [ "$(printf '%s\\n' {recipe['min_cmake'][0]}.{recipe['min_cmake'][1]} "$need" | sort -V | head -n1)" != \\
     "{recipe['min_cmake'][0]}.{recipe['min_cmake'][1]}" ]; then
  die "CMake $need is too old: {recipe['title']} needs {recipe['min_cmake'][0]}.{recipe['min_cmake'][1]} or newer. \\
Install a newer CMake from cmake.org, then press the button again."
fi

# ---- source ---------------------------------------------------------------------------------
mkdir -p "$TARGET" || die "Could not create $TARGET"
if [ -d "$SRC/.git" ]; then
  say "Updating the source"
else
  say "Fetching the source"
  git clone --depth 1 "$REPO" "$SRC" || die "Could not clone $REPO."
fi
cd "$SRC" || die "Could not enter $SRC"
git fetch --tags --depth 1 origin '+refs/heads/*:refs/remotes/origin/*' || die "Could not fetch the source."
if [ -n "$WANT" ]; then
  REF="refs/tags/$WANT"
else
  git remote set-head origin --auto || die "Could not determine the remote default branch."
  REF=$(git symbolic-ref --quiet refs/remotes/origin/HEAD) || die "Could not resolve the remote default branch."
fi
COMMIT=$(git rev-parse --verify "$REF^{{commit}}") || die "Could not resolve source target $REF."
[ -n "$COMMIT" ] || die "Source target $REF has no commit."
git checkout --detach "$COMMIT" || die "Could not check out source target $REF."
HEAD_COMMIT=$(git rev-parse --verify HEAD) || die "Could not verify the source checkout."
[ "$HEAD_COMMIT" = "$COMMIT" ] || die "Source HEAD does not match the resolved target $REF."
say "Building $REF ($COMMIT)"

# ---- build ----------------------------------------------------------------------------------
say "Configuring"
cmake -S "$SRC" -B "$BUILD" {recipe['cmake_args']} || die "CMake could not configure the build."
say "Compiling (this is the slow part; a fast machine takes a few minutes)"
cmake --build "$BUILD" -j "$(nproc)" || die "The build failed. The output above says why."

# ---- put it where Distill looks -------------------------------------------------------------
OUT="$TARGET/build/{recipe['output']}"
[ -d "$OUT" ] || die "The build finished but {recipe['output']} is missing."
say "Copying the built program into $TARGET"
cp -a "$OUT"/. "$TARGET"/ || die "Could not copy the build."
[ -x "$TARGET/{recipe['binary']}" ] || die "The built program is not where it was expected ({recipe['binary']})."
say "Done. {recipe['title']} is at $TARGET/{recipe['binary']} - go back to Distill and click 'Find installed'."
printf '\\nPress Enter to close.\\n'
read -r _
"""


def shell_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


TERMINALS = (
    ("konsole", ["konsole", "-e", "bash", "-c"]),
    ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c"]),
    ("kgx", ["kgx", "--", "bash", "-c"]),
    ("xfce4-terminal", ["xfce4-terminal", "-x", "bash", "-c"]),
    ("mate-terminal", ["mate-terminal", "-x", "bash", "-c"]),
    ("tilix", ["tilix", "-e", "bash", "-c"]),
    ("kitty", ["kitty", "bash", "-c"]),
    ("alacritty", ["alacritty", "-e", "bash", "-c"]),
    ("wezterm", ["wezterm", "start", "--", "bash", "-c"]),
    ("foot", ["foot", "bash", "-c"]),
    ("xterm", ["xterm", "-e", "bash", "-c"]),
)


def terminal_command(script: Path) -> list[str] | None:
    """A command that opens `script` in whatever terminal this desktop has, or None when there isn't one."""
    inner = f"bash {shell_quote(script)}"
    for name, argv in TERMINALS:
        if shutil.which(name):
            return [*argv, inner]
    return None


def write_script(key: str, target: Path, version: str = "") -> Path:
    """Write the build script into the emulator's folder and make it runnable."""
    target.mkdir(parents=True, exist_ok=True)
    path = target / SCRIPT_NAME
    path.write_text(script_for(key, target, version), encoding="utf-8")
    path.chmod(0o755)
    return path


def start(key: str, target: Path, version: str = "") -> Path:
    """Write the script and open it in a terminal. Returns the script; raises RuntimeError with a readable reason."""
    script = write_script(key, target, version)
    argv = terminal_command(script)
    if argv is None:
        raise RuntimeError(f"No terminal program was found to run the build in. The script is saved at:\\n\\n{script}")
    subprocess.Popen(argv, cwd=str(target), close_fds=True, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return script
