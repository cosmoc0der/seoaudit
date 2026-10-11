from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

_PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


class LighthouseError(RuntimeError):
    pass


class QuotaError(LighthouseError):
    pass


class LighthouseUnavailable(LighthouseError):
    pass


@dataclass
class Measurement:
    lighthouse: dict[str, Any]
    field: dict[str, Any] = field(default_factory=dict)


class Runner(Protocol):
    name: str
    concurrency: int
    has_field_data: bool

    async def run(self, url: str, strategy: str) -> Measurement: ...


class PsiRunner:
    name = "PageSpeed API"
    has_field_data = True

    def __init__(self, client: httpx.AsyncClient, api_key: str | None, lang: str, timeout: float, concurrency: int):
        self._client = client
        self._api_key = api_key
        self._lang = lang
        self._timeout = timeout
        self.concurrency = concurrency

    async def run(self, url: str, strategy: str) -> Measurement:
        params = [("url", url), ("strategy", strategy), ("locale", self._lang), ("category", "performance"), ("category", "seo")]
        if self._api_key:
            params.append(("key", self._api_key))
        try:
            response = await self._client.get(_PSI_API, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise LighthouseError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            try:
                message = response.json()["error"]["message"]
            except Exception:
                message = response.text[:200]
            error = QuotaError if response.status_code == 429 else LighthouseError
            raise error(f"HTTP {response.status_code}: {message}")
        data = response.json()
        return Measurement(data.get("lighthouseResult") or {}, data.get("loadingExperience") or {})


class LocalRunner:
    name = "Lighthouse"
    has_field_data = False
    # Parallel runs compete for CPU and skew lab metrics, so measurements go one at a time.
    concurrency = 1

    def __init__(self, node: str, cli: Path, chrome_path: str | None, chrome_flags: str, lang: str, timeout: float):
        self._node = node
        self._cli = cli
        self._chrome_path = chrome_path
        self._chrome_flags = _with_headless(chrome_path, chrome_flags)
        self._lang = lang
        self._timeout = timeout

    async def run(self, url: str, strategy: str) -> Measurement:
        args = [
            url,
            "--output=json",
            "--output-path=stdout",
            "--quiet",
            "--only-categories=performance,seo",
            f"--locale={self._lang}",
            f"--chrome-flags={self._chrome_flags}",
        ]
        if strategy == "desktop":
            args.append("--preset=desktop")
        env = {**os.environ, "CHROME_PATH": self._chrome_path} if self._chrome_path else None
        stdout, stderr, code = await _run_process([self._node, str(self._cli), *args], env, self._timeout)
        try:
            return Measurement(json.loads(stdout))
        except json.JSONDecodeError:
            tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise LighthouseError(f"exit code {code}: {' '.join(tail) or 'no output'}") from None


def _with_headless(chrome_path: str | None, flags: str) -> str:
    # chrome-headless-shell is headless by itself, and --headless=new there breaks viewport
    # emulation: the whole body registers as a layout shift and CLS jumps above 1.
    if "headless-shell" in Path(chrome_path or "").name or "--headless" in flags:
        return flags
    return f"--headless=new {flags}".strip()


async def create_local_runner(options: dict[str, Any], lang: str) -> LocalRunner:
    node = shutil.which("node")
    if not node:
        raise LighthouseUnavailable("Node.js not found")
    cli = Path(options["lighthouse"]) if options.get("lighthouse") else await _find_or_install_lighthouse(
        node, options["lighthouse_version"]
    )
    chrome = options.get("chrome_path") or os.environ.get("CHROME_PATH") or await _playwright_chromium()
    return LocalRunner(node, cli, chrome, options["chrome_flags"], lang, options["timeout"])


def node_available() -> bool:
    return shutil.which("node") is not None


async def _find_or_install_lighthouse(node: str, version: str) -> Path:
    if cli := _npm_script("lighthouse", "lighthouse/cli/index.js"):
        return cli
    prefix = Path(os.environ.get("SEOAUDIT_CACHE") or Path.home() / ".cache" / "seoaudit") / "lighthouse"
    cli = prefix / "node_modules" / "lighthouse" / "cli" / "index.js"
    if cli.is_file():
        return cli
    npm = _npm_script("npm", "npm/bin/npm-cli.js")
    if not npm:
        raise LighthouseUnavailable("npm not found")
    log.warning("Installing lighthouse@%s into %s", version, prefix)
    prefix.mkdir(parents=True, exist_ok=True)
    _, stderr, code = await _run_process(
        [node, str(npm), "install", "--prefix", str(prefix), "--no-audit", "--no-fund", f"lighthouse@{version}"],
        None,
        600,
    )
    if code != 0 or not cli.is_file():
        raise LighthouseUnavailable(f"npm install lighthouse failed: {stderr.decode(errors='replace')[-300:]}")
    return cli


def _npm_script(name: str, script: str) -> Path | None:
    """Locates the JS entry of an npm binary so it runs via node directly: Windows .cmd shims
    go through cmd.exe, which would interpret `&` and other characters in crawled URLs."""
    found = shutil.which(name)
    if not found:
        return None
    path = Path(found).resolve()
    if path.suffix.lower() == ".js":
        return path
    candidate = path.parent / "node_modules" / script
    return candidate if candidate.is_file() else None


async def _playwright_chromium() -> str | None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as pw:
            chromium = Path(pw.chromium.executable_path)
    except Exception:
        return None
    # The headless shell is what Playwright itself runs headless; it is lighter and is often
    # the only build installed (`playwright install --only-shell`).
    revision = chromium.parents[1].name.rsplit("-", 1)[-1]
    shells = sorted(chromium.parents[2].glob(f"chromium_headless_shell-{revision}/*/chrome-headless-shell*"))
    for candidate in (*shells, chromium):
        if candidate.is_file():
            return str(candidate)
    return None


async def _run_process(cmd: list[str], env: dict[str, str] | None, timeout: float) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        raise LighthouseError(f"timeout after {timeout:.0f} s") from None
    return stdout, stderr, proc.returncode


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Killing only node would leave the Chrome it spawned running."""
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
