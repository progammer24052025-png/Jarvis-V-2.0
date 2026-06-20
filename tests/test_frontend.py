import urllib.request

r = urllib.request.urlopen("http://localhost:8000/app/")
html = r.read().decode()

checks = {
    "Meta description": 'meta name="description"' in html,
    "Favicon": 'rel="icon"' in html,
    "Noscript": "<noscript>" in html,
    "History button": 'id="history-btn"' in html,
    "Theme button": 'id="theme-btn"' in html,
    "Upload button": 'id="upload-btn"' in html,
    "History panel": 'id="history-panel"' in html,
    "Cache-bust CSS": "style.css?v=2.0" in html,
    "Cache-bust JS": "script.js?v=2.0" in html,
    "New Chat shortcut": "Ctrl+N" in html,
    "History shortcut": "Ctrl+Shift+H" in html,
}

print("=== JARVIS Frontend Feature Check ===")
all_ok = True
for name, ok in checks.items():
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_ok = False
    print(f"  [{status}] {name}")

print()
print("All features present!" if all_ok else "Some features missing!")
print(f"HTML size: {len(html)} bytes")
