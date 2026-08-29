#!/usr/bin/env python3
"""build_all.py — Build every .c, .asm, and .ini gecko code in the repo.

Scans the "Gecko Codes" folder next to CGecko/, not the whole repo. Scoping it
that way keeps the sweep to actual gecko codes: a project can now hold sources
that are NOT built into the ini (mod packs, DOL-baked builds, tooling) without
build_all picking them up and deploying them by accident. Pass a different
folder as a positional argument to scan somewhere else.
"""

import sys
import os
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CGECKO     = os.path.join(SCRIPT_DIR, "cgecko.py")
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
CODES_DIR  = os.path.join(ROOT_DIR, "Gecko Codes")


def find_sources(root: str) -> list[str]:
    sources = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Don't descend into submodules (or any other nested git repo, e.g. a
        # decomp checkout) -- their sources aren't gecko codes, and a large
        # one can dominate the scan.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and not os.path.exists(os.path.join(dirpath, d, ".git"))]
        for name in filenames:
            if name.endswith(".rewritten.c"):
                continue
            if name.endswith((".c", ".asm", ".ini")):
                sources.append(os.path.join(dirpath, name))
    return sorted(sources)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build every .c, .asm, and .ini gecko code in the repo."
    )
    # Forwarded to cgecko to control each built code's enabled state in the ini.
    # The default leaves toggles alone: a newly added code isn't enabled and an
    # existing code keeps its current state, so a batch rebuild doesn't flip
    # anything. --enabled / --disabled override that for every code at once.
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--enabled", dest="state", action="store_const", const="--enabled",
                       help="Force every built code enabled in the ini, overriding existing toggles.")
    state.add_argument("--disabled", dest="state", action="store_const", const="--disabled",
                       help="Force every built code disabled in the ini, overriding existing toggles.")
    state.add_argument("--no-enable", dest="state", action="store_const", const="--no-enable",
                       help="Default: don't enable newly added codes; leave existing toggles alone.")
    parser.add_argument("--dir", default=CODES_DIR,
                        help='Folder to scan (default: the "Gecko Codes" folder beside CGecko/).')
    # Anything else (e.g. -d) is forwarded to cgecko untouched.
    args, passthrough = parser.parse_known_args(argv)
    return args, passthrough


def main():
    args, passthrough = parse_args()
    state_flag = args.state or "--no-enable"

    if not os.path.isdir(args.dir):
        print(f"[ERROR] no such folder: {args.dir}")
        print('        build_all scans "<repo>/Gecko Codes" by default; use --dir to override.')
        return 1

    sources = find_sources(args.dir)
    if not sources:
        print(f"[INFO] No .c, .asm, or .ini source files found under {args.dir}")
        return

    print(f"[INFO] Building {len(sources)} file(s)...\n")

    passed: list[str] = []
    failed: list[str] = []

    for src in sources:
        rel = os.path.relpath(src, ROOT_DIR)
        print(f"{'─' * 60}")
        print(f"Building: {rel}")
        print(f"{'─' * 60}")
        result = subprocess.run(
            [sys.executable, CGECKO, state_flag, "--no-launch", *passthrough, src],
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            passed.append(rel)
        else:
            failed.append(rel)
        print()

    print("=" * 60)
    print(f"  {len(passed)} passed  |  {len(failed)} failed  |  {len(sources)} total")
    print("=" * 60)

    if failed:
        print("\n[FAILED]")
        for f in failed:
            print(f"  {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()