"""Fail closed before publishing the allowlisted, data-free GitHub source tree."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TOP = {
    ".dockerignore", ".env.example", ".gitignore", ".github/FUNDING.yml", ".github/workflows/ci.yml",
    "Dockerfile", "compose.yaml", "LICENSE", "README.md", "scripts/check-publication.py", "scripts/browser-smoke.py",
    "server/__init__.py", "server/app.py", "server/hermes.py", "server/pwa.py",
    "server/tests/__init__.py", "server/tests/test_pwa.py",
    "server/tests/test_delivery_core.py", "server/tests/browser-smoke.cjs",
}
WEB_EXTENSIONS = {".js", ".css", ".html", ".webmanifest", ".svg", ".png"}
SAFE_IMAGES = {"server/web/icon-192.png", "server/web/icon-512.png"}
FORBIDDEN = [
    re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b(?:/Users|/home)/[A-Za-z0-9_-]+/"),
    re.compile(r"\b(?:gho_|ghp_|github_pat_|sk-[A-Za-z0-9]{18,}|xox[baprs]-)[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"[A-Za-z0-9._-]+\.home\.[A-Za-z0-9.-]+"),
    re.compile(r"@(?:mmusama|example-internal)\.com\b", re.I),
    re.compile(r"\b(?:gitea|youtrack)\.[A-Za-z0-9.-]*mmusama\.com\b", re.I),
]


def main():
    output = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT)
    paths = [p.decode() for p in output.split(b"\0") if p]
    if not paths:
        raise SystemExit("No staged files to audit")
    failures = []
    for path in paths:
        file = ROOT / path
        if not (path in TOP or (path.startswith("server/web/") and
                                file.suffix in WEB_EXTENSIONS)):
            failures.append(f"Unapproved tracked file: {path}")
            continue
        if not file.is_file() or file.is_symlink():
            failures.append(f"Missing or symbolic-linked file: {path}")
            continue
        if file.suffix == ".png":
            if path not in SAFE_IMAGES:
                failures.append(f"Unexpected binary media: {path}")
            continue
        source = file.read_text(encoding="utf-8")
        if path == "scripts/check-publication.py":
            continue  # The deny patterns are documented in this scanner.
        for pattern in FORBIDDEN:
            if pattern.search(source):
                failures.append(f"Sensitive identifier in {path}: {pattern.pattern}")
    if failures:
        print("PUBLICATION AUDIT FAILED", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"PUBLICATION_AUDIT=PASS files={len(paths)} no private hostnames, addresses, keys, or unapproved paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
