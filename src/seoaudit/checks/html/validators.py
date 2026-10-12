from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx

from seoaudit.core.tools import cache_dir, run_process

log = logging.getLogger(__name__)

_VNU_RELEASE = "https://github.com/validator/validator/releases/download/latest/"
_VNU_ZIPS = {"win32": "vnu.windows.zip", "darwin": "vnu.osx.zip", "linux": "vnu.linux.zip"}
_VNU_MAIN = "vnu/nu.validator.client.SimpleCommandLineValidator"
_VNU_IMAGE = "vnu-runtime-image"
_LOCAL_TIMEOUT = 600
_ONLINE_PAGES = 5

Results = dict[str, list[dict[str, Any]]]


class ValidatorUnavailable(RuntimeError):
    pass


class Validator(Protocol):
    name: str
    max_pages: int | None

    def add(self, url: str, html: str) -> None: ...

    async def run(self) -> tuple[Results, list[str]]:
        """Messages per validated URL, plus errors for the pages that could not be validated."""


class OnlineValidator:
    def __init__(self, client: httpx.AsyncClient, endpoint: str, delay: float, max_pages: int | None):
        self.name = endpoint
        self.max_pages = _ONLINE_PAGES if max_pages is None else max_pages
        self._client = client
        self._endpoint = endpoint
        self._delay = delay
        self._docs: list[tuple[str, str]] = []

    def add(self, url: str, html: str) -> None:
        self._docs.append((url, html))

    async def run(self) -> tuple[Results, list[str]]:
        from seoaudit import __version__

        results: Results = {}
        errors: list[str] = []
        for index, (url, html) in enumerate(self._docs):
            if index:
                await asyncio.sleep(self._delay)
            try:
                response = await self._client.post(
                    self._endpoint,
                    params={"out": "json"},
                    content=html.encode("utf-8"),
                    # validator.w3.org sits behind a bot challenge that answers 403 to the crawler's browser-like UA
                    headers={"Content-Type": "text/html; charset=utf-8", "User-Agent": f"seoaudit/{__version__}"},
                    timeout=60,
                )
                response.raise_for_status()
                results[url] = response.json().get("messages", [])
            except Exception as exc:
                errors.append(f"{url}: {exc!r}")
        return results, errors


class LocalValidator:
    """Nu Html Checker (the engine behind validator.w3.org) run once over all collected pages."""

    name = "vnu"

    def __init__(self, command: list[str], max_pages: int | None):
        self.max_pages = max_pages
        self._command = command
        # Pages go to disk while crawling, so a large site doesn't keep every body in memory.
        self._dir = tempfile.TemporaryDirectory(prefix="seoaudit-vnu-")
        self._urls: dict[str, str] = {}

    def add(self, url: str, html: str) -> None:
        name = f"{len(self._urls)}.html"
        Path(self._dir.name, name).write_text(html, encoding="utf-8", newline="")
        self._urls[name] = url

    async def run(self) -> tuple[Results, list[str]]:
        try:
            # A directory instead of file names keeps the command line short on Windows.
            cmd = [*self._command, "--format", "json", "--stdout", "--exit-zero-always", self._dir.name]
            try:
                stdout, stderr, code = await run_process(cmd, None, _LOCAL_TIMEOUT)
            except TimeoutError as exc:
                raise ValidatorUnavailable(f"vnu: {exc}") from None
            try:
                messages = json.loads(stdout)["messages"]
            except (json.JSONDecodeError, KeyError):
                tail = " ".join(stderr.decode(errors="replace").strip().splitlines()[-3:])
                raise ValidatorUnavailable(f"vnu exit code {code}: {tail or 'no output'}") from None
        finally:
            self._dir.cleanup()

        results: Results = {url: [] for url in self._urls.values()}
        errors: list[str] = []
        for message in messages:
            url = self._urls.get(unquote(urlsplit(message.get("url", "")).path).rsplit("/", 1)[-1])
            if url is None or url not in results:
                continue
            if message.get("type") == "non-document-error":
                errors.append(f"{url}: {message.get('message')}")
                del results[url]
            else:
                results[url].append(message)
        return results, errors


async def create_validator(options: dict[str, Any], client: httpx.AsyncClient, diagnostics: list[str]) -> Validator:
    mode = options["validator"]
    max_pages = options["validator_pages"]
    if mode != "online":
        try:
            return LocalValidator(await _vnu_command(options.get("vnu")), max_pages)
        except ValidatorUnavailable as exc:
            if mode == "local":
                raise
            diagnostics.append(f"html: local validator unavailable, using {options['validator_url']}: {exc}")
    return OnlineValidator(client, options["validator_url"], options["validator_delay"], max_pages)


async def _vnu_command(path: str | None) -> list[str]:
    target = Path(path) if path else cache_dir("vnu") / _VNU_IMAGE
    if target.suffix == ".jar":
        java = shutil.which("java")
        if not java:
            raise ValidatorUnavailable("Java not found for vnu.jar")
        return [java, "-jar", str(target)]
    java = target / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    if not java.is_file():
        if path:
            raise ValidatorUnavailable(f"vnu runtime image not found at {target}")
        await _install_vnu(target)
    return [str(java), "-m", _VNU_MAIN]


async def _install_vnu(image: Path) -> None:
    archive = _VNU_ZIPS.get(sys.platform)
    if not archive:
        raise ValidatorUnavailable(f"no vnu build for {sys.platform}")
    log.warning("Downloading %s into %s", archive, image.parent)
    image.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as client:
            expected = (await client.get(_VNU_RELEASE + archive + ".sha1")).raise_for_status().text.split()[0]
            with tempfile.TemporaryFile() as file:
                digest = hashlib.sha1()
                async with client.stream("GET", _VNU_RELEASE + archive) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        digest.update(chunk)
                        file.write(chunk)
                if digest.hexdigest() != expected:
                    raise ValidatorUnavailable(f"{archive}: checksum mismatch")
                file.seek(0)
                await asyncio.to_thread(_extract, file, image)
    except httpx.HTTPError as exc:
        raise ValidatorUnavailable(f"download of {archive} failed: {exc!r}") from exc


def _extract(file, image: Path) -> None:
    # Unpacked next to the target and renamed at the end, so an interrupted install is never picked up.
    staging = Path(tempfile.mkdtemp(dir=image.parent))
    try:
        with zipfile.ZipFile(file) as zf:
            zf.extractall(staging)
            # zipfile drops the executable bit that bin/java needs outside Windows
            for info in zf.infolist():
                if mode := info.external_attr >> 16:
                    os.chmod(staging / info.filename, mode)
        (staging / _VNU_IMAGE).rename(image)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
