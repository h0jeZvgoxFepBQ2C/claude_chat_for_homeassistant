"""Claude Code CLI binary management and native account authentication.

Mirrors how T3 Code authenticates with Claude Pro/Max/Team subscriptions:
instead of calling Anthropic's raw Messages API with consumer OAuth tokens
(which Anthropic blocks for third-party clients), we run the official Claude
Code CLI binary with its native authentication (`claude auth login` or
`CLAUDE_CODE_OAUTH_TOKEN`).

On Home Assistant OS / Container (Alpine Linux / musl), PyPI wheels for
`claude-agent-sdk` don't ship a musl binary, so we locate an existing `claude`
executable or download the official platform binary (including
`linux-x64-musl` and `linux-arm64-musl`) directly from Anthropic's release
host with SHA-256 verification.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import platform
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CLI_DIR_NAME

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[^\s]+)?$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

# Matches OSC 8 hyperlinks, other OSC sequences, and CSI ANSI escape sequences.
_ANSI_RE = re.compile(
    r"\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
)
_AUTH_URL_RE = re.compile(
    r"https://(?:claude\.com|claude\.ai|console\.anthropic\.com)/[^\s\"'<>)\]]+"
)


class ClaudeCliError(Exception):
    """Base error for Claude Code CLI setup or execution failures."""


class ClaudeCliDownloadError(ClaudeCliError):
    """Could not download or verify the Claude Code CLI binary."""


class ClaudeCliAuthError(ClaudeCliError):
    """Claude Code CLI authentication failed or is not logged in."""


def default_cli_dir(hass: HomeAssistant) -> Path:
    """Return the persistent directory under HA's config dir for CLI data."""
    return Path(hass.config.path(CLI_DIR_NAME))


def default_cli_binary_path(hass: HomeAssistant) -> Path:
    """Return the default managed binary path under HA's config dir."""
    return default_cli_dir(hass) / "claude"


def default_claude_config_dir(hass: HomeAssistant) -> Path:
    """Return the isolated CLAUDE_CONFIG_DIR managed by this integration."""
    return default_cli_dir(hass) / "config"


def _is_musl_linux() -> bool:
    """Detect whether the current Linux userland uses musl libc (e.g. Alpine/HAOS)."""
    if os.path.exists("/lib/libc.musl-x86_64.so.1") or os.path.exists(
        "/lib/libc.musl-aarch64.so.1"
    ):
        return True
    libc_name, _ = platform.libc_ver()
    if libc_name and "musl" in libc_name.lower():
        return True
    if not libc_name:
        # On Alpine Linux, platform.libc_ver() often returns ('', '').
        # Check for /etc/alpine-release or musl dynamic linker under /lib.
        if os.path.exists("/etc/alpine-release"):
            return True
        try:
            for name in os.listdir("/lib"):
                if name.startswith("ld-musl-"):
                    return True
        except OSError:
            pass
    return False


def detect_platform() -> str:
    """Return Anthropic's release platform identifier for this host."""
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise ClaudeCliDownloadError(f"Unsupported CPU architecture: {machine}")

    if sys_name == "darwin":
        return f"darwin-{arch}"
    if sys_name == "linux":
        suffix = "-musl" if _is_musl_linux() else ""
        return f"linux-{arch}{suffix}"

    raise ClaudeCliDownloadError(f"Unsupported operating system: {sys_name}")


def _is_executable(path: str | Path | None) -> bool:
    if not path:
        return False
    p = Path(path)
    return p.is_file() and os.access(p, os.X_OK)


def find_cli_binary(
    hass: HomeAssistant, configured_path: str | None = None
) -> str | None:
    """Locate an executable `claude` CLI binary without downloading anything."""
    if configured_path:
        expanded = os.path.expanduser(configured_path.strip())
        if _is_executable(expanded):
            return expanded
        resolved = shutil.which(expanded)
        if resolved and _is_executable(resolved):
            return resolved
        return None

    managed = default_cli_binary_path(hass)
    if _is_executable(managed):
        return str(managed)

    for candidate in (
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
    ):
        if candidate and _is_executable(candidate):
            return candidate

    return None


def build_cli_env(
    *,
    config_dir: str | Path | None = None,
    oauth_token: str | None = None,
    headless: bool = False,
) -> dict[str, str]:
    """Build a clean environment dict for running the Claude Code CLI.

    Always strips ANTHROPIC_API_KEY so an ambient console key does not silently
    override subscription OAuth authentication.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    if config_dir:
        cfg_path = Path(os.path.expanduser(str(config_dir)))
        cfg_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(cfg_path, 0o700)
        except OSError:
            pass
        env["CLAUDE_CONFIG_DIR"] = str(cfg_path)

    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token.strip()

    # Keep the CLI predictable inside minimal/Alpine HA containers.
    env["USE_BUILTIN_RIPGREP"] = "0"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    if headless:
        # Force the remote copy-paste OAuth callback URL
        # (https://platform.claude.com/oauth/code/callback) rather than trying
        # to launch a local browser or bind a localhost-only callback.
        env["BROWSER"] = "false"
        env["DISPLAY"] = ""
        env["WAYLAND_DISPLAY"] = ""
        env["SSH_CONNECTION"] = "127.0.0.1 1 127.0.0.1 22"
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"

    return env


async def async_verify_cli_binary(cli_path: str) -> str:
    """Run `claude --version` to verify the binary executes on this host."""
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_cli_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except OSError as err:
        raise ClaudeCliDownloadError(
            f"Claude CLI binary at {cli_path} could not be executed: {err}"
        ) from err
    except TimeoutError as err:
        raise ClaudeCliDownloadError(
            f"Claude CLI binary at {cli_path} timed out during version check"
        ) from err

    if proc.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        raise ClaudeCliDownloadError(
            f"Claude CLI exited with code {proc.returncode}: {detail}"
        )

    return stdout.decode("utf-8", errors="replace").strip()


async def async_ensure_cli_binary(
    hass: HomeAssistant,
    configured_path: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Return a working `claude` CLI binary path, downloading it if needed."""
    existing = await hass.async_add_executor_job(
        find_cli_binary, hass, configured_path
    )
    if existing:
        await async_verify_cli_binary(existing)
        return existing

    if configured_path:
        raise ClaudeCliDownloadError(
            f"Configured Claude CLI binary not found or not executable: {configured_path}"
        )

    plat = await hass.async_add_executor_job(detect_platform)
    http = session or async_get_clientsession(hass)

    _LOGGER.info("Downloading official Claude Code CLI for platform %s", plat)
    try:
        async with http.get(
            f"{DOWNLOAD_BASE_URL}/latest",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            version = (await resp.text()).strip()
    except Exception as err:  # noqa: BLE001
        raise ClaudeCliDownloadError(
            f"Could not fetch latest Claude Code version: {err}"
        ) from err

    if not _VERSION_RE.match(version):
        raise ClaudeCliDownloadError(
            f"Unexpected Claude Code version response: {version[:80]!r}"
        )

    try:
        async with http.get(
            f"{DOWNLOAD_BASE_URL}/{version}/manifest.json",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            manifest = await resp.json(content_type=None)
    except Exception as err:  # noqa: BLE001
        raise ClaudeCliDownloadError(
            f"Could not fetch Claude Code release manifest: {err}"
        ) from err

    platform_info = (manifest.get("platforms") or {}).get(plat)
    expected_sha = (
        platform_info.get("checksum") if isinstance(platform_info, dict) else None
    )
    if not expected_sha or not _SHA256_RE.match(expected_sha):
        raise ClaudeCliDownloadError(
            f"Platform {plat!r} not available in Claude Code release {version}"
        )

    cli_dir = default_cli_dir(hass)
    target = default_cli_binary_path(hass)
    await hass.async_add_executor_job(
        lambda: cli_dir.mkdir(parents=True, exist_ok=True)
    )

    tmp_fd, tmp_name = await hass.async_add_executor_job(
        lambda: tempfile.mkstemp(prefix=".claude-dl-", dir=str(cli_dir))
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        hasher = hashlib.sha256()
        binary_url = f"{DOWNLOAD_BASE_URL}/{version}/{plat}/claude"
        async with http.get(
            binary_url,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as out_f:
                async for chunk in resp.content.iter_chunked(128 * 1024):
                    hasher.update(chunk)
                    out_f.write(chunk)

        actual_sha = hasher.hexdigest()
        if actual_sha != expected_sha:
            raise ClaudeCliDownloadError(
                f"Checksum mismatch for Claude Code {version} ({plat}): "
                f"expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
            )

        await hass.async_add_executor_job(_finalize_binary, tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    await async_verify_cli_binary(str(target))
    _LOGGER.info("Installed Claude Code CLI %s to %s", version, target)
    return str(target)


def _finalize_binary(tmp_path: Path, target: Path) -> None:
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, target)


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences (CSI and OSC hyperlinks)."""
    return _ANSI_RE.sub("", text)


def extract_auth_url(raw_output: str) -> str | None:
    """Extract a complete Claude OAuth authorize URL from `claude auth login` output.

    Requires trailing whitespace after the URL so a mid-chunk partial read is
    never mistaken for the complete URL.
    """
    cleaned = strip_ansi(raw_output)
    for match in _AUTH_URL_RE.finditer(cleaned):
        # Ensure the URL has terminated (followed by whitespace, newline, etc.)
        if match.end() >= len(cleaned) or not cleaned[match.end()].isspace():
            continue
        url = match.group(0).rstrip(".,;:")
        if "oauth" in url or "authorize" in url:
            return url
    return None


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object from CLI stdout even if wrapped with noise."""
    cleaned = strip_ansi(raw).strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


async def async_check_auth_status(
    cli_path: str,
    *,
    config_dir: str | Path | None = None,
    oauth_token: str | None = None,
) -> dict[str, Any]:
    """Run `claude auth status --json` and return the parsed status dict."""
    env = build_cli_env(config_dir=config_dir, oauth_token=oauth_token)
    proc = await asyncio.create_subprocess_exec(
        cli_path,
        "auth",
        "status",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    parsed = _extract_json_object(stdout.decode("utf-8", errors="replace"))
    if parsed is not None:
        return parsed

    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return {"loggedIn": False, "authMethod": "none", "error": detail}


async def async_validate_cli_auth(
    cli_path: str,
    *,
    config_dir: str | Path | None = None,
    oauth_token: str | None = None,
) -> None:
    """Verify CLI credentials actually work against Claude's backend.

    Used during setup to validate a pasted CLAUDE_CODE_OAUTH_TOKEN (since
    `claude auth status` only checks token presence without hitting the API).
    """
    env = build_cli_env(config_dir=config_dir, oauth_token=oauth_token)
    proc = await asyncio.create_subprocess_exec(
        cli_path,
        "-p",
        "--output-format",
        "json",
        "--model",
        "haiku",
        "--no-session-persistence",
        "--tools",
        "",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-chrome",
        "--max-turns",
        "1",
        "Reply with ok",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except TimeoutError as err:
        proc.kill()
        await proc.wait()
        raise ClaudeCliError("Timed out verifying Claude credentials") from err

    out_text = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace")
    parsed = _extract_json_object(out_text)

    if proc.returncode == 0 and (not parsed or not parsed.get("is_error")):
        return

    detail = ""
    if parsed:
        detail = str(
            parsed.get("result")
            or parsed.get("error")
            or (parsed.get("errors") or [""])[0]
            or ""
        )
    if not detail:
        detail = (err_text or out_text).strip()

    lowered = detail.lower()
    if any(
        hint in lowered
        for hint in (
            "auth",
            "login",
            "token",
            "unauthorized",
            "401",
            "invalid",
            "expired",
            "credential",
            "not logged in",
        )
    ):
        raise ClaudeCliAuthError(detail or "Invalid Claude credentials")
    raise ClaudeCliError(detail or f"Claude CLI returned exit code {proc.returncode}")


async def async_logout(cli_path: str, config_dir: str | Path) -> None:
    """Log out an isolated Claude Code CLI config directory."""
    env = build_cli_env(config_dir=config_dir)
    proc = await asyncio.create_subprocess_exec(
        cli_path,
        "auth",
        "logout",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        await proc.wait()


class ClaudeLoginSession:
    """Drives `claude auth login --claudeai` headlessly inside a PTY.

    Because Home Assistant usually runs on a headless server (Raspberry Pi,
    HAOS VM, Docker) accessed from another device's browser, we run Claude
    Code's native headless login flow which prints an OAuth URL redirecting to
    `https://platform.claude.com/oauth/code/callback`. The user signs in on any
    browser, copies the verification code, and submits it back to the HA config
    flow, which writes it into the PTY so Claude Code exchanges and stores the
    refreshable OAuth credentials in `CLAUDE_CONFIG_DIR`.
    """

    def __init__(self, cli_path: str, config_dir: str | Path) -> None:
        self._cli_path = cli_path
        self._config_dir = Path(config_dir)
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._buffer = ""

    async def start(self, timeout: float = 20.0) -> str:
        """Spawn `claude auth login --claudeai` and wait for the OAuth URL."""
        await self.close()
        env = build_cli_env(config_dir=self._config_dir, headless=True)

        def _spawn() -> tuple[subprocess.Popen[bytes], int]:
            master_fd, slave_fd = pty.openpty()
            try:
                # Wide virtual terminal so long OAuth URLs are never line-wrapped.
                try:
                    fcntl.ioctl(
                        slave_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", 40, 4096, 0, 0),
                    )
                except OSError:
                    pass
                proc = subprocess.Popen(
                    [self._cli_path, "auth", "login", "--claudeai"],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            os.set_blocking(master_fd, False)
            return proc, master_fd

        self._proc, self._master_fd = await asyncio.to_thread(_spawn)
        self._buffer = ""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = await self._read_available(0.25)
            if chunk:
                self._buffer += chunk
                url = extract_auth_url(self._buffer)
                if url:
                    return url
            if self._proc.poll() is not None:
                break

        cleaned = strip_ansi(self._buffer).strip()
        await self.close()
        raise ClaudeCliAuthError(
            f"Could not obtain Claude sign-in URL. CLI output: {cleaned[-400:]}"
        )

    async def submit_code(self, code: str, timeout: float = 30.0) -> tuple[bool, str]:
        """Submit the pasted OAuth code to the running CLI login process."""
        if self._proc is None or self._master_fd is None:
            return False, "Login session is no longer active; please try again."

        payload = (code.strip() + "\r\n").encode("utf-8")
        try:
            await asyncio.to_thread(os.write, self._master_fd, payload)
        except OSError as err:
            await self.close()
            return False, f"Failed to send code to Claude CLI: {err}"

        tail = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = await self._read_available(0.25)
            if chunk:
                tail += chunk
            rc = self._proc.poll()
            if rc is not None:
                # Drain any remaining bytes after process exit.
                tail += await self._read_available(0.1)
                await self.close()
                if rc == 0:
                    return True, ""
                status = await async_check_auth_status(
                    self._cli_path, config_dir=self._config_dir
                )
                if status.get("loggedIn"):
                    return True, ""
                return False, strip_ansi(tail).strip()[-400:] or f"Exit code {rc}"

        await self.close()
        # Check if credentials were saved even if CLI hung on exit.
        status = await async_check_auth_status(
            self._cli_path, config_dir=self._config_dir
        )
        if status.get("loggedIn"):
            return True, ""
        return False, "Timed out waiting for Claude sign-in confirmation."

    async def _read_available(self, wait_s: float) -> str:
        fd = self._master_fd
        if fd is None:
            return ""

        def _poll_read() -> str:
            import select

            chunks: list[bytes] = []
            try:
                r, _, _ = select.select([fd], [], [], wait_s)
                while r:
                    try:
                        data = os.read(fd, 4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                    r, _, _ = select.select([fd], [], [], 0.02)
            except (OSError, ValueError):
                pass
            return b"".join(chunks).decode("utf-8", errors="replace")

        return await asyncio.to_thread(_poll_read)

    async def close(self) -> None:
        """Terminate the login subprocess group and close the PTY."""
        proc = self._proc
        fd = self._master_fd
        self._proc = None
        self._master_fd = None

        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                await asyncio.to_thread(proc.wait, 2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    await asyncio.to_thread(proc.wait, 1.0)
                except Exception:  # noqa: BLE001
                    pass

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
