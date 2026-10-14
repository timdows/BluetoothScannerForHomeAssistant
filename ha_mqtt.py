"""
Home Assistant MQTT bridge for the Chipolo scanner.

Publishes MQTT discovery configs so every Chipolo becomes a device in Home Assistant,
sends state updates, and listens for block/unblock commands coming from Home Assistant.

Only technical data (MAC, RSSI, distance, last seen) is sent; names and labels are
managed in Home Assistant itself.

MQTT callbacks run on paho's network thread. They are forwarded to the asyncio loop
with call_soon_threadsafe, so the scanner only ever touches its state from one thread.
"""

import json

import paho.mqtt.client as mqtt


def tag_id(mac):
    return mac.replace(":", "").lower()


def mac_from_id(tid):
    return ":".join(tid[i:i + 2] for i in range(0, 12, 2)).upper()


class HomeAssistantBridge:
    TAG_ENTITIES = ("tracker", "rssi", "distance", "last_seen", "block")

    def __init__(self, cfg, scanner_name, loop, handler):
        """
        handler(event, data) is called on the asyncio loop for:
          "connected"   None          (re)connected to the broker; republish everything
          "block"       mac           Block button pressed, or device deleted in HA
          "unblock"     mac           MAC typed into the "Unblock MAC" text entity
          "unblock_all" None
          "known_state" dict          retained state from a previous run
          "log"         str           message for the console / journal
        """
        self.cfg = cfg
        self.loop = loop
        self.handler = handler
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.base = cfg.get("base_topic", "chipolo_scanner")
        self.avail_topic = f"{self.base}/status"
        self.scanner_name = scanner_name
        self.scanner_uid = f"chipolo_scanner_{scanner_name}".lower().replace("-", "_")
        self.discovered = set()  # tag ids whose discovery config was sent on this connection
        self.known_configs = set()  # tag ids with a tracker config on the broker (paho thread)
        self.connected = False
        self.stopping = False

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=self.scanner_uid)
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password"))
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ----------------------------------------------------------------- lifecycle

    def start(self):
        self.client.connect_async(self.cfg["host"], int(self.cfg.get("port", 1883)), keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.stopping = True
        if self.connected:
            try:
                self.client.publish(self.avail_topic, "offline", qos=1, retain=True) \
                    .wait_for_publish(2)
            except Exception:  # noqa: BLE001
                pass
        self.client.disconnect()
        self.client.loop_stop()

    def _emit(self, event, data=None):
        self.loop.call_soon_threadsafe(self.handler, event, data)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._emit("log", f"MQTT connect failed: {reason_code}")
            return
        self.connected = True
        client.subscribe([
            (f"{self.prefix}/status", 1),                                   # HA birth message
            (f"{self.base}/+/block", 1),                                    # Block buttons
            (f"{self.base}/scanner/unblock", 1),
            (f"{self.base}/scanner/unblock_all", 1),
            (f"{self.base}/+/state", 1),                                    # retained states
            (f"{self.prefix}/device_tracker/{self.base}/+/config", 1),      # device deleted in HA
        ])
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        self._publish_scanner_discovery()
        self._emit("connected")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if not self.stopping:
            self._emit("log", f"MQTT disconnected ({reason_code}), reconnecting...")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace").strip()
        parts = topic.split("/")

        if topic == f"{self.prefix}/status":
            if payload == "online":  # HA restarted: resend discovery and states
                self._publish_scanner_discovery()
                self._emit("connected")
        elif topic == f"{self.base}/scanner/unblock":
            if payload:
                self._emit("unblock", payload.upper())
        elif topic == f"{self.base}/scanner/unblock_all":
            self._emit("unblock_all")
        elif topic.startswith(f"{self.base}/") and parts[-1] == "block":
            self._emit("block", mac_from_id(parts[-2]))
        elif topic.startswith(f"{self.base}/") and parts[-1] == "state":
            if parts[-2] != "scanner" and payload and msg.retain:
                try:
                    self._emit("known_state", json.loads(payload))
                except ValueError:
                    pass
        elif topic.endswith("/config") and parts[-2].endswith("_tracker"):
            # HA clears the retained discovery config when you delete the device.
            tid = parts[-2][:-len("_tracker")]
            if payload:
                self.known_configs.add(tid)
            elif tid in self.known_configs:
                self.known_configs.discard(tid)
                self._emit("block", mac_from_id(tid))

    # ------------------------------------------------------------------ helpers

    def _publish(self, topic, payload, retain=True):
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=1, retain=retain)

    def _config_topic(self, component, object_id):
        return f"{self.prefix}/{component}/{self.base}/{object_id}/config"

    def _scanner_device(self):
        return {
            "identifiers": [self.scanner_uid],
            "name": f"Chipolo scanner {self.scanner_name}",
            "manufacturer": "BluetoothScannerForHomeAssistant",
            "model": "Raspberry Pi BLE scanner",
        }

    def _publish_scanner_discovery(self):
        dev = self._scanner_device()
        st = f"{self.base}/scanner/state"
        common = {"availability_topic": self.avail_topic, "device": dev}
        uid = self.scanner_uid
        self._publish(self._config_topic("sensor", "scanner_nearby"), {
            **common, "name": "Chipolos nearby", "unique_id": f"{uid}_nearby",
            "state_topic": st, "value_template": "{{ value_json.nearby }}",
            "state_class": "measurement", "icon": "mdi:bluetooth",
        })
        self._publish(self._config_topic("sensor", "scanner_blocked"), {
            **common, "name": "Blocked Chipolos", "unique_id": f"{uid}_blocked",
            "state_topic": st, "value_template": "{{ value_json.blocked | count }}",
            "json_attributes_topic": st,
            "json_attributes_template": "{{ {'blocked': value_json.blocked} | tojson }}",
            "entity_category": "diagnostic", "icon": "mdi:cancel",
        })
        # These two have no availability_topic on purpose: they must keep showing
        # their last value while the scanner is offline.
        self._publish(self._config_topic("sensor", "scanner_last_active"), {
            "device": dev, "name": "Last active", "unique_id": f"{uid}_last_active",
            "state_topic": st, "value_template": "{{ value_json.last_active }}",
            "device_class": "timestamp", "icon": "mdi:heart-pulse",
        })
        self._publish(self._config_topic("binary_sensor", "scanner_service"), {
            "device": dev, "name": "Service", "unique_id": f"{uid}_service",
            "state_topic": self.avail_topic, "payload_on": "online", "payload_off": "offline",
            "device_class": "running",
        })
        self._publish(self._config_topic("sensor", "scanner_started"), {
            **common, "name": "Started", "unique_id": f"{uid}_started",
            "state_topic": st, "value_template": "{{ value_json.started }}",
            "device_class": "timestamp", "entity_category": "diagnostic",
        })
        self._publish(self._config_topic("text", "scanner_unblock"), {
            **common, "name": "Unblock MAC", "unique_id": f"{uid}_unblock",
            "command_topic": f"{self.base}/scanner/unblock",
            "entity_category": "config", "icon": "mdi:lock-open-variant",
            "pattern": "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$",
        })
        self._publish(self._config_topic("button", "scanner_unblock_all"), {
            **common, "name": "Unblock all", "unique_id": f"{uid}_unblock_all",
            "command_topic": f"{self.base}/scanner/unblock_all",
            "entity_category": "config", "icon": "mdi:lock-open-variant-outline",
        })

    def _publish_tag_discovery(self, mac):
        tid = tag_id(mac)
        st = f"{self.base}/{tid}/state"
        common = {
            "availability_topic": self.avail_topic,
            "device": {
                "identifiers": [f"chipolo_{tid}"],
                "name": f"Chipolo {mac}",
                "manufacturer": "Chipolo",
                "model": "Chipolo (Bluetooth LE)",
                "via_device": self.scanner_uid,
            },
        }
        self._publish(self._config_topic("device_tracker", f"{tid}_tracker"), {
            **common, "name": None, "unique_id": f"chipolo_{tid}_tracker",
            "state_topic": st, "value_template": "{{ value_json.state }}",
            "payload_home": "home", "payload_not_home": "not_home",
            "source_type": "bluetooth_le", "json_attributes_topic": st,
            "icon": "mdi:tag",
        })
        self._publish(self._config_topic("sensor", f"{tid}_rssi"), {
            **common, "name": "RSSI", "unique_id": f"chipolo_{tid}_rssi",
            "state_topic": st, "value_template": "{{ value_json.rssi }}",
            "device_class": "signal_strength", "unit_of_measurement": "dBm",
            "state_class": "measurement", "entity_category": "diagnostic",
        })
        self._publish(self._config_topic("sensor", f"{tid}_distance"), {
            **common, "name": "Distance", "unique_id": f"chipolo_{tid}_distance",
            "state_topic": st, "value_template": "{{ value_json.distance }}",
            "device_class": "distance", "unit_of_measurement": "m",
            "state_class": "measurement", "suggested_display_precision": 1,
        })
        self._publish(self._config_topic("sensor", f"{tid}_last_seen"), {
            **common, "name": "Last seen", "unique_id": f"chipolo_{tid}_last_seen",
            "state_topic": st, "value_template": "{{ value_json.last_seen }}",
            "device_class": "timestamp", "entity_category": "diagnostic",
        })
        self._publish(self._config_topic("button", f"{tid}_block"), {
            **common, "name": "Block", "unique_id": f"chipolo_{tid}_block",
            "command_topic": f"{self.base}/{tid}/block", "payload_press": "PRESS",
            "entity_category": "config", "icon": "mdi:cancel",
        })
        self.discovered.add(tid)

    # --------------------------------------------------------------- public API

    def publish_tag(self, mac, state):
        """state: dict with state/rssi/distance/last_seen/mac."""
        if not self.connected:
            return False
        tid = tag_id(mac)
        if tid not in self.discovered:
            self._publish_tag_discovery(mac)
        self._publish(f"{self.base}/{tid}/state", state)
        return True

    def remove_tag(self, mac):
        """Remove the device from HA (clear retained discovery configs and state)."""
        tid = tag_id(mac)
        self.discovered.discard(tid)
        components = {"tracker": "device_tracker", "block": "button"}
        for key in self.TAG_ENTITIES:
            self._publish(self._config_topic(components.get(key, "sensor"), f"{tid}_{key}"), "")
        self._publish(f"{self.base}/{tid}/state", "")

    def publish_scanner_state(self, nearby, blocked, started, last_active):
        if not self.connected:
            return False
        self._publish(f"{self.base}/scanner/state", {
            "nearby": nearby, "blocked": sorted(blocked),
            "started": started, "last_active": last_active,
        })
        return True

    def reset_discovered(self):
        self.discovered.clear()
