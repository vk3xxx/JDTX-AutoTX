import curses
import threading
import time
import subprocess
import sys
import socket
import re
from collections import deque
import os
import struct
import json

# Your callsign
CALLSIGN = "5Z4XB"
UDP_PORT = 2237

# Regex patterns for QSO and CQ detection
qso_start_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b\s+([A-Z0-9]+)\b", re.IGNORECASE)
qso_finish_pattern = re.compile(rf"\b{re.escape(CALLSIGN)}\b.*\b(RR?73|73)\b", re.IGNORECASE)
cq_pattern = re.compile(rf"FT8.*{re.escape(CALLSIGN)}", re.IGNORECASE)

# Helper functions for keystrokes

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

def refocus_own_terminal(add_message=None):
    import subprocess
    import os
    import time
    status = None
    try:
        # 1. Try to refocus by window class (lxterminal.LXterminal)
        out = subprocess.check_output(['wmctrl', '-lx']).decode()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            win_id, win_class = parts[0], parts[2]
            if win_class.lower() == 'lxterminal.lxterminal':
                subprocess.call(['wmctrl', '-ia', win_id])
                status = 'Refocused LXTerminal window (class match)'
                if add_message:
                    add_message(status)
                return True
        # 2. Try to refocus by window title substring (pi@digipi)
        for line in out.splitlines():
            if 'pi@digipi' in line:
                win_id = line.split()[0]
                subprocess.call(['wmctrl', '-ia', win_id])
                status = 'Refocused LXTerminal window (title match)'
                if add_message:
                    add_message(status)
                return True
        # 3. Fallback: walk up the process tree to find any ancestor PID that matches a window
        import psutil
        p = psutil.Process(os.getpid())
        ancestor_pids = [p.pid]
        try:
            while True:
                p = p.parent()
                if not p:
                    break
                ancestor_pids.append(p.pid)
        except Exception:
            pass
        out2 = subprocess.check_output(['wmctrl', '-lp']).decode()
        for pid in ancestor_pids:
            for line in out2.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                win_id, win_pid = parts[0], parts[2]
                if str(pid) == win_pid:
                    subprocess.call(['wmctrl', '-ia', win_id])
                    status = 'Refocused terminal window (PID match)'
                    if add_message:
                        add_message(status)
                    return True
        # 4. Fallback: try to refocus by TERM env
        term = os.environ.get('TERM_PROGRAM') or os.environ.get('TERM') or 'Terminal'
        for line in out2.splitlines():
            if term in line:
                win_id = line.split()[0]
                subprocess.call(['wmctrl', '-ia', win_id])
                status = 'Refocused terminal window (env fallback)'
                if add_message:
                    add_message(status)
                return True
    except Exception as e:
        status = 'Could not refocus terminal window'
    if add_message:
        add_message(status or 'Could not refocus terminal window')
    return False

class Autotx73UI:
    def __init__(self, stdscr):
        self.last_qso_partner = None
        self.stdscr = stdscr
        self.tx_enabled = False  # Ensure this is set before threads start
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)   # Enabled: white on red
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_GREEN) # Disabled: white on green
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE) # Countdown/message: black on white
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE) # Main area: black on white
        self.enabled = False
        self.last_tx_time = time.time()
        self.messages = deque(maxlen=10)
        self.running = True
        self.lock = threading.Lock()
        self.qso_partner = None  # Ensure this is set before threads start
        self.cq_active = False   # Ensure this is set before threads start
        self.countdown_active = False
        self.countdown_max = 0
        self.countdown_value = 0
        self.countdown_label = ""
        self.timer_thread = threading.Thread(target=self.update_timer, daemon=True)
        self.timer_thread.start()
        self.udp_thread = threading.Thread(target=self.udp_listener, daemon=True)
        self.udp_thread.start()
        self.add_message("System started. Press E to enable, D to disable, Q to quit.")
        self.status_thread = threading.Thread(target=self.status_and_command_worker, daemon=True)
        self.status_thread.start()
        # Clear status and command files on startup
        try:
            open('/tmp/autotx73_status.json', 'w').close()
            open('/tmp/autotx73_command.txt', 'w').close()
        except Exception:
            pass

    def add_message(self, msg):
        with self.lock:
            self.messages.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def update_timer(self):
        while self.running:
            self.draw()
            time.sleep(1)

    def reset_timer(self):
        self.last_tx_time = time.time()

    def start_countdown(self, seconds, label):
        def countdown_thread():
            self.countdown_active = True
            self.countdown_max = seconds
            self.countdown_label = label
            for i in range(seconds + 1):
                self.countdown_value = i
                self.draw()
                time.sleep(1)
            self.countdown_active = False
            self.draw()
        t = threading.Thread(target=countdown_thread, daemon=True)
        t.start()

    def enable_system(self):
        self.enabled = True
        self.add_message("Enabling system: Sending Alt-6 (CQ)...")
        if send_alt_6():
            self.add_message("Alt-6 sent (CQ enabled). Waiting 10 seconds...")
            self.reset_timer()
            def after_enable_countdown():
                self.add_message("Enabling TX (Alt-N)...")
                if send_alt_n():
                    self.add_message("Alt-N sent - Tx toggled")
                    self.add_message("TX enabled (Alt-N sent). System is now active.")
                    self.reset_timer()
                    refocus_own_terminal(self.add_message)
                else:
                    self.add_message("Failed to send Alt-N (TX enable).")
            self.start_countdown(10, "Enabling:")
            threading.Thread(target=lambda: (time.sleep(10), after_enable_countdown()), daemon=True).start()
        else:
            self.add_message("Failed to send Alt-6 (CQ enable).")

    def send_alt_h(self):
        wid = get_jtdx_window()
        if not wid:
            self.add_message("✘  No JTDX / WSJT‑X window found for Alt-H")
            return False
        try:
            subprocess.check_call(["wmctrl", "-ia", wid])
            subprocess.check_call(["xte", "keydown Alt_L", "key h", "keyup Alt_L"])
            self.add_message("Alt-H sent - Halt TX")
            return True
        except subprocess.CalledProcessError as e:
            self.add_message(f"✘  Command failed for Alt-H: {e}")
            return False

    def disable_system(self):
        self.add_message("System disabled by user. Sending Alt-N to turn off enable TX...")
        if send_alt_n():
            self.add_message("Alt-N sent to disable TX.")
        else:
            self.add_message("Failed to send Alt-N to disable TX.")
        def after_disable_countdown():
            self.add_message("Sending Alt-H to halt TX...")
            self.send_alt_h()
            self.enabled = False
            self.qso_partner = None
            self.cq_active = False
            refocus_own_terminal(self.add_message)
        self.start_countdown(5, "Disabling:")
        threading.Thread(target=lambda: (time.sleep(5), after_disable_countdown()), daemon=True).start()

    def parse_status_message(self, data):
        try:
            # Log the first 64 bytes after the type field for every UDP packet
            with open('tx_debug.log', 'a') as f:
                f.write("All bytes after type: " + " ".join(f"{b:02x}" for b in data[12:76]) + f" | Data: {data.hex()}\n")
            # Set self.tx_enabled based on byte at offset 60 (data[60])
            if len(data) > 60:
                self.tx_enabled = data[60] == 1
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
            except Exception:
                continue
            now = time.time()
            # Force UI update on TX state change
            if self.tx_enabled != prev_tx_enabled:
                self.draw()
            if self.tx_enabled and not prev_tx_enabled:
                self.reset_timer()
            prev_tx_enabled = self.tx_enabled
            # QSO logic remains
            match = qso_start_pattern.search(text)
            if match:
                partner = match.group(1)
                if not self.qso_partner or self.qso_partner != partner:
                    self.qso_partner = partner
                    self.add_message(f"QSO started with {partner}.")
                self.reset_timer()
            if qso_finish_pattern.search(text):
                partner = self.qso_partner if self.qso_partner else "Unknown"
                self.add_message(f"QSO with {partner} finished.")
                self.last_qso_partner = partner
                self.qso_partner = None
                self.reset_timer()
                # Start post-QSO delay and re-enable TX in a background thread
                def post_qso_reenable():
                    self.add_message("Waiting 45 seconds before re-enabling TX...")
                    self.start_countdown(45, "Post-QSO delay:")
                    while self.countdown_active:
                        time.sleep(0.1)
                    self.add_message(f"Re-enabling TX (Alt-N) after QSO with {partner}...")
                    if send_alt_n():
                        self.add_message("Alt-N sent - TX re-enabled after QSO.")
                        self.reset_timer()
                        refocus_own_terminal(self.add_message)
                    else:
                        self.add_message("Failed to send Alt-N after QSO.")
                threading.Thread(target=post_qso_reenable, daemon=True).start()

    def write_status(self):
        if self.enabled:
            now = time.time()
            elapsed = int(now - self.last_tx_time)
            mins, secs = divmod(elapsed, 60)
            qso_timer_str = f"Last QSO: {mins}m {secs}s"
        else:
            qso_timer_str = ""
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
            'qso_timer_str': qso_timer_str
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

    def draw(self):
        max_y, max_x = self.stdscr.getmaxyx()
        border_thickness = 2  # Double border all around
        color = curses.color_pair(1) if self.enabled else curses.color_pair(2)
        # Fill main area with white background
        for y in range(border_thickness, max_y - border_thickness):
            try:
                self.stdscr.addstr(y, border_thickness, " " * (max_x - 2 * border_thickness), curses.color_pair(4))
            except curses.error:
                pass
        # Top and bottom double rows
        for y in range(border_thickness):
            for x in range(max_x):
                try:
                    self.stdscr.addstr(y, x, " ", color)
                    self.stdscr.addstr(max_y - 1 - y, x, " ", color)
                except curses.error:
                    pass
        # Left and right double columns
        for y in range(border_thickness, max_y - border_thickness):
            for x in range(border_thickness):
                try:
                    self.stdscr.addstr(y, x, " ", color)
                    self.stdscr.addstr(y, max_x - 1 - x, " ", color)
                except curses.error:
                    pass
        # Top right: timer and TX status
        elapsed = int(time.time() - self.last_tx_time)
        mins, secs = divmod(elapsed, 60)
        timer_str = f"Time since last TX: {mins:02d}:{secs:02d}"
        tx_str = "TX: ON" if self.tx_enabled else "TX: OFF"
        try:
            self.stdscr.addstr(0, max_x - len(timer_str) - len(tx_str) - 4, timer_str + "  " + tx_str, curses.color_pair(4))
        except curses.error:
            pass
        # Top left: QSO/CQ status
        qso_str = f"QSO: {self.qso_partner}" if self.qso_partner else "QSO: None"
        cq_str = "CQ: ACTIVE" if self.cq_active else "CQ: -"
        try:
            self.stdscr.addstr(0, border_thickness + 2, qso_str + "   " + cq_str, curses.color_pair(4))
        except curses.error:
            pass
        # Dynamic message area: centered, up to 10 lines, never overlapping border or controls
        msg_area_height = min(10, max_y - 2 * border_thickness - 7)
        msg_area_top = border_thickness + (max_y - 2 * border_thickness - msg_area_height - 7) // 2
        # Countdown bar: always drawn as part of main UI if active
        if self.countdown_active:
            bar_width = 30 if self.countdown_max >= 10 else 20
            bar_y = max_y - border_thickness - 1  # Just above the border
            bar_x = max_x // 2 - bar_width // 2
            label = self.countdown_label
            label_x = bar_x - len(label) - 2
            try:
                self.stdscr.addstr(bar_y, border_thickness, " " * (max_x - 2 * border_thickness), curses.color_pair(4))
                self.stdscr.addstr(bar_y, label_x, label, curses.color_pair(4))
                self.stdscr.addstr(bar_y, bar_x, " " * bar_width, curses.color_pair(4))
                filled = int(bar_width * self.countdown_value / self.countdown_max) if self.countdown_max > 0 else bar_width
                bar = "█" * filled
                self.stdscr.addstr(bar_y, bar_x, bar.ljust(bar_width), curses.color_pair(3))
                time_str = f"{self.countdown_value}/{self.countdown_max}s"
                self.stdscr.addstr(bar_y, bar_x + bar_width + 2, time_str.ljust(len(f"{self.countdown_max}/{self.countdown_max}s")), curses.color_pair(4))
            except curses.error:
                pass
        # Message area
        msgs_to_show = list(self.messages)[-msg_area_height:]
        msgs_to_show = ["" for _ in range(msg_area_height - len(msgs_to_show))] + msgs_to_show
        for i, msg in enumerate(msgs_to_show):
            try:
                self.stdscr.addstr(msg_area_top + i, border_thickness + 2, msg.ljust(max_x - 2 * border_thickness - 4)[:max_x - 2 * border_thickness - 4], curses.color_pair(4))
            except curses.error:
                pass
        # Bottom: controls (no status messages or bar here)
        status = "ENABLED" if self.enabled else "DISABLED"
        try:
            self.stdscr.addstr(max_y - border_thickness - 3, border_thickness + 2, f"System status: {status}", curses.color_pair(4))
            self.stdscr.addstr(max_y - border_thickness - 2, border_thickness + 2, "[E]nable  [D]isable  [Q]uit".ljust(max_x - 2 * border_thickness - 4), curses.color_pair(4))
        except curses.error:
            pass
        self.stdscr.refresh()

    def run(self):
        while self.running:
            c = self.stdscr.getch()
            if c in (ord('q'), ord('Q')):
                if self.enabled:
                    self.disable_system()
                self.running = False
                return  # Quit after system is fully disabled
            elif c in (ord('e'), ord('E')):
                if not self.enabled:
                    self.enable_system()
                else:
                    self.add_message("System already enabled.")
            elif c in (ord('d'), ord('D')):
                if self.enabled:
                    self.disable_system()
                else:
                    self.add_message("System already disabled.")
            self.draw()


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    ui = Autotx73UI(stdscr)
    ui.draw()
    ui.run()

if __name__ == "__main__":
    curses.wrapper(main) 