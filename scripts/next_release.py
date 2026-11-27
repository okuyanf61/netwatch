#!/usr/bin/env python3
"""Work out the next release from Conventional Commits since the last vX.Y.Z tag.

    feat: ...                         -> minor  (1.2.3 -> 1.3.0)
    fix: ... / perf: ...              -> patch  (1.2.3 -> 1.2.4)
    feat!: ... / BREAKING CHANGE: ... -> major  (1.2.3 -> 2.0.0)
    docs:, ci:, build:, chore:, ...   -> no release

Prints the new version (without "v") or nothing when no release is due.
If a path is given as the first argument, Markdown release notes are written there.
"""
import re
import subprocess
import sys

HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<desc>.+)$")
BREAKING = re.compile(r"^BREAKING[ -]CHANGE: ", re.M)


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def last_tag():
    try:
        return git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*").strip()
    except subprocess.CalledProcessError:
        return ""


def main():
    tag = last_tag()
    major, minor, patch = map(int, tag[1:].split(".")) if tag else (0, 0, 0)
    log = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{tag}..HEAD" if tag else "HEAD")

    level = 0  # 0 none, 1 patch, 2 minor, 3 major
    sections = {"Breaking changes": [], "Features": [], "Fixes": []}
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, body = rec.split("\x1f", 2)
        m = HEADER.match(subject)
        if not m:
            continue
        kind = m["type"].lower()
        line = f"- {'**' + m['scope'] + ':** ' if m['scope'] else ''}{m['desc']} ({sha[:7]})"
        if m["bang"] or BREAKING.search(body):
            level = 3
            sections["Breaking changes"].append(line)
        elif kind == "feat":
            level = max(level, 2)
            sections["Features"].append(line)
        elif kind in ("fix", "perf"):
            level = max(level, 1)
            sections["Fixes"].append(line)

    if not level:
        return
    if level == 3:
        version = f"{major + 1}.0.0"
    elif level == 2:
        version = f"{major}.{minor + 1}.0"
    else:
        version = f"{major}.{minor}.{patch + 1}"
    print(version)

    if len(sys.argv) > 1:
        out = []
        for title, lines in sections.items():
            if lines:
                out += [f"### {title}", *reversed(lines), ""]  # oldest first
        with open(sys.argv[1], "w") as f:
            f.write("\n".join(out))


if __name__ == "__main__":
    main()
