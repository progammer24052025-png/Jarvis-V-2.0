"""
System automation tools for J.A.R.V.I.S.
Provides functions for opening/closing apps, URLs, system info, volume, screenshots, etc.
"""

import subprocess
import os
import platform
import logging
import webbrowser
import json
import time
import re

logger = logging.getLogger("J.A.R.V.I.S")

# ── Start Menu shortcut cache ──
_shortcut_cache = None
_shortcut_cache_time = 0
_CACHE_TTL = 300  # Refresh every 5 minutes


def _scan_start_menu_shortcuts() -> dict:
    """
    Scan Windows Start Menu folders for .lnk shortcuts.
    Returns {lowercase_name: full_path_to_lnk, ...}
    Caches result for 5 minutes.
    """
    global _shortcut_cache, _shortcut_cache_time
    if _shortcut_cache is not None and (time.time() - _shortcut_cache_time) < _CACHE_TTL:
        return _shortcut_cache

    shortcuts = {}
    start_dirs = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ]

    for start_dir in start_dirs:
        if not os.path.isdir(start_dir):
            continue
        try:
            for root, dirs, files in os.walk(start_dir):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        name = f[:-4]  # Strip .lnk
                        full_path = os.path.join(root, f)
                        shortcuts[name.lower()] = full_path
        except (PermissionError, OSError):
            continue

    _shortcut_cache = shortcuts
    _shortcut_cache_time = time.time()
    logger.info("[TOOL] Scanned Start Menu: %d shortcuts found", len(shortcuts))
    return shortcuts


def _fuzzy_find_shortcut(app_name: str) -> str:
    """Find the best matching Start Menu shortcut for an app name."""
    shortcuts = _scan_start_menu_shortcuts()
    app_lower = app_name.strip().lower()

    # Exact match
    if app_lower in shortcuts:
        return shortcuts[app_lower]

    # Substring match – app_name found inside shortcut name
    for name, path in shortcuts.items():
        if app_lower in name:
            return path

    # Reverse substring – shortcut name found inside app_name
    for name, path in shortcuts.items():
        if name in app_lower:
            return path

    return ""


# Cache for _is_on_path to avoid repeated subprocess calls (~200ms each)
_path_cache = {}
_path_cache_time = {}
_PATH_CACHE_TTL = 60  # seconds

def _is_on_path(cmd: str) -> bool:
    """Check if a command exists on the system PATH (cached for 60s)."""
    import time as _time
    now = _time.time()
    if cmd in _path_cache and (now - _path_cache_time.get(cmd, 0)) < _PATH_CACHE_TTL:
        return _path_cache[cmd]
    try:
        result = subprocess.run(
            ["where", cmd], capture_output=True, text=True, timeout=3,
        )
        found = result.returncode == 0
    except Exception:
        found = False
    _path_cache[cmd] = found
    _path_cache_time[cmd] = now
    return found


def open_app(app_name: str) -> str:
    """Open an application by name — local first, web fallback."""
    raw_name = app_name.strip()
    app_lower = raw_name.lower()

    # Step 1: Fast hardcoded map for common apps
    APP_MAP = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "firefox": "firefox",
        "brave": "brave",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "notepad": "notepad",
        "calculator": "calc",
        "calc": "calc",
        "paint": "mspaint",
        "cmd": "cmd",
        "command prompt": "cmd",
        "terminal": "wt",
        "windows terminal": "wt",
        "powershell": "powershell",
        "task manager": "taskmgr",
        "file explorer": "explorer",
        "explorer": "explorer",
        "settings": "ms-settings:",
        "control panel": "control",
        "word": "winword",
        "microsoft word": "winword",
        "excel": "excel",
        "microsoft excel": "excel",
        "powerpoint": "powerpnt",
        "outlook": "outlook",
        "vs code": "code",
        "vscode": "code",
        "visual studio code": "code",
    }

    if app_lower in APP_MAP:
        cmd = APP_MAP[app_lower]
        try:
            if cmd.startswith("ms-"):
                os.startfile(cmd)
            else:
                subprocess.Popen(cmd, shell=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Opened app (fast map): %s -> %s", raw_name, cmd)
            return f"Opened {raw_name}."
        except Exception as e:
            logger.warning("[TOOL] Fast map failed for %s: %s, trying PATH", raw_name, e)

    # Step 2: Check if command is on PATH (faster than Start Menu scan)
    if re.match(r'^[a-z0-9\s.\-+]+$', app_lower) and _is_on_path(app_lower):
        try:
            subprocess.Popen(app_lower, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Opened app (PATH): %s", raw_name)
            return f"Opened {raw_name}."
        except Exception as e:
            logger.warning("[TOOL] PATH launch failed for %s: %s", raw_name, e)

    # Step 3: Scan Start Menu shortcuts (fuzzy match)
    shortcut_path = _fuzzy_find_shortcut(app_lower)
    if shortcut_path:
        try:
            os.startfile(shortcut_path)
            shortcut_name = os.path.basename(shortcut_path)[:-4]  # Strip .lnk
            logger.info("[TOOL] Opened app (Start Menu): %s -> %s", raw_name, shortcut_path)
            return f"Opened {shortcut_name}."
        except Exception as e:
            logger.warning("[TOOL] Start Menu launch failed for %s: %s", raw_name, e)

    # Step 4: Web fallback — Direct app URLs (not search results)
    WEB_MAP = {
        "youtube": "https://www.youtube.com",
        "spotify": "https://open.spotify.com",
        "whatsapp": "https://web.whatsapp.com",
        "discord": "https://discord.com/app",
        "twitter": "https://twitter.com",
        "x": "https://x.com",
        "facebook": "https://www.facebook.com",
        "instagram": "https://www.instagram.com",
        "netflix": "https://www.netflix.com",
        "prime": "https://www.primevideo.com",
        "gmail": "https://mail.google.com",
        "google maps": "https://maps.google.com",
        "maps": "https://maps.google.com",
        "notion": "https://www.notion.so",
        "chatgpt": "https://chat.openai.com",
        "gemini": "https://gemini.google.com",
        "claude": "https://claude.ai",
        "github": "https://github.com",
        "reddit": "https://www.reddit.com",
        "linkedin": "https://www.linkedin.com",
        "telegram": "https://web.telegram.org",
        "slack": "https://app.slack.com",
        "twitch": "https://www.twitch.tv",
        "pinterest": "https://www.pinterest.com",
        "tiktok": "https://www.tiktok.com",
        "amazon": "https://www.amazon.com",
        "flipkart": "https://www.flipkart.com",
    }

    import urllib.parse
    if app_lower in WEB_MAP:
        web_url = WEB_MAP[app_lower]
    else:
        # Construct a direct URL for unknown apps
        clean_name = raw_name.replace(" ", "").lower()
        web_url = f"https://www.{clean_name}.com"

    try:
        webbrowser.open(web_url)
        logger.info("[TOOL] Web fallback for %s: %s", raw_name, web_url)
        return f"Opened {raw_name}."
    except Exception as e:
        return f"Could not open {raw_name}: {e}"


def list_installed_apps() -> str:
    """List all locally installed applications (from Start Menu shortcuts)."""
    shortcuts = _scan_start_menu_shortcuts()
    if not shortcuts:
        return "Could not detect installed applications."

    # Group by first letter, skip duplicates
    names = sorted(set(
        os.path.basename(p)[:-4] for p in shortcuts.values()
    ))

    # Limit to reasonable length
    if len(names) > 80:
        displayed = names[:80]
        result = f"Found {len(names)} installed apps (showing first 80):\n"
    else:
        displayed = names
        result = f"Found {len(names)} installed apps:\n"

    result += ", ".join(displayed)
    logger.info("[TOOL] Listed %d installed apps", len(names))
    return result


def close_app(app_name: str) -> str:
    """Close an application or browser tab by name on Windows."""
    app_name = app_name.strip().lower()
    safe_name = re.sub(r'[^\w\s\-.]', '', app_name)
    if not safe_name:
        return "Invalid application name."

    BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}

    PROCESS_MAP = {
        "chrome": "chrome.exe",
        "google chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "brave": "brave.exe",
        "edge": "msedge.exe",
        "microsoft edge": "msedge.exe",
        "notepad": "notepad.exe",
        "calculator": "Calculator.exe",
        "paint": "mspaint.exe",
        "word": "WINWORD.EXE",
        "excel": "EXCEL.EXE",
        "powerpoint": "POWERPNT.EXE",
        "outlook": "OUTLOOK.EXE",
        "vs code": "Code.exe",
        "vscode": "Code.exe",
        "spotify": "Spotify.exe",
        "discord": "Discord.exe",
        "vlc": "vlc.exe",
        "teams": "Teams.exe",
    }

    process_name = PROCESS_MAP.get(safe_name, f"{safe_name}.exe")
    is_browser = process_name in BROWSER_PROCESSES

    # Single robust PowerShell script: find window, close tab (browser) or window (non-browser)
    try:
        if is_browser:
            # For browsers: activate the window, wait for focus, then Ctrl+W to close the tab
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$titles = '*{safe_name}*'
$proc = Get-Process | Where-Object {{ $_.MainWindowTitle -like $titles }} | Select-Object -First 1
if ($proc) {{
    $wshell = New-Object -ComObject WScript.Shell
    $activated = $wshell.AppActivate($proc.Id)
    Start-Sleep -Milliseconds 500
    [System.Windows.Forms.SendKeys]::SendWait('^w')
    Write-Output "TAB_CLOSED"
}} else {{
    Write-Output "NOT_FOUND"
}}
"""
        else:
            # Non-browser: close the window gracefully via CloseMainWindow()
            ps_script = f"""
$titles = '*{safe_name}*'
$procs = Get-Process | Where-Object {{ $_.MainWindowTitle -like $titles }}
if ($procs) {{
    $procs | ForEach-Object {{ $_.CloseMainWindow() | Out-Null }}
    Write-Output "CLOSED"
}} else {{
    Write-Output "NOT_FOUND"
}}
"""
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=5
        )
        stdout = result.stdout
        if "TAB_CLOSED" in stdout or "CLOSED" in stdout:
            logger.info("[TOOL] Closed %s: %s", "tab" if is_browser else "window", safe_name)
            return f"Closed {safe_name}."
        elif "NOT_FOUND" in stdout:
            # Window not found by title, try process kill as fallback
            pass
        else:
            logger.warning("[TOOL] PowerShell output: %s", stdout[:200])
    except Exception as e:
        logger.warning("[TOOL] Window close failed for %s: %s", safe_name, e)

    # Fallback: kill the process
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", process_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            logger.info("[TOOL] Killed process: %s (%s)", safe_name, process_name)
            return f"Closed {safe_name}."
        else:
            return f"Could not find or close {safe_name}."
    except Exception as e:
        logger.error("[TOOL] Failed to close app %s: %s", safe_name, e)
        return f"Could not close {safe_name}: {e}"


def open_url(url: str) -> str:
    """Open a URL in the default web browser."""
    from urllib.parse import urlparse
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        domain = urlparse(url).netloc.replace("www.", "")
        webbrowser.open(url)
        logger.info("[TOOL] Opened URL: %s", url)
        return f"Opened {domain}."
    except Exception as e:
        logger.error("[TOOL] Failed to open URL %s: %s", url, e)
        return f"Could not open: {e}"


def search_web(query: str) -> str:
    """Open a Google search for the given query."""
    import urllib.parse
    url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    try:
        webbrowser.open(url)
        logger.info("[TOOL] Web search: %s", query)
        return f"Searching for {query}."
    except Exception as e:
        return f"Could not search: {e}"


def system_info() -> str:
    """Get current system information: CPU, RAM, battery, disk usage."""
    import psutil

    info = []
    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count()
    info.append(f"CPU: {cpu_percent}% usage ({cpu_count} cores)")

    # RAM
    mem = psutil.virtual_memory()
    info.append(f"RAM: {mem.percent}% used ({_format_bytes(mem.used)} / {_format_bytes(mem.total)})")

    # Disk
    disk = psutil.disk_usage("/")
    info.append(f"Disk: {disk.percent}% used ({_format_bytes(disk.used)} / {_format_bytes(disk.total)})")

    # Battery
    battery = psutil.sensors_battery()
    if battery:
        plug = "plugged in" if battery.power_plugged else "on battery"
        info.append(f"Battery: {battery.percent}% ({plug})")

    # Uptime
    boot = psutil.boot_time()
    uptime_secs = time.time() - boot
    hours = int(uptime_secs // 3600)
    mins = int((uptime_secs % 3600) // 60)
    info.append(f"Uptime: {hours}h {mins}m")

    # OS — detect Win 11 correctly (platform.release() returns "10" for both)
    os_release = platform.release()
    if platform.system() == "Windows" and os_release == "10":
        try:
            build = int(platform.version().split(".")[-1])
            if build >= 22000:
                os_release = "11"
        except (ValueError, IndexError):
            pass
    info.append(f"OS: {platform.system()} {os_release} ({platform.machine()})")

    result = "\n".join(info)
    logger.info("[TOOL] System info retrieved")
    return result


def screenshot(filename: str = None) -> str:
    """Take a screenshot and save to the Desktop."""
    try:
        import mss
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not filename:
            filename = f"screenshot_{int(time.time())}.png"
        filepath = os.path.join(desktop, filename)
        with mss.mss() as sct:
            sct.shot(output=filepath)
        logger.info("[TOOL] Screenshot saved: %s", filepath)
        return f"Screenshot saved to: {filepath}"
    except ImportError:
        # Fallback: use Windows Snipping Tool
        try:
            subprocess.Popen("snippingtool", shell=True)
            return "Opened Snipping Tool for you to take a screenshot."
        except FileNotFoundError:
            return "Could not take screenshot. Snipping Tool not found. Install 'mss' package: pip install mss"
        except Exception as e:
            return f"Could not take screenshot ({type(e).__name__}): {str(e)}. Install 'mss' package: pip install mss"
    except PermissionError:
        return f"Permission denied: cannot save screenshot to '{filepath}'. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Screenshot failed: %s", e)
        return f"Screenshot failed ({type(e).__name__}): {str(e)}"


def volume_control(action: str, level: int = None) -> str:
    """Control system volume robustly using pycaw. Actions: mute, unmute, set (with level 0-100)."""
    action = action.strip().lower()
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        if action == "mute":
            volume.SetMute(1, None)
            return "Volume muted."
        elif action == "unmute":
            volume.SetMute(0, None)
            return "Volume unmuted."
        elif action == "set" and level is not None:
            level = max(0, min(100, int(level)))
            # pycaw expects a scalar from 0.0 to 1.0
            volume.SetMasterVolumeLevelScalar(level / 100.0, None)
            return f"Volume set accurately to {level}%."
        else:
            return f"Unknown volume action: {action}. Use 'mute', 'unmute', or 'set' with a level."
    except ImportError:
        return "Audio control libraries (pycaw, comtypes) are not installed. Volume control is unavailable. Install with: pip install pycaw comtypes"
    except PermissionError:
        return f"Permission denied: cannot access audio device. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Volume control failed: %s", e)
        return f"Volume control failed ({type(e).__name__}): {str(e)}"


def file_search(name: str, directory: str = None) -> str:
    """Search for files matching a name pattern on the PC."""
    if not directory:
        directory = os.path.expanduser("~")
    
    matches = []
    name_lower = name.strip().lower()
    try:
        for root, dirs, files in os.walk(directory):
            # Skip hidden/system directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in (
                'node_modules', '__pycache__', '.git', 'AppData', '$Recycle.Bin',
            )]
            for f in files:
                if name_lower in f.lower():
                    matches.append(os.path.join(root, f))
                    if len(matches) >= 15:
                        break
            if len(matches) >= 15:
                break
    except PermissionError:
        return f"Permission denied: cannot search in '{directory}'. Try running as administrator."
    except FileNotFoundError:
        return f"Not found: directory '{directory}' does not exist."
    except Exception as e:
        return f"Search error ({type(e).__name__}): {str(e)}"

    if matches:
        result = f"Found {len(matches)} file(s):\n" + "\n".join(matches[:15])
        if len(matches) >= 15:
            result += "\n(showing first 15 results)"
        logger.info("[TOOL] File search '%s': %d results", name, len(matches))
        return result
    return f"No files matching '{name}' found in {directory}."


def run_shell_command(command: str) -> str:
    """Run a safe shell command and return output. Only allows whitelisted commands."""
    SAFE_PREFIXES = [
        "ipconfig", "hostname", "whoami", "date", "time",
        "systeminfo", "ver", "echo", "type", "dir", "where",
        "ping", "nslookup", "tracert", "netstat",
        "wmic", "powershell -Command Get-",
        "python --version", "node --version", "git --version",
        "pip list", "npm list",
    ]

    command = command.strip()
    cmd_lower = command.lower()

    # Block dangerous commands
    BLOCKED = ["del ", "rm ", "rmdir", "format", "shutdown", "restart",
               "reg ", "regedit", "taskkill", "net user", "net stop",
               "cipher", "diskpart", "bcdedit", "sfc"]
    for b in BLOCKED:
        if b in cmd_lower:
            return f"Blocked: '{command}' is not allowed for safety reasons."

    # Block shell metacharacters that allow command chaining / redirection
    DANGEROUS_SEQUENCES = ['&&', '||', ';', '|', '>', '<', '`', '$(']
    for ds in DANGEROUS_SEQUENCES:
        if ds in command:
            return f"Blocked: command contains disallowed character sequence '{ds}'."

    # Check if command starts with a safe prefix
    is_safe = any(cmd_lower.startswith(p) for p in SAFE_PREFIXES)
    if not is_safe:
        return f"Command '{command}' is not in the safe command list. Only system info and diagnostic commands are allowed."

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if result.stderr.strip():
            output += f"\n(stderr: {result.stderr.strip()[:200]})"
        if not output:
            output = "(no output)"
        # Limit output length
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        logger.info("[TOOL] Shell command: %s", command[:80])
        return output
    except subprocess.TimeoutExpired:
        return f"Command timed out after 30 seconds: {command}"
    except PermissionError:
        return f"Permission denied: cannot execute '{command}'. Try running as administrator."
    except Exception as e:
        return f"Command error ({type(e).__name__}): {str(e)}"


def play_youtube(query: str) -> str:
    """Play a YouTube video by searching and autoplaying the first result."""
    import urllib.parse
    import re as _re
    query = query.strip()
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"

    try:
        import urllib.request
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract first video ID from the search results page
        # Try multiple patterns for higher accuracy
        match = _re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        if not match:
            match = _re.search(r'vi/([a-zA-Z0-9_-]{11})', html)
        if match:
            video_id = match.group(1)
            # Use embed URL with autoplay=1 to force immediate playback
            embed_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1"
            webbrowser.open(embed_url)
            logger.info("[TOOL] Playing YouTube video: %s -> %s", query, embed_url)
            return f"Playing {query}."
        else:
            # Fallback: open search results page
            webbrowser.open(search_url)
            logger.warning("[TOOL] Could not extract video ID, opened search instead: %s", query)
            return f"Playing {query}."

    except Exception as e:
        # Fallback: just open the search URL
        try:
            webbrowser.open(search_url)
            return f"Playing {query}."
        except Exception:
            return f"Could not play {query}: {e}"


def _get_desktop_path() -> str:
    """Get the user's Desktop path."""
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _is_safe_desktop_path(filepath: str) -> bool:
    """Check if a filepath is safely within the Desktop directory."""
    desktop = os.path.normpath(_get_desktop_path())
    target = os.path.normpath(os.path.abspath(filepath))
    return target.startswith(desktop)


def open_file(filename: str) -> str:
    """Open a file from the Desktop with its default application."""
    desktop = _get_desktop_path()

    # If filename is just a name, prepend Desktop path
    if not os.path.isabs(filename):
        filepath = os.path.join(desktop, filename)
    else:
        filepath = filename

    if not _is_safe_desktop_path(filepath):
        return "For safety, I can only open files from the Desktop."

    if not os.path.exists(filepath):
        # Try fuzzy match on Desktop
        name_lower = filename.strip().lower()
        try:
            for f in os.listdir(desktop):
                if name_lower in f.lower():
                    filepath = os.path.join(desktop, f)
                    break
            else:
                return f"File not found on Desktop: {filename}"
        except Exception:
            return f"File not found on Desktop: {filename}"

    try:
        os.startfile(filepath)
        logger.info("[TOOL] Opened file: %s", filepath)
        return f"Opened {os.path.basename(filepath)}."
    except PermissionError:
        return f"Permission denied: cannot open '{filename}'. Try running as administrator."
    except FileNotFoundError:
        return f"Not found: '{filename}' does not exist on the Desktop."
    except Exception as e:
        return f"Could not open file '{filename}' ({type(e).__name__}): {str(e)}"


def read_file(filename: str) -> str:
    """Read the contents of a text file from the Desktop."""
    desktop = _get_desktop_path()

    if not os.path.isabs(filename):
        filepath = os.path.join(desktop, filename)
    else:
        filepath = filename

    if not _is_safe_desktop_path(filepath):
        return "For safety, I can only read files from the Desktop."

    if not os.path.exists(filepath):
        # Fuzzy match
        name_lower = filename.strip().lower()
        try:
            for f in os.listdir(desktop):
                if name_lower in f.lower():
                    filepath = os.path.join(desktop, f)
                    break
            else:
                return f"File not found on Desktop: {filename}"
        except Exception:
            return f"File not found on Desktop: {filename}"

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(5000)  # Limit to 5000 chars
        if len(content) >= 5000:
            content += "\n... (truncated, file is larger)"
        logger.info("[TOOL] Read file: %s (%d chars)", filepath, len(content))
        return f"Contents of {os.path.basename(filepath)}:\n{content}"
    except PermissionError:
        return f"Permission denied: cannot read '{filename}'. Try running as administrator."
    except FileNotFoundError:
        return f"Not found: '{filename}' does not exist on the Desktop."
    except UnicodeDecodeError as e:
        return f"Cannot read '{filename}': file appears to be binary or uses an unsupported encoding."
    except Exception as e:
        return f"Could not read file '{filename}' ({type(e).__name__}): {str(e)}"


def write_file(filename: str, content: str) -> str:
    """Write content to a file on the Desktop. Creates or overwrites the file."""
    desktop = _get_desktop_path()

    if not os.path.isabs(filename):
        filepath = os.path.join(desktop, filename)
    else:
        filepath = filename

    if not _is_safe_desktop_path(filepath):
        return "For safety, I can only write files to the Desktop."

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("[TOOL] Wrote file: %s (%d chars)", filepath, len(content))
        return f"Saved {os.path.basename(filepath)} to Desktop ({len(content)} characters)."
    except PermissionError:
        return f"Permission denied: cannot write to '{filename}'. Try running as administrator."
    except FileNotFoundError:
        return f"Not found: directory for '{filename}' does not exist."
    except Exception as e:
        return f"Could not write file '{filename}' ({type(e).__name__}): {str(e)}"

def _format_bytes(b: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def weather(city: str = "") -> str:
    """Get current weather using Open-Meteo API (free, no key required)."""
    import urllib.request
    import urllib.parse

    city = city.strip() if city else "auto"

    try:
        if city.lower() in ("auto", "here", "my location", "current", ""):
            # Auto-detect location via IP
            with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
                loc = json.loads(resp.read().decode())
            lat, lon = loc.get("loc", "28.6,77.2").split(",")
            city_name = loc.get("city", "Unknown")
        else:
            # Geocode the city name
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote_plus(city)}&count=1"
            with urllib.request.urlopen(geo_url, timeout=5) as resp:
                geo = json.loads(resp.read().decode())
            results = geo.get("results", [])
            if not results:
                return f"Could not find location: {city}"
            lat, lon = results[0]["latitude"], results[0]["longitude"]
            city_name = results[0].get("name", city)

        # Fetch weather
        wx_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=1"
        )
        with urllib.request.urlopen(wx_url, timeout=5) as resp:
            wx = json.loads(resp.read().decode())

        cur = wx.get("current", {})
        temp = cur.get("temperature_2m", "?")
        feels = cur.get("apparent_temperature", "?")
        humid = cur.get("relative_humidity_2m", "?")
        wind = cur.get("wind_speed_10m", "?")

        # Weather codes → descriptions
        WX_CODES = {0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",
                    45:"Fog",48:"Rime fog",51:"Light drizzle",53:"Drizzle",55:"Heavy drizzle",
                    61:"Light rain",63:"Rain",65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",
                    80:"Light showers",81:"Showers",82:"Heavy showers",95:"Thunderstorm",96:"Thunderstorm + hail"}
        code = cur.get("weather_code", -1)
        condition = WX_CODES.get(code, f"Code {code}")

        daily = wx.get("daily", {})
        hi = daily.get("temperature_2m_max", ["?"])[0]
        lo = daily.get("temperature_2m_min", ["?"])[0]

        result = (
            f"Weather in {city_name}:\n"
            f"Condition: {condition}\n"
            f"Temperature: {temp}°C (feels like {feels}°C)\n"
            f"High/Low: {hi}°C / {lo}°C\n"
            f"Humidity: {humid}%\n"
            f"Wind: {wind} km/h"
        )
        logger.info("[TOOL] Weather for %s: %s, %s°C", city_name, condition, temp)
        return result
    except Exception as e:
        logger.error("[TOOL] Weather failed: %s", e)
        return f"Could not fetch weather: {e}"


def brightness_control(level: int = 50) -> str:
    """Set screen brightness (0-100) on Windows using PowerShell."""
    level = max(0, min(100, int(level)))
    try:
        ps_cmd = (
            f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
            f".WmiSetBrightness(1,{level})"
        )
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("[TOOL] Brightness set to %d%%", level)
            return f"Brightness set to {level}%."
        else:
            # Fallback message for desktop PCs (no WMI brightness)
            return f"Could not set brightness. This may not be supported on desktop monitors. Error: {result.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return f"Command timed out after 10 seconds: brightness control"
    except PermissionError:
        return "Permission denied: cannot adjust brightness. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Brightness control failed: %s", e)
        return f"Brightness control failed ({type(e).__name__}): {str(e)}"


def wifi_info() -> str:
    """Get current WiFi network info on Windows."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "Could not retrieve WiFi info. WiFi adapter may not be available."

        lines = result.stdout.strip().split("\n")
        info = {}
        for line in lines:
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if any(k in key for k in ("ssid", "signal", "radio", "channel", "band", "state")):
                    info[key.strip()] = val

        if not info:
            return "No WiFi connection detected."

        parts = [f"{k}: {v}" for k, v in info.items()]
        logger.info("[TOOL] WiFi info retrieved")
        return "WiFi Info:\n" + "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "Command timed out after 10 seconds: WiFi info query"
    except FileNotFoundError:
        return "WiFi info command not found. 'netsh' may not be available on this system."
    except PermissionError:
        return "Permission denied: cannot access WiFi info. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] WiFi info failed: %s", e)
        return f"Could not get WiFi info ({type(e).__name__}): {str(e)}"


def play_spotify(query: str) -> str:
    """Open Spotify and search for a song/artist/playlist."""
    import urllib.parse
    query = query.strip()

    # Try Spotify URI (opens desktop app directly to search results)
    spotify_uri = f"spotify:search:{urllib.parse.quote_plus(query)}"
    try:
        os.startfile(spotify_uri)
        logger.info("[TOOL] Opened Spotify app: %s", query)
        return f"Playing {query}."
    except Exception:
        pass

    # Fallback: open Spotify web search
    try:
        web_url = f"https://open.spotify.com/search/{urllib.parse.quote_plus(query)}"
        webbrowser.open(web_url)
        logger.info("[TOOL] Opened Spotify web: %s", query)
        return f"Playing {query}."
    except Exception as e:
        return f"Could not open Spotify: {e}"


def generate_image(prompt: str) -> str:
    """Generate an image using Pollinations.ai (free, no key) and open it in browser."""
    import urllib.parse
    prompt = prompt.strip()
    if not prompt:
        return "Please provide a description for the image."

    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote_plus(prompt)}?width=1024&height=1024&nologo=true"
    try:
        webbrowser.open(url)
        logger.info("[TOOL] Generated image for: %s", prompt[:50])
        return f"Generating image for: '{prompt}'. Opened in your browser."
    except Exception as e:
        return f"Could not generate image: {e}"


# ── In-memory reminders store ──
_reminders = []
_reminder_id_counter = 0

def set_reminder(message: str, minutes: int = 5) -> str:
    """Set a timed reminder. After the specified minutes, it triggers."""
    global _reminder_id_counter
    import asyncio
    minutes = max(1, min(1440, int(minutes)))  # 1 min to 24 hours
    _reminder_id_counter += 1
    rid = _reminder_id_counter

    async def _fire_reminder():
        await asyncio.sleep(minutes * 60)
        logger.info("[REMINDER] Fired reminder #%d: %s", rid, message[:80])
        _reminders.append({"id": rid, "message": message, "fired": True, "time": time.time()})

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_fire_reminder())
        else:
            # Fallback for sync context
            import threading
            def _sync_fire():
                time.sleep(minutes * 60)
                logger.info("[REMINDER] Fired reminder #%d: %s", rid, message[:80])
                _reminders.append({"id": rid, "message": message, "fired": True, "time": time.time()})
            threading.Thread(target=_sync_fire, daemon=True).start()
    except Exception:
        import threading
        def _sync_fire():
            time.sleep(minutes * 60)
            _reminders.append({"id": rid, "message": message, "fired": True, "time": time.time()})
        threading.Thread(target=_sync_fire, daemon=True).start()

    logger.info("[TOOL] Reminder #%d set: '%s' in %d minutes", rid, message[:50], minutes)
    return f"Reminder set! I'll remind you in {minutes} minute{'s' if minutes != 1 else ''}: '{message}'"


def lock_pc() -> str:
    """Lock the Windows workstation."""
    try:
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=True)
        logger.info("[TOOL] Workstation locked.")
        return "Locked."
    except subprocess.TimeoutExpired:
        return "Timed out locking workstation."
    except PermissionError:
        return "Permission denied: cannot lock workstation."
    except Exception as e:
        logger.error("[TOOL] Failed to lock workstation: %s", e)
        return f"Could not lock: {e}"


def empty_recycle_bin() -> str:
    """Empty the Windows recycle bin."""
    try:
        subprocess.run(
            ["powershell", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
            check=True
        )
        logger.info("[TOOL] Recycle bin emptied.")
        return "Recycle bin emptied successfully."
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds: empty recycle bin"
    except PermissionError:
        return "Permission denied: cannot empty recycle bin. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Failed to empty recycle bin: %s", e)
        return f"Failed to empty recycle bin ({type(e).__name__}): {str(e)}"


def _fuzzy_find_folder(base_path: str, name: str) -> str:
    """Try to fuzzy-match a folder name inside base_path.
    Returns the full path if found, empty string otherwise."""
    name_lower = name.strip().lower().replace(' ', '-')
    try:
        for entry in os.listdir(base_path):
            entry_lower = entry.lower().replace(' ', '-')
            if entry_lower == name_lower:
                return os.path.join(base_path, entry)
        # Substring match
        for entry in os.listdir(base_path):
            entry_lower = entry.lower().replace(' ', '-')
            if name_lower in entry_lower or entry_lower in name_lower:
                return os.path.join(base_path, entry)
    except (PermissionError, OSError):
        pass
    return ""


def _resolve_desktop_dir(directory: str) -> tuple:
    """Resolve a directory name to a full Desktop path with fuzzy fallback.
    Returns (filepath, error_message). If error_message is non-empty, return it to the user."""
    desktop = _get_desktop_path()

    if not os.path.isabs(directory):
        filepath = os.path.join(desktop, directory)
    else:
        filepath = directory

    if not _is_safe_desktop_path(filepath):
        return "", "For safety, I can only access directories on the Desktop."

    if os.path.exists(filepath) and os.path.isdir(filepath):
        return filepath, ""

    # Fuzzy fallback
    match = _fuzzy_find_folder(desktop, directory)
    if match:
        return match, ""

    return "", f"Directory not found: '{directory}' does not exist on the Desktop."


def git_status(directory: str) -> str:
    """Get the git status of a directory on the Desktop with branch info and recent commits."""
    filepath, err = _resolve_desktop_dir(directory)
    if err:
        return err

    try:
        # Get current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        if branch_result.returncode != 0:
            err = branch_result.stderr.strip()
            if "not a git repository" in err.lower():
                return f"'{directory}' is not a git repository."
            return f"Git error in '{directory}': {err}"
        branch = branch_result.stdout.strip()

        # Get short status
        status_result = subprocess.run(
            ["git", "status", "-s"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        status_output = status_result.stdout.strip()

        # Get recent commits
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        log_output = log_result.stdout.strip()

        parts = [f"Repository: {os.path.basename(filepath)}", f"Branch: {branch}"]

        if status_output:
            changed_files = status_output.split('\n')
            parts.append(f"Changed files ({len(changed_files)}):")
            for f in changed_files[:10]:
                parts.append(f"  {f.strip()}")
            if len(changed_files) > 10:
                parts.append(f"  ... and {len(changed_files) - 10} more")
        else:
            parts.append("Working tree clean -- no uncommitted changes.")

        if log_output:
            parts.append("Recent commits:")
            for line in log_output.split('\n'):
                parts.append(f"  {line.strip()}")

        logger.info("[TOOL] Git status for %s retrieved (branch: %s)", filepath, branch)
        return "\n".join(parts)

    except FileNotFoundError:
        return "Git is not installed or not in the system PATH. Install Git from https://git-scm.com/"
    except subprocess.TimeoutExpired:
        return f"Git command timed out after 10 seconds for '{directory}'. The repository may be very large."
    except PermissionError:
        return f"Permission denied: cannot access '{directory}'. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Failed to get git status for %s: %s (%s)", filepath, e, type(e).__name__)
        return f"Failed to get git status for '{directory}' ({type(e).__name__}): {str(e)}"


def git_log(directory: str, count: int = 5) -> str:
    """Show recent git commits for a project on the Desktop."""
    filepath, err = _resolve_desktop_dir(directory)
    if err:
        return err

    try:
        count = max(1, min(int(count), 30))  # Clamp between 1 and 30
        result = subprocess.run(
            ["git", "log", "--oneline", f"-n{count}", "--no-decorate"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            if "not a git repository" in err.lower():
                return f"'{directory}' is not a git repository."
            return f"Git log error in '{directory}': {err}"

        output = result.stdout.strip()
        if not output:
            return f"No commits found in '{directory}'."

        lines = output.split('\n')
        parts = [f"Recent commits in {os.path.basename(filepath)}:"]
        for line in lines:
            parts.append(f"  {line.strip()}")

        logger.info("[TOOL] Git log for %s retrieved (%d entries)", filepath, len(lines))
        return "\n".join(parts)

    except FileNotFoundError:
        return "Git is not installed or not in the system PATH. Install Git from https://git-scm.com/"
    except subprocess.TimeoutExpired:
        return f"Git command timed out after 10 seconds for '{directory}'."
    except PermissionError:
        return f"Permission denied: cannot access '{directory}'."
    except Exception as e:
        logger.error("[TOOL] Failed to get git log for %s: %s (%s)", filepath, e, type(e).__name__)
        return f"Failed to get git log for '{directory}' ({type(e).__name__}): {str(e)}"


def git_diff(directory: str) -> str:
    """Show a summary of uncommitted changes in a git project."""
    filepath, err = _resolve_desktop_dir(directory)
    if err:
        return err

    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            if "not a git repository" in err.lower():
                return f"'{directory}' is not a git repository."
            return f"Git diff error in '{directory}': {err}"

        output = result.stdout.strip()
        if not output:
            # Check for staged but uncommitted changes
            staged = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                cwd=filepath, capture_output=True, text=True, timeout=10
            )
            staged_output = staged.stdout.strip()
            if staged_output:
                return f"Staged changes in {os.path.basename(filepath)}:\n{staged_output}"
            return f"No uncommitted changes in '{directory}'. Working tree is clean."

        # Also get untracked files count
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        untracked_files = [f for f in untracked.stdout.strip().split('\n') if f]

        parts = [f"Uncommitted changes in {os.path.basename(filepath)}:"]
        for line in output.split('\n'):
            parts.append(f"  {line.strip()}")
        if untracked_files:
            parts.append(f"\nUntracked files ({len(untracked_files)}):")
            for f in untracked_files[:5]:
                parts.append(f"  {f}")
            if len(untracked_files) > 5:
                parts.append(f"  ... and {len(untracked_files) - 5} more")

        logger.info("[TOOL] Git diff for %s retrieved", filepath)
        return "\n".join(parts)

    except FileNotFoundError:
        return "Git is not installed or not in the system PATH. Install Git from https://git-scm.com/"
    except subprocess.TimeoutExpired:
        return f"Git command timed out after 10 seconds for '{directory}'."
    except PermissionError:
        return f"Permission denied: cannot access '{directory}'."
    except Exception as e:
        logger.error("[TOOL] Failed to get git diff for %s: %s (%s)", filepath, e, type(e).__name__)
        return f"Failed to get git diff for '{directory}' ({type(e).__name__}): {str(e)}"


def list_desktop() -> str:
    """List everything currently on the Desktop -- files, folders, and their sizes."""
    try:
        desktop = _get_desktop_path()
        if not os.path.exists(desktop):
            return f"Desktop folder not found at: {desktop}"

        folders = []
        files = []
        for item in sorted(os.listdir(desktop)):
            # Skip system files
            if item.lower() in ('desktop.ini', 'thumbs.db'):
                continue
            full_path = os.path.join(desktop, item)
            try:
                stat = os.stat(full_path)
                if os.path.isdir(full_path):
                    # Count items inside folder
                    try:
                        count = len(os.listdir(full_path))
                        folders.append(f"  {item}/ ({count} items)")
                    except PermissionError:
                        folders.append(f"  {item}/ (access denied)")
                else:
                    folders_str = None  # just for files below
                    files.append(f"  {item} ({_format_bytes(stat.st_size)})")
            except OSError:
                files.append(f"  {item} (size unknown)")

        parts = [f"Desktop contents ({os.path.basename(desktop)}):"]
        if folders:
            parts.append(f"\nFolders ({len(folders)}):")
            parts.extend(folders)
        else:
            parts.append("\nNo folders.")
        if files:
            parts.append(f"\nFiles ({len(files)}):")
            parts.extend(files)
        else:
            parts.append("\nNo files.")

        logger.info("[TOOL] Desktop listed: %d folders, %d files", len(folders), len(files))
        return "\n".join(parts)

    except PermissionError:
        return "Permission denied: cannot access the Desktop folder."
    except Exception as e:
        logger.error("[TOOL] Failed to list desktop: %s (%s)", e, type(e).__name__)
        return f"Failed to list Desktop ({type(e).__name__}): {str(e)}"


def list_folder(folder_name: str) -> str:
    """List contents of a specific folder on the Desktop."""
    filepath, err = _resolve_desktop_dir(folder_name)
    if err:
        return err

    try:
        folders = []
        files = []
        for item in sorted(os.listdir(filepath)):
            if item.lower() in ('desktop.ini', 'thumbs.db', '.git'):
                continue
            full_path = os.path.join(filepath, item)
            try:
                stat = os.stat(full_path)
                if os.path.isdir(full_path):
                    try:
                        count = len(os.listdir(full_path))
                        folders.append(f"  {item}/ ({count} items)")
                    except PermissionError:
                        folders.append(f"  {item}/ (access denied)")
                else:
                    files.append(f"  {item} ({_format_bytes(stat.st_size)})")
            except OSError:
                files.append(f"  {item} (size unknown)")

        parts = [f"Contents of '{os.path.basename(filepath)}':"]
        if folders:
            parts.append(f"\nFolders ({len(folders)}):")
            parts.extend(folders)
        if files:
            parts.append(f"\nFiles ({len(files)}):")
            parts.extend(files)
        if not folders and not files:
            parts.append("  (empty folder)")

        logger.info("[TOOL] Listed folder %s: %d folders, %d files", folder_name, len(folders), len(files))
        return "\n".join(parts)

    except PermissionError:
        return f"Permission denied: cannot access '{folder_name}'."
    except Exception as e:
        logger.error("[TOOL] Failed to list folder %s: %s (%s)", folder_name, e, type(e).__name__)
        return f"Failed to list '{folder_name}' ({type(e).__name__}): {str(e)}"


def move_item(source: str, destination: str) -> str:
    """Move a file or folder. Both source and destination must be on the Desktop."""
    import shutil
    desktop = _get_desktop_path()

    # Resolve source path
    if not os.path.isabs(source):
        src_path = os.path.join(desktop, source)
    else:
        src_path = source

    # Resolve destination path
    if not os.path.isabs(destination):
        dst_path = os.path.join(desktop, destination)
    else:
        dst_path = destination

    if not _is_safe_desktop_path(src_path):
        return f"For safety, source must be on the Desktop: '{source}'"
    if not _is_safe_desktop_path(dst_path):
        return f"For safety, destination must be on the Desktop: '{destination}'"

    if not os.path.exists(src_path):
        return f"Source not found: '{source}' does not exist on the Desktop."

    try:
        # If destination is an existing folder, move source INTO it
        if os.path.isdir(dst_path):
            final_path = os.path.join(dst_path, os.path.basename(src_path))
            shutil.move(src_path, final_path)
            logger.info("[TOOL] Moved '%s' into folder '%s'", source, destination)
            return f"Moved '{os.path.basename(src_path)}' into '{os.path.basename(dst_path)}/'."
        else:
            # Move/rename to the exact destination path
            shutil.move(src_path, dst_path)
            logger.info("[TOOL] Moved '%s' to '%s'", source, destination)
            return f"Moved '{os.path.basename(src_path)}' to '{os.path.basename(dst_path)}'."

    except PermissionError:
        return f"Permission denied: cannot move '{source}'. Try running as administrator."
    except shutil.Error as e:
        return f"Move failed: {str(e)}"
    except Exception as e:
        logger.error("[TOOL] Failed to move '%s' to '%s': %s (%s)", source, destination, e, type(e).__name__)
        return f"Failed to move '{source}' ({type(e).__name__}): {str(e)}"


def create_folder(folder_name: str) -> str:
    """Create a new folder on the Desktop."""
    desktop = _get_desktop_path()

    # Sanitize: remove path separators and dangerous chars
    safe_name = re.sub(r'[<>:"/\\|?*]', '', folder_name).strip()
    if not safe_name:
        return "Invalid folder name. Please provide a valid name."

    filepath = os.path.join(desktop, safe_name)

    if not _is_safe_desktop_path(filepath):
        return f"For safety, I can only create folders on the Desktop."

    if os.path.exists(filepath):
        return f"Folder '{safe_name}' already exists on the Desktop."

    try:
        os.makedirs(filepath, exist_ok=True)
        logger.info("[TOOL] Created folder: %s", safe_name)
        return f"Created folder '{safe_name}' on Desktop."
    except PermissionError:
        return f"Permission denied: cannot create '{safe_name}'. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Failed to create folder '%s': %s (%s)", safe_name, e, type(e).__name__)
        return f"Failed to create '{safe_name}' ({type(e).__name__}): {str(e)}"


def delete_item(item_name: str) -> str:
    """Delete a file or folder from the Desktop by sending it to the Recycle Bin."""
    desktop = _get_desktop_path()

    if not os.path.isabs(item_name):
        filepath = os.path.join(desktop, item_name)
    else:
        filepath = item_name

    if not _is_safe_desktop_path(filepath):
        return "For safety, I can only delete items from the Desktop."

    if not os.path.exists(filepath):
        return f"Not found: '{item_name}' does not exist on the Desktop."

    try:
        # Use PowerShell to send to Recycle Bin (not permanent delete)
        ps_script = f"""
        Add-Type -AssemblyName Microsoft.VisualBasic
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            '{filepath}',
            'OnlyErrorDialogs',
            'SendToRecycleBin'
        )
        """
        # Check if it's a folder (need different method for directories)
        if os.path.isdir(filepath):
            ps_script = f"""
            Add-Type -AssemblyName Microsoft.VisualBasic
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                '{filepath}',
                'OnlyErrorDialogs',
                'SendToRecycleBin'
            )
            """

        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            item_type = "folder" if os.path.isdir(filepath) else "file"
            logger.info("[TOOL] Deleted (to Recycle Bin): %s", item_name)
            return f"Moved '{item_name}' to Recycle Bin."
        else:
            return f"Delete failed: {result.stderr.strip()}"

    except subprocess.TimeoutExpired:
        return f"Delete timed out after 10 seconds for '{item_name}'."
    except PermissionError:
        return f"Permission denied: cannot delete '{item_name}'. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Failed to delete '%s': %s (%s)", item_name, e, type(e).__name__)
        return f"Failed to delete '{item_name}' ({type(e).__name__}): {str(e)}"


def rename_item(old_name: str, new_name: str) -> str:
    """Rename a file or folder on the Desktop."""
    desktop = _get_desktop_path()

    # Sanitize new name
    safe_new_name = re.sub(r'[<>:"/\\|?*]', '', new_name).strip()
    if not safe_new_name:
        return "Invalid new name. Please provide a valid name."

    if not os.path.isabs(old_name):
        src_path = os.path.join(desktop, old_name)
    else:
        src_path = old_name

    dst_path = os.path.join(os.path.dirname(src_path), safe_new_name)

    if not _is_safe_desktop_path(src_path):
        return "For safety, I can only rename items on the Desktop."

    if not os.path.exists(src_path):
        return f"Not found: '{old_name}' does not exist on the Desktop."

    if os.path.exists(dst_path):
        return f"Cannot rename: '{safe_new_name}' already exists on the Desktop."

    try:
        os.rename(src_path, dst_path)
        logger.info("[TOOL] Renamed '%s' to '%s'", old_name, safe_new_name)
        return f"Renamed '{old_name}' to '{safe_new_name}'."
    except PermissionError:
        return f"Permission denied: cannot rename '{old_name}'. Try running as administrator."
    except Exception as e:
        logger.error("[TOOL] Failed to rename '%s' to '%s': %s (%s)", old_name, safe_new_name, e, type(e).__name__)
        return f"Failed to rename '{old_name}' ({type(e).__name__}): {str(e)}"


def switch_window(app_name: str) -> str:
    """Bring a specific application or window to the foreground."""
    app_name = app_name.strip()
    try:
        import pygetwindow as gw
        # Get all windows containing the app_name in their title (case-insensitive via lower)
        app_lower = app_name.lower()
        windows = [w for w in gw.getAllWindows() if w.title and app_lower in w.title.lower()]
        
        if not windows:
            return f"Could not find any open window for '{app_name}'."
            
        # Prioritize the most prominent match (or just take the first)
        win = windows[0]
        
        if win.isMinimized:
            win.restore()
            
        try:
            win.activate()
        except Exception:
            # Pygetwindow activate() sometimes throws PyGetWindowException on Windows
            # due to foreground lock timeout. We fallback to PowerShell.
            ps_script = f"""
            $sig = @'
            [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
            [DllImport("user32.dll")] public static extern int SetForegroundWindow(IntPtr hwnd);
            '@
            Add-Type -MemberDefinition $sig -name NativeMethods -namespace Win32
            $hwnd = [IntPtr]{win._hWnd}
            [Win32.NativeMethods]::ShowWindowAsync($hwnd, 9)
            [Win32.NativeMethods]::SetForegroundWindow($hwnd)
            """
            subprocess.run(["powershell", "-Command", ps_script], capture_output=True, timeout=5)
            
        logger.info("[TOOL] Switched to window: %s", win.title)
        return f"Switched to {app_name}."
    except ImportError:
        return "Window management library (pygetwindow) is not installed."
    except Exception as e:
        logger.error("[TOOL] Switch window failed for %s: %s", app_name, e)
        return f"Failed to switch to {app_name}: {e}"


# ── Media / System Control Tools ──

def media_control(action: str) -> str:
    """Control media playback globally using Windows media keys."""
    action = action.strip().lower()
    VK_MAP = {
        "play_pause": "0xB3",
        "play": "0xB3",
        "pause": "0xB3",
        "next_track": "0xB0",
        "next": "0xB0",
        "prev_track": "0xB1",
        "previous": "0xB1",
        "stop": "0xB2",
    }
    RESPONSE_MAP = {
        "play_pause": "Toggled play/pause.",
        "play": "Playing.",
        "pause": "Paused.",
        "next_track": "Next track.",
        "next": "Next track.",
        "prev_track": "Previous track.",
        "previous": "Previous track.",
        "stop": "Stopped.",
    }
    vk = VK_MAP.get(action)
    if not vk:
        return f"Unknown media action '{action}'. Use: play_pause, next_track, prev_track, stop."
    try:
        ps = f"$wshell = New-Object -ComObject WScript.Shell; $wshell.SendKeys([char]{vk})"
        subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=3)
        logger.info("[TOOL] Media control: %s", action)
        return RESPONSE_MAP.get(action, "Done.")
    except Exception as e:
        return f"Media control failed: {e}"


def shutdown_pc(action: str) -> str:
    """Shutdown, restart, sleep, or cancel a pending shutdown."""
    action = action.strip().lower()
    try:
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "30"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Shutdown initiated (30s delay)")
            return "Shutting down in 30 seconds."
        elif action == "restart":
            subprocess.Popen(["shutdown", "/r", "/t", "30"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Restart initiated (30s delay)")
            return "Restarting in 30 seconds."
        elif action == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Sleep initiated")
            return "Sleeping."
        elif action == "cancel":
            result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.info("[TOOL] Shutdown cancelled")
                return "Shutdown cancelled."
            else:
                return "No pending shutdown to cancel."
        else:
            return f"Unknown action '{action}'. Use: shutdown, restart, sleep, cancel."
    except Exception as e:
        return f"Failed: {e}"


# Shutdown flag file for exit_jarvis
_SHUTDOWN_FLAG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), ".jarvis_shutdown")

def exit_jarvis() -> str:
    """Signal JARVIS to shut down gracefully."""
    try:
        # Set the flag directly so the stream generator includes the shutdown event
        import app.main as _main_mod
        _main_mod._jarvis_shutting_down = True
        # Also write the flag file as backup for the background checker
        with open(_SHUTDOWN_FLAG, "w") as f:
            f.write("shutdown")
        logger.info("[TOOL] JARVIS shutdown triggered")
        return "Goodbye, sir."
    except Exception as e:
        # Fallback: just write the flag file
        try:
            with open(_SHUTDOWN_FLAG, "w") as f:
                f.write("shutdown")
            logger.info("[TOOL] JARVIS shutdown flag written (direct set failed)")
            return "Goodbye, sir."
        except Exception as e2:
            return f"Could not initiate shutdown: {e2}"


def press_keys(key_combo: str) -> str:
    """Simulate a keyboard shortcut via PowerShell SendKeys."""
    combo = key_combo.strip().lower()
    # Whitelist of safe key combos -> PowerShell SendKeys string
    KEY_MAP = {
        "alt_tab": "%{TAB}",
        "ctrl_c": "^c",
        "ctrl_v": "^v",
        "ctrl_x": "^x",
        "ctrl_z": "^z",
        "ctrl_s": "^s",
        "ctrl_a": "^a",
        "ctrl_f": "^f",
        "win_d": "{DOWN}d",  # Win+D not directly supported via SendKeys, use alternative
        "alt_f4": "%{F4}",
        "ctrl_shift_esc": "^+{ESC}",
    }
    RESPONSE_MAP = {
        "alt_tab": "Switched window.",
        "ctrl_c": "Copied.",
        "ctrl_v": "Pasted.",
        "ctrl_x": "Cut.",
        "ctrl_z": "Undone.",
        "ctrl_s": "Saved.",
        "ctrl_a": "Selected all.",
        "ctrl_f": "Find opened.",
        "win_d": "Showing desktop.",
        "alt_f4": "Closed window.",
        "ctrl_shift_esc": "Task manager opened.",
    }
    if combo not in KEY_MAP:
        return f"Unknown key combo '{combo}'. Available: {', '.join(KEY_MAP.keys())}"
    try:
        keys = KEY_MAP[combo]
        # Special handling for Win+D (not possible via SendKeys, use explorer shortcut)
        if combo == "win_d":
            ps = "$wshell = New-Object -ComObject WScript.Shell; $wshell.SendKeys('^{ESC}d')"
        else:
            ps = f"$wshell = New-Object -ComObject WScript.Shell; $wshell.SendKeys('{keys}')"
        subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=3)
        logger.info("[TOOL] Key combo pressed: %s", combo)
        return RESPONSE_MAP.get(combo, "Done.")
    except Exception as e:
        return f"Key press failed: {e}"


def browser_control(action: str) -> str:
    """Control browser tabs: new_tab, close_tab, next_tab, prev_tab, refresh, fullscreen."""
    action = action.strip().lower()
    ACTION_MAP = {
        "new_tab": ("^t", "New tab."),
        "close_tab": ("^w", "Tab closed."),
        "next_tab": ("^{TAB}", "Next tab."),
        "prev_tab": ("^+{TAB}", "Previous tab."),
        "refresh": ("{F5}", "Refreshed."),
        "fullscreen": ("{F11}", "Fullscreen toggled."),
    }
    if action not in ACTION_MAP:
        return f"Unknown browser action '{action}'. Use: {', '.join(ACTION_MAP.keys())}"
    keys, response = ACTION_MAP[action]
    try:
        ps = f"$wshell = New-Object -ComObject WScript.Shell; $wshell.SendKeys('{keys}')"
        subprocess.run(["powershell", "-Command", ps], capture_output=True, timeout=3)
        logger.info("[TOOL] Browser control: %s", action)
        return response
    except Exception as e:
        return f"Browser control failed: {e}"


def network_speed() -> str:
    """Quick network speed test and public IP lookup."""
    try:
        # Get public IP
        import urllib.request
        ip_req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(ip_req, timeout=3) as resp:
            public_ip = resp.read().decode("utf-8").strip()

        # Quick download speed test (1MB file from CDN)
        import time as _time
        test_url = "https://speed.cloudflare.com/__down?bytes=1000000"
        req = urllib.request.Request(test_url, headers={"User-Agent": "Mozilla/5.0"})
        start = _time.time()
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
        elapsed = _time.time() - start
        bytes_downloaded = len(data)
        speed_mbps = (bytes_downloaded * 8) / (elapsed * 1_000_000)

        logger.info("[TOOL] Network speed: %.1f Mbps, IP: %s", speed_mbps, public_ip)
        return f"Download speed: {speed_mbps:.1f} Mbps\nPublic IP: {public_ip}"
    except Exception as e:
        return f"Network test failed: {e}"


def pc_health() -> str:
    """Get a full PC health report from the background health monitor."""
    try:
        from app.services.health_service import get_health_monitor
        monitor = get_health_monitor()
        status = monitor.get_health_status()

        if status.get("status") == "no_data":
            return "Health monitor has not collected data yet. Please wait a moment."

        lines = []
        lines.append(f"CPU: {status['cpu_percent']}%")
        lines.append(f"RAM: {status['ram_percent']}% ({status['ram_used_gb']}/{status['ram_total_gb']} GB)")

        if status.get("battery_percent") is not None:
            plugged = " (charging)" if status.get("battery_plugged") else ""
            lines.append(f"Battery: {status['battery_percent']}%{plugged}")

        for disk in status.get("disks", []):
            lines.append(f"Disk {disk['drive']}: {disk['percent']}% full ({disk['free_gb']} GB free)")

        alerts = status.get("alerts", [])
        if alerts:
            lines.append("\n⚠ ALERTS:")
            lines.extend(f"  - {a}" for a in alerts)
        else:
            lines.append("\nAll systems normal.")

        # Top processes
        hogs = monitor.get_process_hogs(3)
        if hogs:
            lines.append("\nTop processes:")
            for p in hogs:
                lines.append(f"  {p['name']}: CPU {p['cpu']}%, RAM {p['ram']}%")

        logger.info("[TOOL] PC health report generated")
        return "\n".join(lines)
    except Exception as e:
        return f"Health report failed: {e}"


# Registry of all available tools with descriptions
SYSTEM_TOOLS = {
    "open_app": {
        "func": open_app,
        "description": "Open a PROGRAM/APPLICATION (e.g. Chrome, Notepad, Spotify). NOT for opening files/documents — use open_file for that",
        "params": ["app_name"],
    },
    "switch_window": {
        "func": switch_window,
        "description": "Bring an already open application or window to the foreground (e.g. if the user says 'show me chrome', 'switch to VS Code')",
        "params": ["app_name"],
    },
    "close_app": {
        "func": close_app,
        "description": "Close a running application or specific window",
        "params": ["app_name"],
    },
    "open_url": {
        "func": open_url,
        "description": "Open a URL/website in the browser",
        "params": ["url"],
    },
    "search_web": {
        "func": search_web,
        "description": "Open a Google search for a query",
        "params": ["query"],
    },
    "system_info": {
        "func": system_info,
        "description": "Get CPU, RAM, battery, disk usage, and OS info",
        "params": [],
    },
    "screenshot": {
        "func": screenshot,
        "description": "Take a screenshot of the screen",
        "params": ["filename"],
    },
    "volume_control": {
        "func": volume_control,
        "description": "Control volume: mute, unmute, or set level (0-100)",
        "params": ["action", "level"],
    },
    "brightness_control": {
        "func": brightness_control,
        "description": "Set screen brightness level (0-100)",
        "params": ["level"],
    },
    "file_search": {
        "func": file_search,
        "description": "Search for files by name on the PC",
        "params": ["name", "directory"],
    },
    "run_command": {
        "func": run_shell_command,
        "description": "Run a safe diagnostic shell command (ipconfig, hostname, etc.)",
        "params": ["command"],
    },
    "list_installed_apps": {
        "func": list_installed_apps,
        "description": "List all locally installed applications on this PC",
        "params": [],
    },
    "play_youtube": {
        "func": play_youtube,
        "description": "Play a YouTube video — searches and opens the first result directly (not just search)",
        "params": ["query"],
    },
    "play_spotify": {
        "func": play_spotify,
        "description": "Open Spotify and search/play a song, artist, or playlist",
        "params": ["query"],
    },
    "open_file": {
        "func": open_file,
        "description": "Open an EXISTING FILE or DOCUMENT from the Desktop (e.g. resume.pdf, notes.txt, report.docx). Opens with the default app. NOT for launching programs — use open_app for that",
        "params": ["filename"],
    },
    "read_file": {
        "func": read_file,
        "description": "Read and return the TEXT CONTENTS of a file on the Desktop. Use this when the user wants to know WHAT IS INSIDE a file",
        "params": ["filename"],
    },
    "write_file": {
        "func": write_file,
        "description": "Write/create/save a text file on the Desktop. Use this when user says write, save, create a file, or make a note",
        "params": ["filename", "content"],
    },
    "weather": {
        "func": weather,
        "description": "Get current weather for a city (temperature, conditions, forecast). Leave city blank for auto-detect.",
        "params": ["city"],
    },
    "wifi_info": {
        "func": wifi_info,
        "description": "Get current WiFi network name, signal strength, and connection info",
        "params": [],
    },
    "generate_image": {
        "func": generate_image,
        "description": "Generate an AI image from a text description and open it in the browser",
        "params": ["prompt"],
    },
    "set_reminder": {
        "func": set_reminder,
        "description": "Set a timed reminder. Specify what to remember and how many minutes from now (1-1440)",
        "params": ["message", "minutes"],
    },
    "lock_pc": {
        "func": lock_pc,
        "description": "Lock the Windows workstation immediately for security.",
        "params": [],
    },
    "empty_recycle_bin": {
        "func": empty_recycle_bin,
        "description": "Empty the Windows recycle bin to free up space.",
        "params": [],
    },
    "git_status": {
        "func": git_status,
        "description": "Get detailed git status of a project on the Desktop: branch name, changed files, and recent commits. Provide the directory name.",
        "params": ["directory"],
    },
    "git_log": {
        "func": git_log,
        "description": "Show recent git commit history for a project on the Desktop. Provide the directory name and optionally the number of commits (default 5).",
        "params": ["directory", "count"],
    },
    "git_diff": {
        "func": git_diff,
        "description": "Show a summary of uncommitted changes (modified, staged, untracked files) in a git project on the Desktop.",
        "params": ["directory"],
    },
    "list_desktop": {
        "func": list_desktop,
        "description": "List everything currently on the Desktop -- all files and folders with their sizes. Use when the user asks 'what's on my desktop' or 'show my desktop'.",
        "params": [],
    },
    "list_folder": {
        "func": list_folder,
        "description": "List contents of a specific folder on the Desktop. Use when the user asks 'what's in my X folder' or 'show what's inside Y'.",
        "params": ["folder_name"],
    },
    "move_item": {
        "func": move_item,
        "description": "Move a file or folder to another location on the Desktop. Use when user says 'move X to Y' or 'put X in Y folder'.",
        "params": ["source", "destination"],
    },
    "create_folder": {
        "func": create_folder,
        "description": "Create a new folder on the Desktop. Use when user says 'create a folder called X' or 'make a new folder named Y'.",
        "params": ["folder_name"],
    },
    "delete_item": {
        "func": delete_item,
        "description": "Delete a file or folder from the Desktop (sends to Recycle Bin, not permanent). Use when user says 'delete X' or 'remove Y'.",
        "params": ["item_name"],
    },
    "rename_item": {
        "func": rename_item,
        "description": "Rename a file or folder on the Desktop. Use when user says 'rename X to Y' or 'change the name of X to Y'.",
        "params": ["old_name", "new_name"],
    },
    "media_control": {
        "func": media_control,
        "description": "Control media playback globally (Spotify, YouTube, VLC, etc.). Actions: play_pause, next_track, prev_track, stop.",
        "params": ["action"],
    },
    "shutdown_pc": {
        "func": shutdown_pc,
        "description": "Shutdown, restart, sleep, or cancel a pending shutdown. Actions: shutdown, restart, sleep, cancel.",
        "params": ["action"],
    },
    "exit_jarvis": {
        "func": exit_jarvis,
        "description": "Shut down JARVIS gracefully. Use when user says 'Jarvis bye', 'goodbye Jarvis', or 'exit Jarvis'.",
        "params": [],
    },
    "press_keys": {
        "func": press_keys,
        "description": "Simulate a keyboard shortcut. Combos: alt_tab, ctrl_c, ctrl_v, ctrl_x, ctrl_z, ctrl_s, ctrl_a, ctrl_f, win_d, alt_f4, ctrl_shift_esc.",
        "params": ["key_combo"],
    },
    "browser_control": {
        "func": browser_control,
        "description": "Control browser tabs. Actions: new_tab, close_tab, next_tab, prev_tab, refresh, fullscreen.",
        "params": ["action"],
    },
    "network_speed": {
        "func": network_speed,
        "description": "Run a quick network speed test and show public IP address.",
        "params": [],
    },
    "pc_health": {
        "func": pc_health,
        "description": "Get a full PC health report: CPU, RAM, disk, battery, top processes, and any warnings. Use when user asks about PC health, performance, or system status.",
        "params": [],
    },
}
