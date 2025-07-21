import socket
import re
import time
import subprocess
import sys
import random
import threading

# Your callsign
CALLSIGN = "5Z4XB"
# UDP listening port
UDP_PORT = 2237

# Regex patterns for QSO and CQ detection
complete_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b.*\b(?:RR?73)\b", re.IGNORECASE)
start_pattern = re.compile(rf"\b([A-Z0-9]+)\s+{re.escape(CALLSIGN)}\b", re.IGNORECASE)
qso_start_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b\s+([A-Z0-9]+)\b", re.IGNORECASE)
qso_finish_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b.*\b(RR?73|73)\b", re.IGNORECASE)
cq_pattern = re.compile(rf"\bCQ\s+{re.escape(CALLSIGN)}\b", re.IGNORECASE)

DEBOUNCE_INTERVAL = 5  # seconds
last_complete_time = 0
in_qso = False

# CQ restart logic state
last_qso_time = time.time()
cq_restart_active = False
cq_restart_start_time = None
cq_seen_during_restart = False

script_start_time = time.time()

def get_jtdx_window():
    try:
        out = subprocess.check_output(["wmctrl", "-lx"]).decode()
        for line in out.splitlines():
            wid, *rest, title = line.split(None, 4)
            if re.search(r"(JTDX|WSJT-X)", title, re.I):
                return wid
    except Exception:
        pass
    return None

def send_alt_n():
    wid = get_jtdx_window()
    if not wid:
        print("✘  No JTDX / WSJT‑X window found")
        return
    try:
        subprocess.check_call(["wmctrl", "-ia", wid])
        subprocess.check_call(["xte", "keydown Alt_L", "key n", "keyup Alt_L"])
        print("✅  Alt‑N sent – Tx toggled")
    except subprocess.CalledProcessError as e:
        print("✘  Command failed:", e)

def send_alt_6():
    wid = get_jtdx_window()
    if not wid:
        print("✘  No JTDX / WSJT‑X window found")
        return
    try:
        subprocess.check_call(["wmctrl", "-ia", wid])
        subprocess.check_call(["xte", "keydown Alt_L", "key 6", "keyup Alt_L"])
        print("✅  Alt‑6 sent")
    except subprocess.CalledProcessError as e:
        print("✘  Command failed:", e)

def print_qso_timer():
    while True:
        now = time.time()
        elapsed = int(now - last_qso_time)
        mins, secs = divmod(elapsed, 60)
        print(f"[QSO Timer] Time since last QSO or transmission: {mins} min {secs} sec")
        time.sleep(60)

# Start the QSO timer thread
qso_timer_thread = threading.Thread(target=print_qso_timer, daemon=True)
qso_timer_thread.start()

# Set up UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", UDP_PORT))
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
print(f"✔ Listening on 0.0.0.0:{UDP_PORT} (broadcast enabled)")

while True:
    data, addr = sock.recvfrom(4096)
    try:
        text = data.decode('ascii', errors='ignore')
    except:
        continue

    now = time.time()

    # Detect CQ call from our callsign
    if cq_pattern.search(text):
        if in_qso:
            print(f"QSO aborted: CQ detected from {CALLSIGN} during QSO with {other_callsign if 'other_callsign' in locals() else 'UNKNOWN'}")
            in_qso = False
        if cq_restart_active:
            cq_seen_during_restart = True

    # Detect start of QSO (your callsign followed by another callsign)
    match = qso_start_pattern.search(text)
    if match and not in_qso:
        other_callsign = match.group(1)
        start_time = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"🟢 --- New QSO started with {other_callsign} at {start_time} ---")
        in_qso = True
        last_qso_time = now  # Reset timer ONLY on QSO start
    # If already in QSO, check if the callsign changes
    elif match and in_qso:
        new_callsign = match.group(1)
        if 'other_callsign' in locals() and new_callsign != other_callsign:
            print(f"QSO partner changed: Now in QSO with {new_callsign} (was {other_callsign})")
            other_callsign = new_callsign

    # Detect completion (your callsign and RR73 or 73)
    if in_qso and qso_finish_pattern.search(text):
        if now - last_complete_time > DEBOUNCE_INTERVAL:
            last_complete_time = now
            complete_time = time.strftime('%Y-%m-%d %H:%M:%S')
            print(f"✅ --- QSO finished at {complete_time} ---")
            # Do NOT reset last_qso_time here
            # After 60 minutes of script activity, randomize CQ re-enable
            if now - script_start_time > 3600:
                delay = random.randint(180, 600)
                print(f"Waiting {delay//60} min {delay%60} sec before re-enabling CQ (Alt-6)...")
                for i in range(delay):
                    bar = ('#' * ((i+1) * 45 // delay)).ljust(45)
                    remaining = delay - i - 1
                    mins, secs = divmod(remaining, 60)
                    sys.stdout.write(f"\r[CQ restart delay] [{bar}] {mins:02d}:{secs:02d} remaining ")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
                send_alt_6()
                print("--- CQ re-enabled (Alt-6 sent to JTDX) ---")
                print("Waiting 60 seconds before enabling TX...")
                for i in range(61):
                    bar = ('#' * (i % 45)).ljust(45)
                    sys.stdout.write(f"\r[TX delay] [{bar}] {i}/60s")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
                send_alt_n()
                print("--- TX enabled (Alt-N sent to JTDX) ---")
                script_start_time = time.time()  # Reset 60-min timer after random shutdown
                last_qso_time = time.time()  # Reset timer ONLY when TX is enabled
            else:
                print("Waiting 45 seconds before enabling TX...")
                for i in range(46):
                    bar = ('#' * i).ljust(45)
                    sys.stdout.write(f"\r[{bar}] {i}/45s")
                    sys.stdout.flush()
                    time.sleep(1)
                print()
                send_alt_n()
                print("--- TX enabled (Alt-N sent to JTDX) ---")
                last_qso_time = time.time()  # Reset timer ONLY when TX is enabled
            in_qso = False
            cq_restart_active = False
            cq_seen_during_restart = False
            cq_restart_start_time = None

    # CQ restart logic
    if not cq_restart_active and (now - last_qso_time > 300):
        print("CQ restart: No new QSO in 5 minutes, sending Alt-6 and waiting for CQ message...")
        send_alt_6()
        cq_restart_active = True
        cq_restart_start_time = now
        cq_seen_during_restart = False
        # Do NOT reset last_qso_time here

    # Only monitor for CQ during the 1-min window after Alt-6
    if cq_restart_active and cq_restart_start_time is not None:
        if now - cq_restart_start_time <= 60:
            # If CQ from us detected during this window, handle immediately
            if cq_seen_during_restart:
                print("TX already enabled (CQ detected). Timers reset.")
                cq_restart_active = False
                cq_seen_during_restart = False
                cq_restart_start_time = None
                last_qso_time = now  # Reset timer ONLY if TX is enabled (CQ detected means TX is on)
        elif now - cq_restart_start_time > 60:
            # 1 min passed, no CQ detected
            if not cq_seen_during_restart:
                print("CQ restart: No CQ detected in 1 minute, sending Alt-N to enable TX.")
            send_alt_n()
            print("No CQ detected, TX enabled. Timers reset.")
            cq_restart_active = False
            cq_seen_during_restart = False
            cq_restart_start_time = None
            last_qso_time = now  # Reset timer ONLY when TX is enabled

