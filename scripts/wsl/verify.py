"""Verify this workstation's local deterministic HFlow environment; no model API calls."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    root = Path(os.environ["HFLOW_WSL_ROOT"])
    source = Path(os.environ["HFLOW_WINDOWS_SOURCE"])
    report = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "status": "running",
        "windows_source": str(source),
        "linux_repository": str(Path.cwd()),
        "data_root": os.environ["HFLOW_DATA_ROOT"],
        "cache_root": os.environ["XDG_CACHE_HOME"],
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "source_commit": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
        "packages": {
            name: version(name)
            for name in ["hflow", "mcap", "mcap-ros2-support", "numpy", "duckdb"]
        },
        "real_recordings_processed": False,
        "model_api_calls": False,
    }
    try:
        import fcntl  # noqa: F401

        from hflow.ffmpeg import ffmpeg_path, ffmpeg_version, ffprobe_path, ffprobe_version

        print("Resolving the repository-pinned FFmpeg/FFprobe pair...", flush=True)
        report["ffmpeg_path"] = str(ffmpeg_path())
        report["ffmpeg_version"] = ffmpeg_version()
        report["ffprobe_path"] = str(ffprobe_path())
        report["ffprobe_version"] = ffprobe_version()
        tool_bin = root / "bin"
        tool_bin.mkdir(exist_ok=True)
        for name in ["ffmpeg", "ffprobe"]:
            link = tool_bin / name
            target = Path(str(report[f"{name}_path"]))
            if link.is_symlink():
                if link.resolve() != target.resolve():
                    raise RuntimeError(f"Tool link points to an unexpected binary: {link}")
            elif link.exists():
                raise RuntimeError(f"Refusing to replace an existing tool: {link}")
            else:
                link.symlink_to(target)
        subprocess.run([sys.executable, "-m", "hflow.cli", "--help"], check=True)
        print("Running the bundled synthetic quickstart; no real MCAP inputs...", flush=True)
        subprocess.run([sys.executable, "examples/quickstart.py"], check=True)
        report["synthetic_quickstart"] = "passed"
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        for folder in [root / "logs", source / "data/local-wsl-environment"]:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "environment.json"
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Environment report: {target}", flush=True)


if __name__ == "__main__":
    main()
