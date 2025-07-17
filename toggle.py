#!/usr/bin/env python3
"""
Toggle Enable Tx in JTDX / WSJT‑X (Alt+N) – VNC‑friendly
"""

import subprocess, re, sys

ALT_N = ["xte", "keydown Alt_L", "key n", "keyup Alt_L"]   # ⬅ no  -x  here

def first_jtdx_window():
    out = subprocess.check_output(["wmctrl", "-lx"]).decode()
    for line in out.splitlines():
        wid, *rest, title = line.split(None, 4)
        if re.search(r"(JTDX|WSJT-X)", title, re.I):
            return wid
    return None

wid = first_jtdx_window() if len(sys.argv) == 1 else sys.argv[1]
if not wid:
    sys.exit("✘  No JTDX / WSJT‑X window found")

print(f"✓  Using window {wid}")
try:
    subprocess.check_call(["wmctrl", "-ia", wid])         # give it focus
    subprocess.check_call(ALT_N)                          # send Alt+N
    print("✅  Alt‑N sent – Tx toggled")
except subprocess.CalledProcessError as e:
    print("✘  Command failed:", e)
    sys.exit(1)