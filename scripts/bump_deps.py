#!/usr/bin/env python3
"""Hebt die festen Versionen von yt-dlp und gallery-dl in requirements.txt auf den neuesten
Stand von PyPI.

Läuft täglich in .github/workflows/bump-deps.yml. Ändert sich etwas, committet der Workflow
requirements.txt und pusht auf master — Coolify baut daraufhin neu. Die geänderte Datei sorgt
dafür, dass Docker den pip-Schritt nicht aus dem Cache nimmt.

Ausgabe: eine Zeile pro Änderung („yt-dlp 2026.08.19 → 2026.10.26"), sonst nichts.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

# Nur diese Pakete werden automatisch gehoben — sie müssen den Plattformen hinterherlaufen.
WATCHED = ("yt-dlp", "gallery-dl")

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)(?P<extras>\[[^\]]*\])?==(?P<version>[^\s#;]+)(?P<rest>.*)$")


def latest_version(package: str) -> str:
    """Neueste stabile Version laut PyPI (`info.version` lässt Vorabversionen aus)."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=30) as res:
        return json.load(res)["info"]["version"]


def bump(text: str, latest: dict[str, str]) -> tuple[str, list[str]]:
    """Ersetzt die Pins der beobachteten Pakete. Gibt neuen Text und die Änderungen zurück."""
    lines, changes = [], []
    for line in text.splitlines(keepends=True):
        m = PIN.match(line.rstrip("\n"))
        name = m and m["name"].lower()
        if m and name in latest and m["version"] != latest[name]:
            changes.append(f"{name} {m['version']} → {latest[name]}")
            newline = "\n" if line.endswith("\n") else ""
            line = f"{m['name']}{m['extras'] or ''}=={latest[name]}{m['rest']}{newline}"
        lines.append(line)
    return "".join(lines), changes


def main() -> int:
    text = REQUIREMENTS.read_text()
    pinned = {m["name"].lower() for line in text.splitlines() if (m := PIN.match(line))}
    missing = [p for p in WATCHED if p not in pinned]
    if missing:
        print(f"requirements.txt hat keinen festen Pin für: {', '.join(missing)}", file=sys.stderr)
        return 1

    new_text, changes = bump(text, {p: latest_version(p) for p in WATCHED})
    if changes:
        REQUIREMENTS.write_text(new_text)
        print("\n".join(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
