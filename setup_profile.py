"""One-time setup: copy identity files from the real Chrome profile into the
trusted profile dir used by zillow_pull.py.

PerimeterX fingerprints the browser *profile* (trusted cookies + history), not
just the IP. A copy of a profile that has browsed Zillow passes both the page
and the internal API; fresh/automated profiles get challenged.

Usage:
    python setup_profile.py                 # copies from the default Chrome profile
    python setup_profile.py --profile path  # custom destination

Chrome must be fully closed (including the tray icon) before running.
"""

import argparse
import os
import shutil
import sys

IDENTITY_FILES = [
    "Network/Cookies",
    "Network/Cookies-journal",
    "Network/Network Persistent State",
    "Network/TransportSecurity",
    "Preferences",
    "Secure Preferences",
    "History",
    "Login Data",
    "Web Data",
    "Bookmarks",
    "Top Sites",
    "Network Action Predictor",
]


def default_source():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = os.path.join(local, "Google", "Chrome", "User Data", "Default")
        if os.path.isdir(cand):
            return cand
    return None


def copy_profile(source, dest):
    if not source or not os.path.isdir(source):
        print(f"FATAL: source Chrome profile not found: {source!r}", file=sys.stderr)
        print("Pass --source explicitly if Chrome is not in the default location.", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.join(dest, "Network"), exist_ok=True)
    copied = 0
    for rel in IDENTITY_FILES:
        src = os.path.join(source, rel)
        if not os.path.exists(src):
            continue
        shutil.copy2(src, os.path.join(dest, rel))
        copied += 1
    print(f"copied {copied} identity files into {dest}")
    if copied == 0:
        print("WARNING: nothing copied — is the source profile valid?", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None,
                    help="real Chrome profile dir (default: "
                         "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default)")
    ap.add_argument("--profile", default=os.path.join(os.path.expanduser("~"),
                                                     ".zillow-puller", "real-profile-copy"),
                    help="destination dir (default: ~/.zillow-puller/real-profile-copy)")
    args = ap.parse_args()
    source = args.source or default_source()
    copy_profile(source, args.profile)


if __name__ == "__main__":
    main()
