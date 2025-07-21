import threading
import time
import socket
import re
import subprocess
import sys
import json
import os
from collections import deque
import random
import struct
import udp_message_funct

# Your callsign
CALLSIGN = "5Z4XB"
UDP_PORT = 2237

# Regex patterns for QSO and CQ detection
qso_start_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b\s+([A-Z0-9]+)\b", re.IGNORECASE)
qso_finish_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b.*\b(RR?73|73)\b", re.IGNORECASE)
cq_pattern = re.compile(rf"FT8.*{re.escape(CALLSIGN)}", re.IGNORECASE)

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
        return False
    try:
        subprocess.check_call(["wmctrl", "-ia", wid])
        subprocess.check_call(["xte", "keydown Alt_L", "key n", "keyup Alt_L"])
        return True
    except subprocess.CalledProcessError as e:
        return False

def send_alt_6():
    wid = get_jtdx_window()
    if not wid:
        return False
    try:
        subprocess.check_call(["wmctrl", "-ia", wid])
        subprocess.check_call(["xte", "keydown Alt_L", "key 6", "keyup Alt_L"])
        return True
    except subprocess.CalledProcessError as e:
        return False

def send_alt_h():
    wid = get_jtdx_window()
    if not wid:
        return False
    try:
        subprocess.check_call(["wmctrl", "-ia", wid])
        subprocess.check_call(["xte", "keydown Alt_L", "key h", "keyup Alt_L"])
        return True
    except subprocess.CalledProcessError as e:
        return False

class Autotx73Daemon:
    def __init__(self):
        self.enabled = False
        self.tx_enabled = False
        self.last_tx_time = time.time()
        self.messages = deque(maxlen=10)
        self.running = True
        self.lock = threading.Lock()
        self.qso_partner = None
        self.last_qso_partner = None
        self.cq_active = False
        self.countdown_active = False
        self.countdown_max = 0
        self.countdown_value = 0
        self.countdown_label = ""
        self.qso_active = False
        self.qso_start_time = None
        self.qso_monitor_thread = threading.Thread(target=self.qso_inactivity_monitor, daemon=True)
        self.qso_monitor_thread.start()
        self.script_start_time = time.time()
        self.script_timer_triggered = False
        self.pending_script_timer_action = False
        self.status_thread = threading.Thread(target=self.status_and_command_worker, daemon=True)
        self.status_thread.start()
        self.udp_thread = threading.Thread(target=self.udp_listener, daemon=True)
        self.udp_thread.start()
        self.script_timer_thread = threading.Thread(target=self.script_timer_monitor, daemon=True)
        self.script_timer_thread.start()
        # Clear status and command files on startup
        try:
            open('/tmp/autotx73_status.json', 'w').close()
            open('/tmp/autotx73_command.txt', 'w').close()
        except Exception:
            pass
        self.add_message("System started in daemon mode. Use web UI to control.")
        self.udp_debug_log = 'udp_debug.log'
        self.udp_debug_max_size = 100 * 1024  # 100kB

    def add_message(self, msg):
        with self.lock:
            self.messages.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def reset_timer(self):
        self.last_tx_time = time.time()

    def start_countdown(self, seconds, label):
        def countdown_thread():
            self.countdown_active = True
            self.countdown_max = seconds
            self.countdown_label = label
            for i in range(seconds + 1):
                self.countdown_value = i
                time.sleep(1)
            self.countdown_active = False
        t = threading.Thread(target=countdown_thread, daemon=True)
        t.start()

    def enable_system(self):
        self.enabled = True
        self.add_message("Enabling system: Sending Alt-6 (CQ)...")
        if send_alt_6():
            self.add_message("Alt-6 sent (CQ enabled). Waiting 2 seconds before proceeding...")
            time.sleep(2)
            self.add_message("Waiting 10 seconds before enabling TX...")
            self.reset_timer()
            def after_enable_countdown():
                self.add_message("Enabling TX (Alt-N)...")
                if not self.tx_enabled:
                    if send_alt_n():
                        self.add_message("Alt-N sent - Tx toggled")
                        self.add_message("TX enabled (Alt-N sent). System is now active.")
                        self.tx_enabled = True
                        self.reset_timer()
                    else:
                        self.add_message("Failed to send Alt-N (TX enable).")
                else:
                    self.add_message("TX already enabled, not sending Alt-N again.")
            self.start_countdown(10, "Enabling:")
            threading.Thread(target=lambda: (time.sleep(10), after_enable_countdown()), daemon=True).start()
        else:
            self.add_message("Failed to send Alt-6 (CQ enable).")

    def disable_system(self):
        self.add_message("System disabled by user. Sending Alt-N to turn off enable TX...")
        if not self.tx_enabled or send_alt_n():
            self.add_message("Alt-N sent to disable TX.")
            self.tx_enabled = False
        else:
            self.add_message("Failed to send Alt-N to disable TX.")
        def after_disable_countdown():
            self.add_message("Sending Alt-H to halt TX...")
            send_alt_h()
            self.enabled = False
            self.qso_partner = None
            self.cq_active = False
        self.start_countdown(5, "Disabling:")
        threading.Thread(target=lambda: (time.sleep(5), after_disable_countdown()), daemon=True).start()

    def log_udp_debug(self, msg):
        try:
            # Write message to log file
            with open(self.udp_debug_log, 'a') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            # Check file size and rotate if needed
            if os.path.getsize(self.udp_debug_log) > self.udp_debug_max_size:
                with open(self.udp_debug_log, 'rb') as f:
                    f.seek(-self.udp_debug_max_size, os.SEEK_END)
                    data = f.read()
                with open(self.udp_debug_log, 'wb') as f:
                    f.write(data)
        except Exception:
            pass

    def udp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", UDP_PORT))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        prev_tx_enabled = self.tx_enabled
        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
                self.parse_status_message(data)
                text = data.decode('ascii', errors='ignore')
                # Detect TX enable/disable from UDP message
                tx_state = udp_message_funct.decode_jtdx_tx_enable(data)
                if tx_state == "on":
                    if not self.tx_enabled:
                        self.add_message("[UDP] TX enabled detected from UDP message.")
                    self.tx_enabled = True
                elif tx_state == "off":
                    if self.tx_enabled:
                        self.add_message("[UDP] TX disabled detected from UDP message.")
                    self.tx_enabled = False
            except Exception:
                continue
            now = time.time()
            # Debug: log every received message
            self.log_udp_debug(f"UDP: {text.strip()}")
            match = qso_start_pattern.search(text)
            if match:
                self.log_udp_debug(f"QSO start pattern matched: {match.group(0)}")
            if match:
                partner = match.group(1)
                if not self.qso_partner or self.qso_partner != partner:
                    self.qso_partner = partner
                    self.add_message(f"QSO started with {partner}.")
                self.qso_active = True
                self.qso_start_time = time.time()  # Only reset here
                self.reset_timer()
            finish_match = qso_finish_pattern.search(text)
            if finish_match:
                self.log_udp_debug(f"QSO finish pattern matched: {finish_match.group(0)}")
            if finish_match:
                partner = self.qso_partner if self.qso_partner else "Unknown"
                self.add_message(f"QSO with {partner} finished.")
                self.last_qso_partner = partner
                self.qso_partner = None
                self.qso_active = False
                # self.qso_start_time = None  # Do not reset here
                self.reset_timer()
                def post_qso_reenable():
                    if self.script_timer_triggered or self.pending_script_timer_action:
                        self.script_timer_triggered = False
                        self.pending_script_timer_action = False
                        delay = random.randint(180, 600)
                        self.add_message(f"Script active >60 min. Waiting {delay//60} min {delay%60} sec before CQ restart...")
                        self.countdown_active = True
                        self.countdown_max = delay
                        self.countdown_label = "CQ restart delay:"
                        for i in range(delay + 1):
                            self.countdown_value = i
                            self.write_status()
                            time.sleep(1)
                        self.countdown_active = False
                        self.write_status()
                        # Ensure TX is off before restarting CQ
                        if self.tx_enabled:
                            self.add_message("Disabling TX before CQ restart (60-min rule)...")
                            if send_alt_n():
                                self.tx_enabled = False
                                self.add_message("TX disabled (Alt-N sent).")
                            else:
                                self.add_message("Failed to disable TX (Alt-N) before CQ restart.")
                        self.add_message("Enabling CQ (Alt-6) after 60-min break...")
                        if send_alt_6():
                            self.add_message("CQ enabled (Alt-6 sent). Waiting 2 seconds...")
                            time.sleep(2)
                            self.add_message("Enabling TX (Alt-N) after CQ restart...")
                            if send_alt_n():
                                self.tx_enabled = True
                                self.add_message("TX enabled (Alt-N sent) after CQ restart.")
                            else:
                                self.add_message("Failed to enable TX (Alt-N) after CQ restart.")
                        else:
                            self.add_message("Failed to enable CQ (Alt-6) after 60-min break.")
                        self.script_start_time = time.time()
                    else:
                        self.add_message("Waiting 45 seconds before enabling TX...")
                        self.start_countdown(45, "Post-QSO delay:")
                        while self.countdown_active:
                            time.sleep(0.1)
                        if not self.tx_enabled:
                            if send_alt_n():
                                self.add_message("Alt-N sent - TX enabled after QSO.")
                                self.tx_enabled = True
                                self.reset_timer()
                            else:
                                self.add_message("Failed to send Alt-N after QSO.")
                        else:
                            self.add_message("TX already enabled, not sending Alt-N again.")
                threading.Thread(target=post_qso_reenable, daemon=True).start()

    def parse_status_message(self, data):
        try:
            if len(data) > 60:
                self.tx_enabled = data[60] == 1
        except Exception:
            pass

    def write_status(self):
        if self.enabled:
            now = time.time()
            elapsed = int(now - self.last_tx_time)
            mins, secs = divmod(elapsed, 60)
            qso_timer_str = f"Last QSO: {mins}m {secs}s"
            script_elapsed = int(now - self.script_start_time)
            script_mins, script_secs = divmod(script_elapsed, 60)
            script_timer_str = f"Script Uptime: {script_mins}m {script_secs}s"
        else:
            qso_timer_str = ""
            script_timer_str = ""
        status = {
            'enabled': self.enabled,
            'tx': self.tx_enabled,
            'qso_partner': self.qso_partner,
            'last_qso_partner': self.last_qso_partner,
            'messages': list(self.messages)[-10:],
            'countdown_active': self.countdown_active,
            'countdown_max': self.countdown_max,
            'countdown_value': self.countdown_value,
            'countdown_label': self.countdown_label,
            'qso_timer_str': qso_timer_str,
            'script_timer_str': script_timer_str
        }
        try:
            with open('/tmp/autotx73_status.json', 'w') as f:
                json.dump(status, f)
        except Exception:
            pass

    def check_command(self):
        command_file = '/tmp/autotx73_command.txt'
        if os.path.exists(command_file):
            try:
                with open(command_file) as f:
                    cmd = f.read().strip()
                if cmd == 'enable' and not self.enabled:
                    self.enable_system()
                elif cmd == 'disable' and self.enabled:
                    self.disable_system()
                os.remove(command_file)
            except Exception:
                pass

    def status_and_command_worker(self):
        while self.running:
            self.write_status()
            self.check_command()
            time.sleep(1)

    def qso_inactivity_monitor(self):
        while self.running:
            if self.enabled and self.qso_active and self.qso_start_time:
                elapsed = time.time() - self.qso_start_time
                if elapsed > 360:
                    self.add_message("QSO started but not completed for more than 6 minutes. Resetting to CQ with TX enabled...")
                    if self.tx_enabled:
                        self.add_message("Disabling TX before switching to CQ...")
                        if send_alt_n():
                            self.tx_enabled = False
                            self.add_message("TX disabled (Alt-N sent).")
                        else:
                            self.add_message("Failed to disable TX (Alt-N). Proceeding anyway.")
                    self.add_message("Switching CQ on (Alt-6)...")
                    if send_alt_6():
                        self.add_message("CQ enabled (Alt-6 sent). Waiting 2 seconds...")
                        time.sleep(2)
                        self.add_message("Enabling TX (Alt-N)...")
                        if send_alt_n():
                            self.tx_enabled = True
                            self.add_message("TX enabled (Alt-N sent). Back to CQ mode.")
                        else:
                            self.add_message("Failed to enable TX (Alt-N) after CQ.")
                    else:
                        self.add_message("Failed to enable CQ (Alt-6).")
                    # Reset QSO state so this doesn't fire again
                    self.qso_active = False
                    self.qso_start_time = None
            time.sleep(5)

    def script_timer_monitor(self):
        while self.running:
            if not self.script_timer_triggered and not self.pending_script_timer_action and (time.time() - self.script_start_time > 3600):
                if self.qso_active:
                    self.pending_script_timer_action = True
                else:
                    self.script_timer_triggered = True
            time.sleep(5)

if __name__ == "__main__":
    Autotx73Daemon()
    while True:
        time.sleep(1) 