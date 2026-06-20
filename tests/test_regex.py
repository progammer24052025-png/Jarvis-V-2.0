"""Test the ACTION_PATTERN regex to find bugs."""
import re

# Current regex
ACTION_PATTERN = re.compile(r'\[ACTION:(\w+)\(([^)]*)\)\]', re.IGNORECASE)

# Test cases
tests = [
    ('[ACTION:open_file("resume.pdf")]', "open_file basic"),
    ('[ACTION:write_file("todo.txt", "1. Buy milk")]', "write_file basic"),
    ('[ACTION:write_file("notes.txt", "Hello (world)")]', "write_file with parens in content"),
    ('[ACTION:play_youtube("lofi beats")]', "play_youtube"),
    ('[ACTION:read_file("notes.txt")]', "read_file"),
    ('Some text [ACTION:open_file("report.docx")] more text', "embedded in text"),
    ('[ACTION:write_file("story.txt", "Line 1\nLine 2")]', "write_file with newline"),
]

print("=== REGEX BUG REPORT ===\n")
for text, label in tests:
    match = ACTION_PATTERN.search(text)
    if match:
        tool = match.group(1)
        params = match.group(2)
        print(f"OK   | {label}")
        print(f"       tool={tool}, params={params!r}")
    else:
        print(f"FAIL | {label}")
        print(f"       text={text!r}")
    print()
