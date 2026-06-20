"""Test upload using requests library for proper multipart encoding."""
import requests
import json

API = "http://localhost:8000"

def test_upload(filename, content_str):
    try:
        files = {"file": (filename, content_str.encode("utf-8"), "application/octet-stream")}
        r = requests.post(f"{API}/chat/upload", files=files, timeout=15)
        if r.status_code == 200:
            d = r.json()
            icon = {"pdf": "PDF", "pptx": "PPT", "docx": "DOC", "code": "CODE", "text": "TXT"}.get(d.get("file_type", ""), "?")
            pages = f", {d['pages']} pages" if d.get("pages") else ""
            slides = f", {d['slides']} slides" if d.get("slides") else ""
            print(f"  [PASS] {filename} -> {icon} | {d['words']} words{pages}{slides} | indexed={d['indexed']}")
        else:
            print(f"  [FAIL] {filename} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [FAIL] {filename} -> {e}")

print("=== Upload Endpoint Tests ===\n")

test_upload("hello.py", 'def greet(name):\n    print(f"Hello {name}")\n\ngreet("World")')
test_upload("app.js",   'const express = require("express");\nconst app = express();\napp.listen(3000);')
test_upload("page.html", '<!DOCTYPE html>\n<html><body><h1>Hello</h1></body></html>')
test_upload("style.css", 'body { margin: 0; }\n.container { max-width: 1200px; }')
test_upload("data.json", '{"name": "Jarvis", "version": "2.0"}')
test_upload("notes.txt", "Meeting notes: Discussed roadmap.\nDeploy v2 by Friday.")
test_upload("README.md", "# My Project\n\n## Features\n- Chat\n- Voice")
test_upload("query.sql", "SELECT * FROM users WHERE active = 1;")

print("\n=== Done ===")
