"""Install/remove a macOS launchd job that runs `youtify run` once a day.

launchd runs a missed calendar job when the Mac wakes up, so if the lid was closed at the scheduled
time the run happens the next time you open it. `caffeinate -i` keeps the Mac awake during the run.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.youtify.daily"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def build_plist(config_path: Path, logs_dir: Path, hour: int, minute: int) -> dict:
    path_env = ":".join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
                         str(Path(sys.executable).parent)])
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", sys.executable, "-m", "youtify",
                             "--config", str(config_path), "run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(logs_dir / "launchd.out.log"),
        "StandardErrorPath": str(logs_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": path_env},
        "ProcessType": "Background",
    }


def install(config_path: Path, logs_dir: Path, hour: int, minute: int) -> Path:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    uninstall(quiet=True)
    with PLIST.open("wb") as f:
        plistlib.dump(build_plist(config_path, logs_dir, hour, minute), f)
    domain = f"gui/{os.getuid()}"
    r = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["launchctl", "load", "-w", str(PLIST)], check=True)
    return PLIST


def uninstall(quiet: bool = False) -> None:
    if PLIST.exists():
        domain = f"gui/{os.getuid()}"
        r = subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        PLIST.unlink()
    elif not quiet:
        print("No schedule installed.")
