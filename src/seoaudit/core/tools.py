from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


def cache_dir(name: str) -> Path:
    return Path(os.environ.get("SEOAUDIT_CACHE") or Path.home() / ".cache" / "seoaudit") / name


async def run_process(cmd: list[str], env: dict[str, str] | None, timeout: float) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill_tree(proc)
        raise TimeoutError(f"timeout after {timeout:.0f} s") from None
    return stdout, stderr, proc.returncode


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Killing only the direct child would leave processes it spawned (e.g. Chrome) running."""
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    await proc.wait()
