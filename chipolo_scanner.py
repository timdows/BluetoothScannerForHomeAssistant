#!/usr/bin/env python3
"""
Chipolo presence scanner for Raspberry Pi (BlueZ / built-in Bluetooth).

Continuously scans Bluetooth Low Energy advertisements and reports which
Chipolo tags are nearby, their signal strength, and arrive/leave events.
With an `mqtt:` section in config.yaml every Chipolo is also sent to
Home Assistant (see ha_mqtt.py).

Usage examples:
    python3 chipolo_scanner.py                    # live table; uses config.yaml if present
    python3 chipolo_scanner.py --config /path/config.yaml --plain
    python3 chipolo_scanner.py --discover         # list ALL BLE devices
    python3 chipolo_scanner.py --mac AA:BB:CC:DD:EE:FF --name "Keys"
    python3 chipolo_scanner.py --near -70 --timeout 30 \
        --on-arrive "echo {name} arrived" --on-leave "echo {name} left"
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time

from bleak import BleakScanner

# 16-bit service UUIDs registered to CHIPOLO d.o.o. by the Bluetooth SIG
CHIPOLO_UUIDS = {
    "0000fe33-0000-1000-8000-00805f9b34fb",
    "0000fe65-0000-1000-8000-00805f9b34fb",
}
APPLE_COMPANY_ID = 0x004C
FINDMY_TYPE = 0x12  # Apple "Find My" offline-finding advertisement

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"near": -85, "timeout": 60, "update_interval": 30, "rssi_delta": 3}
KEEPALIVE = 300  # always resend a visible tag's state at least this often (seconds)


def now_str():
    return dt.datetime.now().strftime("%H:%M:%S")


def estimate_distance(rssi, tx_power=-59, n=2.5):
    """Very rough distance estimate in meters (log-distance path loss)."""
    return 10 ** ((tx_power - rssi) / (10 * n))


class Tag:
    def __init__(self, address, label, kind):
        self.address = address
        self.label = label
        self.kind = kind
        self.rssi_history = []
        self.last_seen = 0.0
        self.last_seen_wall = None
        self.present = False
        self.last_pub = None  # monotonic time of last MQTT publish
        self.last_pub_rssi = None

    @property
    def rssi(self):
        # Smooth RSSI over the last few samples; raw RSSI is very noisy
        if not self.rssi_history:
            return -127
        return sum(self.rssi_history) / len(self.rssi_history)

    def update(self, rssi):
        self.rssi_history.append(rssi)
        self.rssi_history = self.rssi_history[-5:]
        self.last_seen = time.monotonic()
        self.last_seen_wall = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def ha_state(self):
        return {
            "state": "home" if self.present else "not_home",
            "mac": self.address,
            "rssi": round(self.rssi),
            "distance": round(estimate_distance(self.rssi), 1),
            "last_seen": self.last_seen_wall,
        }


class Scanner:
    def __init__(self, args, mqtt_cfg, blocked_file):
        self.args = args
        self.tags = {}  # address -> Tag
        self.events = []  # recent event log lines
        self.watch = {}
        for i, mac in enumerate(args.mac or []):
            name = args.name[i] if args.name and i < len(args.name) else mac
            self.watch[mac.upper()] = name
        self.mqtt_cfg = mqtt_cfg
        self.bridge = None
        self.blocked_file = blocked_file
        self.blocked = self.load_blocked()
        self.last_scanner_state = None

    # ------------------------------------------------------------ block list

    def load_blocked(self):
        try:
            with open(self.blocked_file, encoding="utf-8") as f:
                return {m.upper() for m in json.load(f)}
        except FileNotFoundError:
            return set()
        except (OSError, ValueError) as e:
            print(f"Warning: cannot read {self.blocked_file}: {e}", file=sys.stderr)
            return set()

    def save_blocked(self):
        tmp = self.blocked_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(self.blocked), f, indent=2)
        os.replace(tmp, self.blocked_file)

    def block(self, mac):
        if mac in self.blocked:
            return
        self.blocked.add(mac)
        self.save_blocked()
        self.tags.pop(mac, None)
        if self.bridge:
            self.bridge.remove_tag(mac)
        self.log(f"BLOCKED  {mac}")

    def unblock(self, macs):
        macs = [m for m in macs if m in self.blocked]
        if not macs:
            return
        self.blocked.difference_update(macs)
        self.save_blocked()
        for mac in macs:
            self.log(f"UNBLOCKED {mac}")

    # ------------------------------------------------------------ MQTT events

    def on_bridge_event(self, event, data):
        if event == "connected":
            self.log("MQTT connected")
            self.bridge.reset_discovered()
            for tag in self.tags.values():
                tag.last_pub = None  # republish everything
            self.last_scanner_state = None
        elif event == "block":
            self.block(data)
        elif event == "unblock":
            self.unblock([data])
        elif event == "unblock_all":
            self.unblock(list(self.blocked))
        elif event == "known_state":
            # Retained state from before a restart: if HA thinks a tag is home, track it
            # so it is marked not_home after the timeout if it really left meanwhile.
            mac = str(data.get("mac", "")).upper()
            if (data.get("state") == "home" and mac and mac not in self.tags
                    and mac not in self.blocked):
                tag = Tag(mac, "Chipolo", "chipolo")
                tag.update(data.get("rssi", self.args.near))
                tag.last_seen_wall = data.get("last_seen")
                tag.present = True
                self.tags[mac] = tag
        elif event == "log":
            self.log(data)

    # ------------------------------------------------------------ scanning

    def classify(self, device, adv):
        """Return (kind, label) if this advertisement is of interest, else None."""
        addr = device.address.upper()
        name = adv.local_name or device.name or ""

        if self.args.discover:
            return "any", name or "-"
        if addr in self.blocked:
            return None
        if self.watch:
            if addr in self.watch:
                return "watched", self.watch[addr]
            return None

        uuids = {u.lower() for u in adv.service_uuids}
        uuids |= {u.lower() for u in adv.service_data}
        if uuids & CHIPOLO_UUIDS or "chipolo" in name.lower():
            return "chipolo", name or "Chipolo"

        if self.args.findmy:
            data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
            if data and data[0] == FINDMY_TYPE:
                return "findmy", "Find My tag (Spot?)"
        return None

    def on_advertisement(self, device, adv):
        hit = self.classify(device, adv)
        if not hit:
            return
        kind, label = hit
        addr = device.address.upper()
        tag = self.tags.get(addr)
        if tag is None:
            tag = self.tags[addr] = Tag(addr, label, kind)
        elif label not in ("-", "Chipolo"):
            tag.label = label
        tag.update(adv.rssi)

    def log(self, msg):
        line = f"{now_str()}  {msg}"
        self.events.append(line)
        self.events = self.events[-10:]
        if self.args.plain:
            print(line, flush=True)

    def run_hook(self, template, tag):
        if not template:
            return
        cmd = template.format(name=tag.label, mac=tag.address, rssi=round(tag.rssi))
        try:
            subprocess.Popen(shlex.split(cmd))
        except Exception as e:  # noqa: BLE001
            self.log(f"hook failed: {e}")

    def evaluate(self):
        t = time.monotonic()
        for tag in self.tags.values():
            recent = t - tag.last_seen < self.args.timeout
            near = recent and tag.rssi >= self.args.near
            changed = False
            if near and not tag.present:
                tag.present = changed = True
                self.log(f"ARRIVED  {tag.label} ({tag.address}) rssi {tag.rssi:.0f}")
                self.run_hook(self.args.on_arrive, tag)
            elif not recent and tag.present:
                tag.present = False
                changed = True
                self.log(f"LEFT     {tag.label} ({tag.address})")
                self.run_hook(self.args.on_leave, tag)
            if self.bridge and self.should_publish(tag, changed, recent, t):
                if self.bridge.publish_tag(tag.address, tag.ha_state()):
                    tag.last_pub, tag.last_pub_rssi = t, tag.rssi

        if self.bridge:
            state = (sum(g.present for g in self.tags.values()), frozenset(self.blocked))
            if state != self.last_scanner_state:
                self.bridge.publish_scanner_state(state[0], state[1])
                if self.bridge.connected:
                    self.last_scanner_state = state

        # Forget devices that have been gone a long time
        for addr in [a for a, g in self.tags.items()
                     if t - g.last_seen > 10 * self.args.timeout and not g.present]:
            del self.tags[addr]

    def should_publish(self, tag, changed, recent, t):
        if changed or tag.last_pub is None:
            return True
        if not recent:
            return False
        since = t - tag.last_pub
        return since >= self.args.update_interval and (
            abs(tag.rssi - tag.last_pub_rssi) >= self.args.rssi_delta or since >= KEEPALIVE)

    def render(self):
        t = time.monotonic()
        out = ["\033[H\033[2J"]  # clear screen
        mode = "ALL DEVICES (discover)" if self.args.discover else "Chipolo scanner"
        mqtt_state = ""
        if self.bridge:
            mqtt_state = "  MQTT: " + ("connected" if self.bridge.connected else "DISCONNECTED")
        out.append(f"{mode}  -  {now_str()}  -  near >= {self.args.near} dBm, "
                   f"timeout {self.args.timeout}s{mqtt_state}   (Ctrl+C to quit)\n")
        out.append(f"{'STATUS':8} {'ADDRESS':18} {'RSSI':>5} {'~DIST':>7} {'AGO':>5}  NAME")
        out.append("-" * 72)
        rows = sorted(self.tags.values(), key=lambda g: -g.rssi)
        for g in rows:
            ago = t - g.last_seen
            if self.args.discover:
                status = ""
            elif g.present:
                status = "\033[32mNEAR\033[0m    "
            elif ago < self.args.timeout:
                status = "\033[33mfar\033[0m     "
            else:
                status = "\033[90mgone\033[0m    "
            out.append(f"{status:8} {g.address:18} {g.rssi:5.0f} "
                       f"{estimate_distance(g.rssi):6.1f}m {ago:4.0f}s  {g.label}")
        if not rows:
            out.append("  (nothing detected yet...)")
        if self.blocked and not self.args.discover:
            out.append(f"\n{len(self.blocked)} blocked Chipolo(s) hidden")
        if not self.args.discover:
            out.append("\nEvents:")
            out.extend("  " + e for e in self.events) if self.events else out.append("  -")
        print("\n".join(out), flush=True)

    async def run(self):
        try:  # systemd stops us with SIGTERM: shut down cleanly so HA sees "offline"
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,
                                                          asyncio.current_task().cancel)
        except (NotImplementedError, AttributeError):
            pass
        if self.mqtt_cfg and not self.args.discover:
            from ha_mqtt import HomeAssistantBridge
            self.bridge = HomeAssistantBridge(self.mqtt_cfg, self.args.scanner_name,
                                              asyncio.get_running_loop(), self.on_bridge_event)
            self.log(f"connecting to MQTT {self.mqtt_cfg['host']}:{self.mqtt_cfg.get('port', 1883)}")
            self.bridge.start()

        scanner = BleakScanner(detection_callback=self.on_advertisement,
                               scanning_mode="active")
        await scanner.start()
        self.log("scanning started")
        try:
            while True:
                await asyncio.sleep(1)
                if not self.args.discover:
                    self.evaluate()
                if not self.args.plain:
                    self.render()
        finally:
            await scanner.stop()
            if self.bridge:
                self.bridge.stop()


def load_config(path):
    if not path:
        return {}
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_args():
    p = argparse.ArgumentParser(description="Scan for nearby Chipolo Bluetooth tags.")
    p.add_argument("--config",
                   help="YAML config file (default: config.yaml next to this script, if present)")
    p.add_argument("--discover", action="store_true",
                   help="show every BLE device (use this to find your Chipolo's MAC)")
    p.add_argument("--mac", action="append",
                   help="only watch this MAC address (repeatable)")
    p.add_argument("--name", action="append",
                   help="friendly name for the matching --mac (repeatable, same order)")
    p.add_argument("--near", type=int,
                   help=f"RSSI threshold in dBm to count as 'near' (default {DEFAULTS['near']}; "
                        "-70 is roughly a few meters)")
    p.add_argument("--timeout", type=int,
                   help="seconds without a signal before a tag counts as gone "
                        f"(default {DEFAULTS['timeout']})")
    p.add_argument("--findmy", action="store_true",
                   help="also show Apple Find My tags (Chipolo ONE/CARD Spot); "
                        "these cannot be told apart from AirTags")
    p.add_argument("--on-arrive", help="command to run on arrival; {name} {mac} {rssi}")
    p.add_argument("--on-leave", help="command to run on leaving; {name} {mac} {rssi}")
    p.add_argument("--plain", action="store_true",
                   help="log events line by line instead of a live table "
                        "(good for systemd / journal)")
    p.add_argument("--no-mqtt", action="store_true",
                   help="do not connect to Home Assistant even if configured")
    args = p.parse_args()

    if not args.config and os.path.exists(os.path.join(SCRIPT_DIR, "config.yaml")):
        args.config = os.path.join(SCRIPT_DIR, "config.yaml")
    return args


def main():
    args = parse_args()
    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"Error reading config {args.config}: {e}", file=sys.stderr)
        sys.exit(1)

    # Command-line options win over config.yaml, which wins over the defaults
    sc = cfg.get("scanner") or {}
    for key, default in DEFAULTS.items():
        if getattr(args, key, None) is None:
            setattr(args, key, sc.get(key, default))
    args.scanner_name = sc.get("name") or socket.gethostname()

    mqtt_cfg = None if args.no_mqtt else cfg.get("mqtt")
    if mqtt_cfg and not mqtt_cfg.get("host"):
        print("Error: mqtt.host is missing in the config", file=sys.stderr)
        sys.exit(1)
    config_dir = os.path.dirname(os.path.abspath(args.config)) if args.config else SCRIPT_DIR
    blocked_file = os.path.join(config_dir, "blocked.json")

    try:
        asyncio.run(Scanner(args, mqtt_cfg, blocked_file).run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopped.")
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        print("Is Bluetooth on? Try: sudo systemctl start bluetooth && sudo rfkill unblock bluetooth",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
