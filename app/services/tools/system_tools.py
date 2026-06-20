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


def _is_on_path(cmd: str) -> bool:
    """Check if a command exists on the system PATH."""
    try:
        result = subprocess.run(
            ["where", cmd], capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


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
            logger.warning("[TOOL] Fast map failed for %s: %s, trying Start Menu", raw_name, e)

    # Step 2: Scan Start Menu shortcuts (fuzzy match)
    shortcut_path = _fuzzy_find_shortcut(app_lower)
    if shortcut_path:
        try:
            os.startfile(shortcut_path)
            shortcut_name = os.path.basename(shortcut_path)[:-4]  # Strip .lnk
            logger.info("[TOOL] Opened app (Start Menu): %s -> %s", raw_name, shortcut_path)
            return f"Opened {shortcut_name}."
        except Exception as e:
            logger.warning("[TOOL] Start Menu launch failed for %s: %s", raw_name, e)

    # Step 3: Check if command is on PATH
    if _is_on_path(app_lower):
        try:
            subprocess.Popen(app_lower, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("[TOOL] Opened app (PATH): %s", raw_name)
            return f"Opened {raw_name}."
        except Exception as e:
            logger.warning("[TOOL] PATH launch failed for %s: %s", raw_name, e)

    # Step 4: Web fallback — open as website
    import urllib.parse
    web_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(raw_name + ' open online')}"
    try:
        webbrowser.open(web_url)
        logger.info("[TOOL] Web fallback for %s: %s", raw_name, web_url)
        return f"{raw_name} is not installed locally. Opened a web search to find it."
    except Exception as e:
        return f"Could not find or open {raw_name}: {e}"


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
    """Close an application by killing its process on Windows."""
    app_name = app_name.strip().lower()

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

    process = PROCESS_MAP.get(app_name, f"{app_name}.exe")

    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", process],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("[TOOL] Closed app: %s (process: %s)", app_name, process)
            return f"Closed {app_name}."
        else:
            return f"Could not close {app_name}: {result.stderr.strip()}"
    except Exception as e:
        logger.error("[TOOL] Failed to close app %s: %s", app_name, e)
        return f"Could not close {app_name}: {e}"


def open_url(url: str) -> str:
    """Open a URL in the default web browser."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url)
        logger.info("[TOOL] Opened URL: %s", url)
        return f"Opened {url} in your browser."
    except Exception as e:
        logger.error("[TOOL] Failed to open URL %s: %s", url, e)
        return f"Could not open URL: {e}"


def search_web(query: str) -> str:
    """Open a Google search for the given query."""
    import urllib.parse
    url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    try:
        webbrowser.open(url)
        logger.info("[TOOL] Web search: %s", query)
        return f"Opened Google search for: {query}"
    except Exception as e:
        return f"Could not open search: {e}"


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
        except Exception:
            return "Could not take screenshot. Install 'mss' package: pip install mss"
    except Exception as e:
        logger.error("[TOOL] Screenshot failed: %s", e)
        return f"Screenshot failed: {e}"


def volume_control(action: str, level: int = None) -> str:
    """Control system volume. Actions: mute, unmute, set (with level 0-100)."""
    action = action.strip().lower()
    try:
        if action == "mute":
            subprocess.run(
                ["powershell", "-Command",
                 "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"],
                capture_output=True, timeout=5,
            )
            return "Volume muted."
        elif action == "unmute":
            subprocess.run(
                ["powershell", "-Command",
                 "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"],
                capture_output=True, timeout=5,
            )
            return "Volume unmuted."
        elif action == "set" and level is not None:
            # Use nircmd if available, otherwise PowerShell
            level = max(0, min(100, level))
            ps_cmd = f"""
            $vol = {level / 100.0};
            $obj = New-Object -ComObject WScript.Shell;
            1..50 | ForEach-Object {{ $obj.SendKeys([char]174) }};
            $steps = [math]::Round($vol * 50);
            1..$steps | ForEach-Object {{ $obj.SendKeys([char]175) }};
            """
            subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True, timeout=15,
            )
            return f"Volume set to approximately {level}%."
        else:
            return f"Unknown volume action: {action}. Use 'mute', 'unmute', or 'set' with a level."
    except Exception as e:
        logger.error("[TOOL] Volume control failed: %s", e)
        return f"Volume control failed: {e}"


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
        pass
    except Exception as e:
        return f"Search error: {e}"

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
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Command error: {e}"


def play_youtube(query: str) -> str:
    """Play a YouTube video by searching and opening the first result directly."""
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
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract first video ID from the search results page
        match = _re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        if match:
            video_id = match.group(1)
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            webbrowser.open(video_url)
            logger.info("[TOOL] Playing YouTube video: %s -> %s", query, video_url)
            return f"Playing: {video_url}"
        else:
            # Fallback: open search results page
            webbrowser.open(search_url)
            logger.warning("[TOOL] Could not extract video ID, opened search instead: %s", query)
            return f"Couldn't find exact video. Opened YouTube search for: {query}"

    except Exception as e:
        # Fallback: just open the search URL
        try:
            webbrowser.open(search_url)
            return f"Opened YouTube search for: {query}"
        except Exception:
            return f"Could not play YouTube video: {e}"


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
    except Exception as e:
        return f"Could not open file: {e}"


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
    except Exception as e:
        return f"Could not read file: {e}"


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
    except Exception as e:
        return f"Could not write file: {e}"

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
    except Exception as e:
        logger.error("[TOOL] Brightness control failed: %s", e)
        return f"Brightness control failed: {e}"


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
    except Exception as e:
        logger.error("[TOOL] WiFi info failed: %s", e)
        return f"Could not get WiFi info: {e}"


def play_spotify(query: str) -> str:
    """Open Spotify and search for a song/artist/playlist."""
    import urllib.parse
    query = query.strip()

    # Try Spotify URI (opens desktop app)
    spotify_uri = f"spotify:search:{urllib.parse.quote_plus(query)}"
    try:
        os.startfile(spotify_uri)
        logger.info("[TOOL] Opened Spotify search: %s", query)
        return f"Opened Spotify search for: {query}"
    except Exception:
        pass

    # Fallback: open Spotify web
    try:
        web_url = f"https://open.spotify.com/search/{urllib.parse.quote_plus(query)}"
        webbrowser.open(web_url)
        logger.info("[TOOL] Opened Spotify web: %s", query)
        return f"Opened Spotify web search for: {query}"
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
        return "Workstation locked successfully."
    except Exception as e:
        logger.error("[TOOL] Failed to lock workstation: %s", e)
        return f"Failed to lock workstation: {e}"


def empty_recycle_bin() -> str:
    """Empty the Windows recycle bin."""
    try:
        subprocess.run(
            ["powershell", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
            check=True
        )
        logger.info("[TOOL] Recycle bin emptied.")
        return "Recycle bin emptied successfully."
    except Exception as e:
        logger.error("[TOOL] Failed to empty recycle bin: %s", e)
        return f"Failed to empty recycle bin: {e}"


def git_status(directory: str) -> str:
    """Get the git status of a directory on the Desktop."""
    desktop = _get_desktop_path()
    
    if not os.path.isabs(directory):
        filepath = os.path.join(desktop, directory)
    else:
        filepath = directory
        
    if not _is_safe_desktop_path(filepath):
        return "For safety, I can only check git repositories on the Desktop."
        
    if not os.path.exists(filepath):
        return f"Directory not found: {directory}"
        
    try:
        result = subprocess.run(
            ["git", "status", "-s"],
            cwd=filepath, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if not output:
                logger.info("[TOOL] Git status for %s: Clean", filepath)
                return f"Git repository is clean. No uncommitted changes in {os.path.basename(filepath)}."
            logger.info("[TOOL] Git status for %s retrieved", filepath)
            return f"Git status for {os.path.basename(filepath)}:\n{output}"
        else:
            return f"Error running git status (maybe not a git repository?): {result.stderr.strip()}"
    except FileNotFoundError:
        return "Git is not installed or not in the system PATH."
    except Exception as e:
        logger.error("[TOOL] Failed to get git status: %s", e)
        return f"Failed to get git status: {e}"


# Registry of all available tools with descriptions
SYSTEM_TOOLS = {
    "open_app": {
        "func": open_app,
        "description": "Open a PROGRAM/APPLICATION (e.g. Chrome, Notepad, Spotify). NOT for opening files/documents — use open_file for that",
        "params": ["app_name"],
    },
    "close_app": {
        "func": close_app,
        "description": "Close a running application",
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
        "description": "Get the git status (modified/added files) of a directory on the Desktop. Provide the directory name.",
        "params": ["directory"],
    },
}
