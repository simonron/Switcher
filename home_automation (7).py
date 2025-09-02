#!/usr/bin/env python3

#!/usr/bin/env python3
import time
import socket
import threading
import asyncio
import json
from datetime import datetime
from typing import Dict, Tuple, Optional
import sys
import os
import logging
import uuid
import platform
import tkinter as tk
from tkinter import ttk
import re

# ------------------------
# Constants / Globals
# ------------------------
RUN_TS = time.strftime("%Y%m%d_%H%M%S")
BASE_DIR = os.path.dirname(__file__) if "__file__" in globals() else os.getcwd()
ALARMS_FILE = os.path.join(BASE_DIR, "alarms.json")
ALARM_AUTOSAVE = True

MQTT_PORT = 1883
TOPIC_PREFIX = "frog"
PLUG_COMMAND_TOPIC = "meross/plugs/status"
MEROSS_SUBSCRIBE_TOPIC = "/appliance/#"

DEFAULT_TIMEOUT = 60
CONNECT_TIMEOUT = 1.0
UPDATE_INTERVAL = 10000
MEROSS_TIMEOUT = 10
MQTT_RECONNECT_DELAY = 5
MEROSS_RETRY_DELAYS = [1, 2, 4]
MESSAGE_DEBOUNCE_INTERVAL = 1.0
BUTTON_DEBOUNCE_INTERVAL = 1.0

SUPPORTED_DEVICE_TYPES = ["mss420f", "mss305", "mss310", "msl120"]

# ------------------------
# CA bundle preflight
# ------------------------
try:
		import certifi
		import ssl
		_caf = certifi.where()
		os.environ.setdefault("SSL_CERT_FILE", _caf)
		os.environ.setdefault("REQUESTS_CA_BUNDLE", _caf)
		_ctx = ssl.create_default_context(cafile=_caf)
		ssl._create_default_https_context = lambda: _ctx
except Exception:
		pass
	
# ------------------------
# Third-party imports
# ------------------------
import paho.mqtt.client as mqtt
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager
import ntplib

# ------------------------
# Logging
# ------------------------
logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(message)s",
		datefmt="%a %b %d %H:%M:%S",
		handlers=[logging.FileHandler("home_automation.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def log_message(message: str, critical: bool = False):
		if critical:
				logger.info(message)
		else:
				logger.debug(message)
			
# ------------------------
# Utilities
# ------------------------
def is_raspberry_pi():
		try:
				if platform.system() == "Linux":
						with open("/proc/cpuinfo", "r") as f:
								if "Raspberry Pi" in f.read():
										log_message("Detected Raspberry Pi platform", critical=True)
										return True
		except Exception as e:
				log_message(f"Error detecting platform: {e}", critical=True)
		log_message("Not running on Raspberry Pi", critical=True)
		return False

def save_alarms_to_file(alarms: list):
		try:
				data = {"alarms": sorted(list(set(alarms)))}
				tmp = ALARMS_FILE + ".tmp"
				with open(tmp, "w", encoding="utf-8") as f:
						json.dump(data, f, indent=2)
				os.replace(tmp, ALARMS_FILE)
				log_message(f"Saved {len(data['alarms'])} alarm(s) to {ALARMS_FILE}", critical=True)
		except Exception as e:
				log_message(f"Failed to save alarms: {e}", critical=True)
			
def load_alarms_from_file() -> list:
		try:
				if os.path.exists(ALARMS_FILE):
						with open(ALARMS_FILE, "r", encoding="utf-8") as f:
								data = json.load(f)
						alarms = data.get("alarms", [])
						if isinstance(alarms, list):
								cleaned = []
								for a in alarms:
										if isinstance(a, str) and ":" in a and len(a.split(":")) >= 2:
												cleaned.append(a.strip())
								log_message(f"Loaded {len(cleaned)} alarm(s) from {ALARMS_FILE}", critical=True)
								return cleaned
				else:
						log_message("No alarms file found; will create one on first save.", critical=True)
		except Exception as e:
				log_message(f"Failed to load alarms: {e}", critical=True)
		return []

# ------------------------
# Data classes
# ------------------------
class Config:
		def __init__(self):
				self.broker_address = "192.168.10.21"
				self.email = os.environ.get("MEROSS_EMAIL", "simon.anthony@tortysoft.com")
				self.password = os.environ.get("MEROSS_PASSWORD", "MerossTorty101!")
				self.meross_api_url = os.environ.get("MEROSS_API_URL", "https://iotx-eu.meross.com")
				self.client_id = str(uuid.uuid4())
			
		def timestamp(self):
				return time.strftime("%a %b %d %H:%M:%S ")
	
class State:
		def __init__(self):
				self.first_run = True
				self.bedroom_motion = False
				self.sittingroom_motion = False
				self.old_bedroom_motion = None
				self.old_sittingroom_motion = None
				self.bedroom_bright = False
				self.sittingroom_bright = False
				self.old_bedroom_bright = True
				self.old_sittingroom_bright = True
				self.hall_bright = False
				self.kitchen_bright = False
				self.old_hall_bright = True
				self.old_kitchen_bright = True
				self.any_pir = False
				self.bedroom_pir = False
				self.sittingroom_pir = False
				self.bedroom_light_state = False
				self.sittingroom_light_state = False
				self.hall_light_state = False
				self.kitchen_light_state = False
				self.bedroom_countdown = False
				self.sittingroom_countdown = False
				self.bedroom_delay = 100
				self.sr_delay = 100
				self.bedroom_off_time = 0
				self.sittingroom_off_time = 0
				self.alarm_list = []
				self.next = 0
				self.v_start = int(time.time())
				self.target = 0
				self.meross_devices: Dict[str, dict] = {}
				self.bedroom_pir_connected = False
				self.sittingroom_pir_connected = False
				self.last_message_times = {}
				self.last_button_times = {}
				self.visible = True
				self.device_locks = {}
				self.is_rpi = is_raspberry_pi()
				self.meross_display_names: Dict[str, str] = {}
				self.motion_sensors = ["Bedroom PIR", "Sittingroom PIR"]
				self.selected_sensor = "Bedroom PIR"
				self.motion_action = ""
				self.switch_states = {}
			
# ------------------------
# MQTT
# ------------------------
class MQTTHandler:
		def __init__(self, config: Config, state: State, callback, logger_callback):
				self.config = config
				self.state = state
				self.callback = callback
				self.logger_callback = logger_callback
			
				client_kwargs = {
						"client_id": f"HomeAutomation-{config.client_id}",
						"protocol": mqtt.MQTTv5
				}
				if hasattr(mqtt, "CallbackAPIVersion"):
						try:
								client_kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
						except Exception:
								pass
				self.client = mqtt.Client(**client_kwargs)
				self.client.on_connect = self.on_connect
				self.client.on_message = self.on_message
				self.client.on_disconnect = self.on_disconnect
				self.client._connect_timeout = CONNECT_TIMEOUT
				socket.setdefaulttimeout(DEFAULT_TIMEOUT)
				self.running = True
			
		def on_connect(self, client, userdata, *args, **kwargs):
				rc = None
				for a in args:
						if isinstance(a, int):
								rc = a
								break
				if rc is None:
						rc = kwargs.get("rc", 0)
				if rc == 0:
						log_message("Connected to MQTT broker", critical=True)
						client.subscribe(f"{TOPIC_PREFIX}/#")
						client.subscribe(PLUG_COMMAND_TOPIC)
						client.subscribe(MEROSS_SUBSCRIBE_TOPIC)
						# Explicitly subscribe to delay topics
						client.subscribe(f"{TOPIC_PREFIX}/Bedroom_delay")
						client.subscribe(f"{TOPIC_PREFIX}/Sr_delay")
						self.state.bedroom_pir_connected = True
						self.state.sittingroom_pir_connected = True
						# Publish current delay values to ensure synchronization on connect
						self.publish(TOPIC_PREFIX, f"Bedroom_delay = {self.state.bedroom_delay}")
						self.publish(TOPIC_PREFIX, f"Sr_delay = {self.state.sr_delay}")
				else:
						log_message(f"Connection failed with code {rc}", critical=True)
						self.state.bedroom_pir_connected = False
						self.state.sittingroom_pir_connected = False
					
		def on_message(self, client, userdata, msg):
				try:
						payload = msg.payload.decode()
						log_message(f"MQTT on_message received on {msg.topic}: {payload}")
						try:
								data = json.loads(payload)
								if isinstance(data, dict) and data.get('client_id') == self.config.client_id:
										return
								message = data.get('message', payload)
						except json.JSONDecodeError:
								message = payload
						normalized = message.replace(" ", "_")
						if msg.topic.startswith("/appliance/") and "Appliance.Control.Bind" in message:
								self.handle_meross_bind(msg.topic, message)
						elif msg.topic in (TOPIC_PREFIX, PLUG_COMMAND_TOPIC, f"{TOPIC_PREFIX}/Bedroom_delay", f"{TOPIC_PREFIX}/Sr_delay"):
								self.callback(normalized)
								self.logger_callback(client, userdata, msg)
						elif normalized.startswith(("Sittingroom_PIR_", "Bedroom_PIR_", "Any_PIR_", "Motion_Action_",
																				"Bedroom_delay_", "Sr_delay_", "Add_Alarm:")):
								self.callback(normalized)
								self.logger_callback(client, userdata, msg)
						else:
								log_message(f"Unhandled MQTT topic: {msg.topic}, message: {message}", critical=True)
				except Exception as e:
						log_message(f"Unexpected error in on_message: {e}", critical=True)
					
		def handle_meross_bind(self, topic: str, message: str):
				try:
						data = json.loads(message)
						header = data.get("header", {})
						message_id = header.get("messageId")
						if message_id:
								response = {
										"header": {
												"method": "SETACK",
												"namespace": "Appliance.Control.Bind",
												"messageId": message_id,
												"timestamp": int(time.time())
										},
										"payload": {}
								}
								self.publish(topic.replace("publish", "subscribe"), json.dumps(response))
								log_message(f"Sent SETACK for {topic}", critical=True)
				except Exception as e:
						log_message(f"Failed to parse Meross bind message: {e}", critical=True)
					
		def on_disconnect(self, client, userdata, *args, **kwargs):
				rc = None
				for a in args:
						if isinstance(a, int):
								rc = a
								break
				if rc is None:
						rc = kwargs.get("rc", 0)
				if rc != 0:
						log_message(f"Unexpected disconnection (rc={rc}). Reconnecting...", critical=True)
						self.state.bedroom_pir_connected = False
						self.state.sittingroom_pir_connected = False
					
		def connect(self):
				try:
						self.client.connect(self.config.broker_address, MQTT_PORT, 60)
						return True
				except Exception as e:
						log_message(f"MQTT connection failed: {e}", critical=True)
						self.state.bedroom_pir_connected = False
						self.state.sittingroom_pir_connected = False
						return False
			
		def publish(self, topic: str, message: str):
				try:
						current_time = time.time()
						message_key = f"{topic}:{message}"
						if message_key in self.state.last_message_times and current_time - self.state.last_message_times[message_key] < MESSAGE_DEBOUNCE_INTERVAL:
								return
						payload = json.dumps({"client_id": self.config.client_id, "message": message})
						result = self.client.publish(topic, payload, retain=True)  # Retain to ensure new instances get the latest value
						if result.rc == mqtt.MQTT_ERR_SUCCESS:
								self.state.last_message_times[message_key] = current_time
								log_message(f"Published to {topic}: {message}", critical=True)
						else:
								log_message(f"Failed to publish to {topic}: {message}, rc={result.rc}", critical=True)
				except Exception as e:
						log_message(f"Publish error: {e}", critical=True)
					
		def run(self):
				if self.connect():
						self.client.loop_forever()
				else:
						while self.running:
								log_message("Attempting to reconnect to MQTT broker...", critical=True)
								if self.connect():
										self.client.loop_forever()
										break
								time.sleep(MQTT_RECONNECT_DELAY)
							
		def stop(self):
				self.running = False
				try:
						self.client.disconnect()
				except Exception as e:
						log_message(f"MQTT disconnect error (ignored): {e}", critical=True)
					
class MQTTLogger:
		def __init__(self, parent: tk.Toplevel, mqtt_handler: MQTTHandler):
				self.parent = parent
				self.mqtt_handler = mqtt_handler
				self.setup_ui()
				self.logfile = open(os.path.join(BASE_DIR, f"mqtt_log_{RUN_TS}.txt"), "a", encoding="utf-8")
				self.mqtt_handler.connect()
			
		def setup_ui(self):
				self.listbox = tk.Listbox(self.parent, width=44, border=10)
				self.listbox.grid(row=6, column=0, columnspan=3, sticky="nsew")
				scrollbar = tk.Scrollbar(self.parent)
				scrollbar.grid(row=6, column=5, sticky="ns")
				self.listbox.config(yscrollcommand=scrollbar.set)
				scrollbar.config(command=self.listbox.yview)
				self.listbox.bind('<<ListboxSelect>>', self.on_select)
			
		def on_message(self, client, userdata, msg):
				timestamp = datetime.now().strftime("%A %H:%M:%S (%Y/%m/%d)")
				try:
						payload = msg.payload.decode()
						try:
								data = json.loads(payload)
								message = data.get('message', payload)
						except json.JSONDecodeError:
								message = payload
						display_message = f"{message.ljust(30)} {timestamp}"
						self.listbox.insert(0, display_message)
						self.logfile.write(f"{message}, {timestamp}\n")
						self.logfile.flush()
						self.apply_zebra_stripes()
				except UnicodeDecodeError as e:
						log_message(f"Decode error: {e}", critical=True)
					
		def apply_zebra_stripes(self):
				for i in range(self.listbox.size()):
						self.listbox.itemconfigure(i, bg="#555" if i % 2 == 0 else "#888", fg="#fff")
					
		def on_select(self, event):
				if selection := self.listbox.curselection():
						item = self.listbox.get(selection[0]).split()[0].strip()
						self.mqtt_handler.publish(TOPIC_PREFIX, item)
						self.listbox.delete(selection[0])
						self.listbox.insert(0, item)
					
# ------------------------
# App
# ------------------------
class HomeAutomation:
		def update_motion_action_menu(self):
				"""Rebuild the motion action dropdown menu after new devices are discovered."""
				if hasattr(self, "motion_action_menu"):
						self.motion_action_menu.destroy()
				self.motion_action_menu = tk.OptionMenu(
						self.root,
						self.state.motion_action,
						*self.get_switch_choices(),
						command=self.update_motion_action
				)
				self.motion_action_menu.grid(row=5, column=1, padx=4, pady=2, sticky="ew")
			
		def update_motion_action(self, action):
				self.state.motion_action = action
				log_message(f"Updated motion action to: {action}", critical=True)
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Motion_Action = {action}")
			
		def __init__(self):
				self.config = Config()
				self.state = State()
				self.root = tk.Tk()
				self.root.title(f"{os.path.basename(__file__)} {time.strftime('%d-%m %H:%M:%S', time.localtime())}")
				geometry = "415x720+0+300" if not self.state.is_rpi else "320x480+0+0"
				self.root.geometry(geometry)
			
				self._configure_layout()
			
				self.clock = tk.Label(self.root, font=("arial", 62 if not self.state.is_rpi else 40, "bold"),
															bg="#005500", fg="#00aaaa", padx=20)
				self.clock.grid(row=0, column=0, columnspan=3, sticky="ew")
			
				self.mqtt_handler = MQTTHandler(self.config, self.state,
																				self.handle_mqtt_message,
																				lambda c, u, m: self.mqtt_logger.on_message(c, u, m))
				self.mqtt_logger = MQTTLogger(tk.Toplevel(self.root), self.mqtt_handler)
			
				self.add_alarm_btn = tk.Button(self.root, text="Add Alarm", width=10, command=self.add_alarm)
				self.add_alarm_btn.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
			
				self.next_alarm_btn = tk.Button(self.root, text="Alarm", width=10, command=self.next_alarm_list)
				self.next_alarm_btn.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
			
				self.alarm_display = tk.Label(self.root, width=1, anchor="center",
																			justify="center", bg="#003400", fg="#e8ffe8", padx=6, pady=4,
																			font=("Arial", 10, "bold"))
				self.alarm_display.grid(row=1, column=2, padx=4, pady=4, sticky="nsew")
			
				self.toggle_btn = tk.Button(self.root, text="Hide Controls ▲", width=10, command=self.toggle_controls)
				self.toggle_btn.grid(row=2, column=2, padx=4, pady=4, sticky="ew")
			
				# Schedule inputs
				self.switch_var = tk.StringVar(value="")
				self.day_var = tk.StringVar(value=time.strftime("%a"))
				self.time_entry = tk.Entry(self.root, width=8)
				self.time_entry.insert(0, time.strftime("%H:%M:%S"))
			
				tk.Label(self.root, text="Switch").grid(row=2, column=0, padx=4, pady=2, sticky="w")
				tk.Label(self.root, text="Day").grid(row=2, column=1, padx=4, pady=2, sticky="w")
			
				self.switch_menu = tk.OptionMenu(self.root, self.switch_var, *self.get_switch_choices())
				self.switch_menu.grid(row=3, column=0, padx=4, pady=2, sticky="ew")
				tk.OptionMenu(self.root, self.day_var, *['Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri']).grid(
						row=3, column=1, padx=4, pady=2, sticky="ew")
				self.time_entry.grid(row=3, column=2, padx=4, pady=2, sticky="ew")
			
				# Motion sensor selection
				self.sensor_var = tk.StringVar(value=self.state.selected_sensor)
				self.motion_action_var = tk.StringVar(value=self.state.motion_action)
				tk.Label(self.root, text="Motion Sensor").grid(row=4, column=0, padx=4, pady=2, sticky="w")
				tk.OptionMenu(self.root, self.sensor_var, *self.state.motion_sensors,
											command=self.update_selected_sensor).grid(row=5, column=0, padx=4, pady=2, sticky="ew")
				tk.Label(self.root, text="Motion Action").grid(row=4, column=1, padx=4, pady=2, sticky="w")
				self.motion_action_menu = tk.OptionMenu(self.root, self.motion_action_var, *self.get_switch_choices(),
																								command=self.update_motion_action)
				self.motion_action_menu.grid(row=5, column=1, padx=4, pady=2, sticky="ew")
			
				# PIR controls
				self.setup_pir_controls()
			
				# Light controls
				self.setup_light_controls()
			
				# Meross status and buttons
				self.meross_status_label = tk.Label(self.root, text="Initializing Meross (Background)...")
				self.meross_status_label.grid(row=13, column=0, columnspan=3, pady=5, sticky="ew")
			
				# Delay controls
				self.setup_delay_controls(15)
			
				# Async loop & Meross init
				self.meross_manager = None
				self.meross_buttons = {}
				self.loop = asyncio.new_event_loop()
				asyncio.set_event_loop(self.loop)
			
				# Alarms: load/publish or default
				self.state.alarm_list = load_alarms_from_file()
				if self.state.alarm_list:
						for alarm in self.state.alarm_list:
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Add_Alarm:{alarm}")
						self.update_alarm_display()
				else:
						self.setup_alarms()
						if ALARM_AUTOSAVE:
								save_alarms_to_file(self.state.alarm_list)
							
				self.root.after(50, self.process_events)
				self.loop.create_task(self.initialize_meross())
				self.start_meross_polling()
			
				mqtt_thread = threading.Thread(target=self.mqtt_handler.run, daemon=True)
				mqtt_thread.start()
			
				self.last_meross_row = 17
				self.update_alarm_menus()
				self.root.update_idletasks()
				self.tick()
			
		# ---------- Layout ----------
		def _configure_layout(self):
				for c in range(3):
						self.root.grid_columnconfigure(c, weight=1, uniform="cols")
				for r in range(0, 40):
						self.root.grid_rowconfigure(r, weight=0)
					
		# ---------- UI builders ----------
		def setup_pir_controls(self):
				self.pir_buttons = {
						"Any": tk.Button(self.root, text="No PIR is active", width=10, command=self.toggle_any_pir),
						"Bedroom": tk.Button(self.root, text="Bedroom PIR is off", width=10, command=self.toggle_bedroom_pir),
						"Sittingroom": tk.Button(self.root, text="Sittingroom PIR off", width=10, command=self.toggle_sittingroom_pir)
				}
				self.pir_buttons["Any"].grid(row=9, column=0, padx=4, pady=4, sticky="ew")
				self.pir_buttons["Bedroom"].grid(row=10, column=1, padx=4, pady=4, sticky="ew")
				self.pir_buttons["Sittingroom"].grid(row=10, column=2, padx=4, pady=4, sticky="ew")
			
				self.bedroom_countdown_label = tk.Label(self.root, text="Waiting", width=10, bg="red", fg="white")
				self.bedroom_countdown_label.grid(row=9, column=1, padx=4, pady=4, sticky="ew")
				self.sittingroom_countdown_label = tk.Label(self.root, text="Waiting", width=10, bg="red", fg="white")
				self.sittingroom_countdown_label.grid(row=9, column=2, padx=4, pady=4, sticky="ew")
			
				self.update_pir_display()
			
		def setup_light_controls(self):
				self.light_buttons = {
						"Bedroom": tk.Button(self.root, text="Bedroom is off", width=10,
																	command=lambda: self.debounce_light_button("Bedroom"), bg="red", fg="black"),
						"Sittingroom": tk.Button(self.root, text="Sittingroom is off", width=10,
																			command=lambda: self.debounce_light_button("Sittingroom"), bg="red", fg="black"),
						"Hall": tk.Button(self.root, text="Hall is off", width=10,
															command=lambda: self.debounce_light_button("Hall"), bg="red", fg="black"),
						"Kitchen": tk.Button(self.root, text="Kitchen is off", width=10,
																command=lambda: self.debounce_light_button("Kitchen"), bg="red", fg="black")
				}
				self.light_buttons["Bedroom"].grid(row=7, column=1, padx=4, pady=4, sticky="ew")
				self.light_buttons["Sittingroom"].grid(row=7, column=2, padx=4, pady=4, sticky="ew")
				self.light_buttons["Hall"].grid(row=8, column=1, padx=4, pady=4, sticky="ew")
				self.light_buttons["Kitchen"].grid(row=8, column=2, padx=4, pady=4, sticky="ew")
			
				self.brightness_labels = {
						"Bedroom": tk.Label(self.root, text="Bedroom Dark", width=10, bg="red", fg="black"),
						"Sittingroom": tk.Label(self.root, text="Sittingroom Dark", width=10, bg="red", fg="black"),
						"Hall": tk.Label(self.root, text="Hall Dark", width=10, bg="red", fg="black"),
						"Kitchen": tk.Label(self.root, text="Kitchen Dark", width=10, bg="red", fg="black")
				}
				self.brightness_labels["Bedroom"].grid(row=12, column=1, padx=4, pady=4, sticky="ew")
				self.brightness_labels["Sittingroom"].grid(row=12, column=2, padx=4, pady=4, sticky="ew")
				self.brightness_labels["Hall"].grid(row=11, column=1, padx=4, pady=4, sticky="ew")
				self.brightness_labels["Kitchen"].grid(row=11, column=2, padx=4, pady=4, sticky="ew")
			
		def setup_delay_controls(self, start_row):
				tk.Label(self.root, text="Bedroom Delay", width=10, anchor="w").grid(row=start_row, column=0, padx=4, pady=2, sticky="w")
				self.bedroom_delay_entry = tk.Entry(self.root, width=6)
				self.bedroom_delay_entry.insert(0, str(self.state.bedroom_delay))
				self.bedroom_delay_entry.grid(row=start_row + 1, column=0, padx=4, pady=2, sticky="w")
				self.bedroom_delay_entry.bind("<Return>", self.on_bedroom_delay_return)
			
				tk.Label(self.root, text="Sittingroom Delay", width=10, anchor="w").grid(row=start_row, column=1, padx=4, pady=2, sticky="w")
				self.sittingroom_delay_entry = tk.Entry(self.root, width=6)
				self.sittingroom_delay_entry.insert(0, str(self.state.sr_delay))
				self.sittingroom_delay_entry.grid(row=start_row + 1, column=1, padx=4, pady=2, sticky="w")
				self.sittingroom_delay_entry.bind("<Return>", self.on_sittingroom_delay_return)
			
		# ---------- Menus ----------
		def get_switch_choices(self):
				choices = set()
				for key, info in self.state.meross_devices.items():
						display_name = self.state.meross_display_names.get(key, key)
						channel = info['channel'] + 1
						choices.add(f"{display_name} Ch{channel} on")
						choices.add(f"{display_name} Ch{channel} off")
				choices.update({
						'Bedroom_PIR on', 'Bedroom_PIR off',
						'Bedroom_Light on', 'Bedroom_Light off',
						'Sittingroom_PIR on', 'Sittingroom_PIR off',
						'Sittingroom_Light on', 'Sittingroom_Light off',
						'Hall_Light on', 'Hall_Light off',
						'Kitchen_Light on', 'Kitchen_Light off',
						'Any_PIR on', 'Any_PIR off'
				})
				return sorted(choices)
	
		def update_alarm_menus(self):
				menu = self.switch_menu["menu"]
				menu.delete(0, "end")
				for choice in self.get_switch_choices():
						menu.add_command(label=choice, command=lambda v=choice: self.switch_var.set(v))
					
				self.motion_action_menu.destroy()
				self.motion_action_menu = tk.OptionMenu(self.root, self.motion_action_var,
																								*self.get_switch_choices(),
																								command=self.update_motion_action)
				self.motion_action_menu.grid(row=5, column=1, padx=4, pady=2, sticky="ew")
			
		# ---------- Interaction handlers ----------
		def update_selected_sensor(self, sensor):
				self.state.selected_sensor = sensor
				log_message(f"Selected motion sensor: {sensor}", critical=True)
			
		def update_motion_action(self, action):
				self.state.motion_action = action or ""
				log_message(f"Updated motion action to: {self.state.motion_action}", critical=True)
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Motion_Action = {self.state.motion_action}")
			
		def toggle_any_pir(self):
				self.state.any_pir = not self.state.any_pir
				if not self.state.any_pir:
						self.state.bedroom_pir = False
						self.state.sittingroom_pir = False
				self.update_pir_display()
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Any_PIR = {self.state.any_pir}")
			
		def toggle_bedroom_pir(self):
				self.state.bedroom_pir = not self.state.bedroom_pir
				self.update_pir_display()
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Bedroom_PIR = {self.state.bedroom_pir}")
			
		def toggle_sittingroom_pir(self):
				self.state.sittingroom_pir = not self.state.sittingroom_pir
				self.update_pir_display()
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Sittingroom_PIR = {self.state.sittingroom_pir}")
			
		def debounce_light_button(self, room):
				current_time = time.time()
				button_key = f"light_button_{room}"
				if button_key in self.state.last_button_times and current_time - self.state.last_button_times[button_key] < BUTTON_DEBOUNCE_INTERVAL:
						return
				self.state.last_button_times[button_key] = current_time
				if room == "Bedroom":
						self.toggle_bedroom_light()
				elif room == "Sittingroom":
						self.toggle_sittingroom_light()
				elif room == "Hall":
						self.toggle_hall_light()
				elif room == "Kitchen":
						self.toggle_kitchen_light()
					
		def toggle_bedroom_light(self):
				self.state.bedroom_light_state = not self.state.bedroom_light_state
				self.update_light_display("Bedroom")
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Bedroom_is_{'on' if self.state.bedroom_light_state else 'off'}")
			
		def toggle_sittingroom_light(self):
				self.state.sittingroom_light_state = not self.state.sittingroom_light_state
				self.update_light_display("Sittingroom")
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Sittingroom_is_{'on' if self.state.sittingroom_light_state else 'off'}")
			
		def toggle_hall_light(self):
				self.state.hall_light_state = not self.state.hall_light_state
				self.update_light_display("Hall")
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Hall_is_{'on' if self.state.hall_light_state else 'off'}")
			
		def toggle_kitchen_light(self):
				self.state.kitchen_light_state = not self.state.kitchen_light_state
				self.update_light_display("Kitchen")
				self.mqtt_handler.publish(TOPIC_PREFIX, f"Kitchen_is_{'on' if self.state.kitchen_light_state else 'off'}")
			
		def on_bedroom_delay_return(self, event):
				current_time = time.time()
				button_key = "bedroom_delay_entry"
				if button_key in self.state.last_button_times and current_time - self.state.last_button_times[button_key] < BUTTON_DEBOUNCE_INTERVAL:
						return
				self.state.last_button_times[button_key] = current_time
				try:
						val = int(self.bedroom_delay_entry.get())
						if val < 0:
								raise ValueError("Delay cannot be negative")
						if val != self.state.bedroom_delay:
								self.state.bedroom_delay = val
								self.bedroom_delay_entry.delete(0, tk.END)
								self.bedroom_delay_entry.insert(0, str(val))
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Bedroom_delay = {val}")
								log_message(f"Updated Bedroom delay to: {val} via UI", critical=True)
				except ValueError as e:
						log_message(f"Invalid Bedroom delay input: {self.bedroom_delay_entry.get()} - {str(e)}", critical=True)
						self.bedroom_delay_entry.delete(0, tk.END)
						self.bedroom_delay_entry.insert(0, str(self.state.bedroom_delay))
					
		def on_sittingroom_delay_return(self, event):
				current_time = time.time()
				button_key = "sittingroom_delay_entry"
				if button_key in self.state.last_button_times and current_time - self.state.last_button_times[button_key] < BUTTON_DEBOUNCE_INTERVAL:
						return
				self.state.last_button_times[button_key] = current_time
				try:
						val = int(self.sittingroom_delay_entry.get())
						if val < 0:
								raise ValueError("Delay cannot be negative")
						if val != self.state.sr_delay:
								self.state.sr_delay = val
								self.sittingroom_delay_entry.delete(0, tk.END)
								self.sittingroom_delay_entry.insert(0, str(val))
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Sr_delay = {val}")
								log_message(f"Updated Sittingroom delay to: {val} via UI", critical=True)
				except ValueError as e:
						log_message(f"Invalid Sittingroom delay input: {self.sittingroom_delay_entry.get()} - {str(e)}", critical=True)
						self.sittingroom_delay_entry.delete(0, tk.END)
						self.sittingroom_delay_entry.insert(0, str(self.state.sr_delay))
					
		# ---------- UI state updates ----------
		def update_pir_display(self):
				any_txt = "PIR Active" if (self.state.bedroom_pir or self.state.sittingroom_pir) else "No PIR is active"
				any_bg = "yellow" if self.state.any_pir else "red"
				any_fg = "green" if self.state.any_pir else "black"
				self.pir_buttons["Any"].config(text=any_txt, bg=any_bg, fg=any_fg)
			
				bed_txt = "Bedroom PIR is on" if self.state.bedroom_pir_connected else "Bedroom PIR Disconnected"
				bed_bg = "yellow" if self.state.bedroom_pir and self.state.bedroom_pir_connected else ("red" if self.state.bedroom_pir_connected else "grey")
				bed_fg = "green" if self.state.bedroom_pir and self.state.bedroom_pir_connected else "black"
				self.pir_buttons["Bedroom"].config(text=bed_txt, bg=bed_bg, fg=bed_fg)
			
				sr_txt = "Sittingroom PIR is on" if self.state.sittingroom_pir_connected else "Sittingroom PIR Disconnected"
				sr_bg = "yellow" if self.state.sittingroom_pir and self.state.sittingroom_pir_connected else ("red" if self.state.sittingroom_pir_connected else "grey")
				sr_fg = "green" if self.state.sittingroom_pir and self.state.sittingroom_pir_connected else "black"
				self.pir_buttons["Sittingroom"].config(text=sr_txt, bg=sr_bg, fg=sr_fg)
			
		def update_light_display(self, room: str):
				state = {
						"Bedroom": self.state.bedroom_light_state,
						"Sittingroom": self.state.sittingroom_light_state,
						"Hall": self.state.hall_light_state,
						"Kitchen": self.state.kitchen_light_state
				}.get(room, False)
				self.light_buttons[room].config(
						text=f"{room} is {'on' if state else 'off'}",
						bg="yellow" if state else "red",
						fg="green" if state else "black"
				)
			
		def update_brightness_display(self):
				if self.state.old_bedroom_bright != self.state.bedroom_bright:
						self.brightness_labels["Bedroom"].config(
								text="Bedroom Bright" if self.state.bedroom_bright else "Bedroom Dark",
								bg="yellow" if self.state.bedroom_bright else "red",
								fg="green" if self.state.bedroom_bright else "black"
						)
						self.state.old_bedroom_bright = self.state.bedroom_bright
				if self.state.old_sittingroom_bright != self.state.sittingroom_bright:
						self.brightness_labels["Sittingroom"].config(
								text="Sittingroom Bright" if self.state.sittingroom_bright else "Sittingroom Dark",
								bg="yellow" if self.state.sittingroom_bright else "red",
								fg="green" if self.state.sittingroom_bright else "black"
						)
						self.state.old_sittingroom_bright = self.state.sittingroom_bright
				if self.state.old_hall_bright != self.state.hall_bright:
						self.brightness_labels["Hall"].config(
								text="Hall Bright" if self.state.hall_bright else "Hall Dark",
								bg="yellow" if self.state.hall_bright else "red",
								fg="green" if self.state.hall_bright else "black"
						)
						self.state.old_hall_bright = self.state.hall_bright
				if self.state.old_kitchen_bright != self.state.kitchen_bright:
						self.brightness_labels["Kitchen"].config(
								text="Kitchen Bright" if self.state.kitchen_bright else "Kitchen Dark",
								bg="yellow" if self.state.kitchen_bright else "red",
								fg="green" if self.state.kitchen_bright else "black"
						)
						self.state.old_kitchen_bright = self.state.kitchen_bright
					
		# ---------- Schedules / Alarms ----------
		def setup_alarms(self):
				if not self.state.first_run:
						return
				for day in ['Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
						self.state.alarm_list.extend([
								f"Fred_ch1_on:{day} 06:59:56",
								f"Fred_ch1_off:{day} 09:05:00"
						])
				self.state.first_run = False
				self.update_alarm_display()
				for alarm in self.state.alarm_list:
						self.mqtt_handler.publish(TOPIC_PREFIX, f"Add_Alarm:{alarm}")
					
		def add_alarm(self):
				switch_action = self.switch_var.get()
				internal_action = None
				for key, display_name in self.state.meross_display_names.items():
						channel = self.state.meross_devices[key]['channel'] + 1
						if switch_action == f"{display_name} Ch{channel} on":
								internal_action = f"{key}_on"
								break
						elif switch_action == f"{display_name} Ch{channel} off":
								internal_action = f"{key}_off"
								break
				if not internal_action:
						internal_action = switch_action.replace(" ", "_")
				alarm = f"{internal_action}:{self.day_var.get()} {self.time_entry.get()}"
				if alarm not in self.state.alarm_list:
						self.state.alarm_list.append(alarm)
						self.update_alarm_display()
						self.mqtt_handler.publish(TOPIC_PREFIX, f"Add_Alarm:{alarm}")
						if ALARM_AUTOSAVE:
								save_alarms_to_file(self.state.alarm_list)
							
		def update_alarm_display(self, fg_color="black"):
				now = time.strftime("%H:%M:%S")
				current_day = time.strftime("%a")
				upcoming = [a.split(':') for a in self.state.alarm_list
										if a.split(':')[1].startswith(current_day) and a.split(':')[2] > now]
				upcoming.sort(key=lambda x: x[2])
				if upcoming and self.state.next >= len(upcoming):
						self.state.next = 0
				if upcoming and 0 <= self.state.next < len(upcoming):
						next_alarm = " ".join(upcoming[self.state.next])
						action = next_alarm.split(':')[0]
						for key, display_name in self.state.meross_display_names.items():
								if action.startswith(key):
										channel = self.state.meross_devices[key]['channel'] + 1
										state = action.split('_')[-1]
										display_action = f"{display_name} Ch{channel} {state}"
										next_alarm = next_alarm.replace(action, display_action)
										break
						display_text = f"{next_alarm[:next_alarm.find(' ')+4]}\n{next_alarm[next_alarm.find(' ')+4:]}"
						self.alarm_display.config(text=display_text, anchor="center", justify="center")
						self.next_alarm_btn.config(text=f"Alarm {self.state.next + 1}", fg=fg_color)
				else:
						self.alarm_display.config(text="", anchor="center", justify="center")
						self.next_alarm_btn.config(text="No more today", fg="black")
					
		def next_alarm_list(self):
				now = time.strftime("%H:%M:%S")
				current_day = time.strftime("%a")
				upcoming = [a.split(':') for a in self.state.alarm_list
										if a.split(':')[1].startswith(current_day) and a.split(':')[2] > now]
				upcoming.sort(key=lambda x: x[2])
				if upcoming:
						self.state.next = (self.state.next + 1) % len(upcoming)
						self.update_alarm_display("red")
				else:
						self.state.next = 0
					
		def check_alarms(self):
				current_time = time.strftime("%a %H:%M:%S")
				for alarm in self.state.alarm_list:
						alarm_parts = alarm.split(':')
						if len(alarm_parts) < 2:
								continue
						action, time_str = alarm_parts[0], ':'.join(alarm_parts[1:])
						if time_str == current_time:
								self.execute_alarm(action)
							
		def execute_alarm(self, action: str):
				non_meross_actions = {
						"Bedroom_PIR_off": lambda: self.toggle_bedroom_pir() if self.state.bedroom_pir else None,
						"Bedroom_PIR_on": lambda: self.toggle_bedroom_pir() if not self.state.bedroom_pir else None,
						"Sittingroom_PIR_off": lambda: self.toggle_sittingroom_pir() if self.state.sittingroom_pir else None,
						"Sittingroom_PIR_on": lambda: self.toggle_sittingroom_pir() if not self.state.sittingroom_pir else None,
						"Bedroom_Light_on": lambda: self.toggle_bedroom_light() if not self.state.bedroom_light_state else None,
						"Bedroom_Light_off": lambda: self.toggle_bedroom_light() if self.state.bedroom_light_state else None,
						"Sittingroom_Light_on": lambda: self.toggle_sittingroom_light() if not self.state.sittingroom_light_state else None,
						"Sittingroom_Light_off": lambda: self.toggle_sittingroom_light() if self.state.sittingroom_light_state else None,
						"Hall_Light_on": lambda: self.toggle_hall_light() if not self.state.hall_light_state else None,
						"Hall_Light_off": lambda: self.toggle_hall_light() if self.state.hall_light_state else None,
						"Kitchen_Light_on": lambda: self.toggle_kitchen_light() if not self.state.kitchen_light_state else None,
						"Kitchen_Light_off": lambda: self.toggle_kitchen_light() if self.state.kitchen_light_state else None,
						"Any_PIR_on": lambda: self.toggle_any_pir() if not self.state.any_pir else None,
						"Any_PIR_off": lambda: self.toggle_any_pir() if self.state.any_pir else None
				}
				action_key = action.replace(" ", "_")
				if action_key in non_meross_actions:
						non_meross_actions[action_key]()
						return
			
				device_key = None
				if action.endswith("_on"):
						desired = True
						base = action[:-3]
				elif action.endswith("_off"):
						desired = False
						base = action[:-4]
				else:
						return
			
				if base in self.state.meross_devices:
						device_key = base
				else:
						for key, display_name in self.state.meross_display_names.items():
								channel = self.state.meross_devices[key]['channel'] + 1
								display_action = f"{display_name.replace(' ', '_')}_ch{channel}_{'on' if desired else 'off'}"
								if action == display_action:
										device_key = key
										break
				if device_key and device_key in self.state.meross_devices:
						current = self.state.meross_devices[device_key]['status']
						if current != desired:
								self.loop.create_task(self.toggle_meross_device(device_key, desired))
				else:
						self.loop.create_task(self.initialize_meross())
					
		# ---------- General action helpers ----------
		@staticmethod
		def _split_action(action: str) -> Tuple[str, Optional[bool]]:
				a = action.strip()
				if a.lower().endswith(" on"):
						return a[:-3], True
				if a.lower().endswith(" off"):
						return a[:-4], False
				return a, None
	
		def _perform_action(self, base: str, desired_on: bool):
				base_norm = base.strip()
			
				if base_norm == "Bedroom_Light":
						if self.state.bedroom_light_state != desired_on:
								self.state.bedroom_light_state = desired_on
								self.update_light_display("Bedroom")
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Bedroom_is_{'on' if desired_on else 'off'}")
						return
				if base_norm == "Sittingroom_Light":
						if self.state.sittingroom_light_state != desired_on:
								self.state.sittingroom_light_state = desired_on
								self.update_light_display("Sittingroom")
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Sittingroom_is_{'on' if desired_on else 'off'}")
						return
				if base_norm == "Hall_Light":
						if self.state.hall_light_state != desired_on:
								self.state.hall_light_state = desired_on
								self.update_light_display("Hall")
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Hall_is_{'on' if desired_on else 'off'}")
						return
				if base_norm == "Kitchen_Light":
						if self.state.kitchen_light_state != desired_on:
								self.state.kitchen_light_state = desired_on
								self.update_light_display("Kitchen")
								self.mqtt_handler.publish(TOPIC_PREFIX, f"Kitchen_is_{'on' if desired_on else 'off'}")
						return
			
				if base_norm in ("Bedroom_PIR", "Sittingroom_PIR", "Any_PIR"):
						if base_norm == "Bedroom_PIR" and self.state.bedroom_pir != desired_on:
								self.toggle_bedroom_pir()
						elif base_norm == "Sittingroom_PIR" and self.state.sittingroom_pir != desired_on:
								self.toggle_sittingroom_pir()
						elif base_norm == "Any_PIR" and self.state.any_pir != desired_on:
								self.toggle_any_pir()
						return
			
				for key, display_name in self.state.meross_display_names.items():
						ch = self.state.meross_devices[key]['channel'] + 1
						if base_norm == f"{display_name} Ch{ch}":
								current = self.state.meross_devices[key]['status']
								if current != desired_on:
										self.loop.create_task(self.toggle_meross_device(key, desired_on))
								return
					
				if base_norm in self.state.meross_devices:
						current = self.state.meross_devices[base_norm]['status']
						if current != desired_on:
								self.loop.create_task(self.toggle_meross_device(base_norm, desired_on))
						return
			
				log_message(f"Unknown action base '{base_norm}' — no-op", critical=True)
			
		def execute_motion_action(self):
				action = self.state.motion_action
				if not action:
						return
				base, desired = self._split_action(action)
				if desired is None:
						return
				self._perform_action(base, desired)
			
		# ---------- Motion handling ----------
		def handle_motion(self):
				now = int(time.time())
			
				def process(sensor_name: str,
										enabled_flag: bool,
										connected_flag: bool,
										motion_attr: str,
										old_motion_attr: str,
										countdown_attr: str,
										off_time_attr: str,
										delay: int,
										label: tk.Label):
						if not (enabled_flag and connected_flag):
								setattr(self.state, old_motion_attr, getattr(self.state, motion_attr))
								return
					
						current = getattr(self.state, motion_attr)
						prev = getattr(self.state, old_motion_attr)
					
						if prev is None:
								setattr(self.state, old_motion_attr, current)
								if current:
										label.config(text="Motion", bg="green", fg="white")
								else:
										label.config(text="Waiting", bg="red", fg="white")
								return
					
						if current and not prev:
								setattr(self.state, countdown_attr, False)
								self.execute_motion_action()
								label.config(text="Motion", bg="green", fg="white")
							
						if (not current) and prev:
								if self.state.motion_action.lower().endswith(" on"):
										if not getattr(self.state, countdown_attr):
												setattr(self.state, countdown_attr, True)
												setattr(self.state, off_time_attr, now + delay)
												label.config(text=str(delay), bg="yellow", fg="black")
								else:
										label.config(text="Waiting", bg="red", fg="white")
									
						setattr(self.state, old_motion_attr, current)
					
				if self.state.selected_sensor == "Bedroom PIR":
						process("Bedroom PIR",
										self.state.bedroom_pir, self.state.bedroom_pir_connected,
										"bedroom_motion", "old_bedroom_motion",
										"bedroom_countdown", "bedroom_off_time",
										self.state.bedroom_delay, self.bedroom_countdown_label)
				elif self.state.selected_sensor == "Sittingroom PIR":
						process("Sittingroom PIR",
										self.state.sittingroom_pir, self.state.sittingroom_pir_connected,
										"sittingroom_motion", "old_sittingroom_motion",
										"sittingroom_countdown", "sittingroom_off_time",
										self.state.sr_delay, self.sittingroom_countdown_label)
					
		def check_countdowns(self):
				now = int(time.time())
			
				if self.state.bedroom_countdown and now >= self.state.bedroom_off_time:
						self.state.bedroom_countdown = False
						if self.state.motion_action.lower().endswith(" on"):
								base, _ = self._split_action(self.state.motion_action)
								self._perform_action(base, False)
						self.bedroom_countdown_label.config(text="Waiting", bg="red", fg="white")
					
				if self.state.sittingroom_countdown and now >= self.state.sittingroom_off_time:
						self.state.sittingroom_countdown = False
						if self.state.motion_action.lower().endswith(" on"):
								base, _ = self._split_action(self.state.motion_action)
								self._perform_action(base, False)
						self.sittingroom_countdown_label.config(text="Waiting", bg="red", fg="white")
					
		def update_countdown_labels(self):
				now = int(time.time())
			
				if self.state.bedroom_countdown:
						remaining = max(0, self.state.bedroom_off_time - now)
						self.bedroom_countdown_label.config(text=str(remaining), bg="yellow", fg="black")
				elif self.state.bedroom_motion and self.state.bedroom_pir_connected and self.state.bedroom_pir:
						self.bedroom_countdown_label.config(text="Motion", bg="green", fg="white")
				else:
						self.bedroom_countdown_label.config(text="Waiting", bg="red", fg="white")
					
				if self.state.sittingroom_countdown:
						remaining = max(0, self.state.sittingroom_off_time - now)
						self.sittingroom_countdown_label.config(text=str(remaining), bg="yellow", fg="black")
				elif self.state.sittingroom_motion and self.state.sittingroom_pir_connected and self.state.sittingroom_pir:
						self.sittingroom_countdown_label.config(text="Motion", bg="green", fg="white")
				else:
						self.sittingroom_countdown_label.config(text="Waiting", bg="red", fg="white")
					
		def check_brightness(self):
				if self.state.bedroom_light_state and self.state.bedroom_bright:
						self.root.after(3000, self.turn_off_bedroom_if_bright)
				if self.state.sittingroom_light_state and self.state.sittingroom_bright:
						self.root.after(3000, self.turn_off_sittingroom_if_bright)
				if self.state.hall_light_state and self.state.hall_bright:
						self.root.after(3000, self.turn_off_hall_if_bright)
				if self.state.kitchen_light_state and self.state.kitchen_bright:
						self.root.after(3000, self.turn_off_kitchen_if_bright)
					
		def turn_off_bedroom_if_bright(self):
				if self.state.bedroom_light_state and self.state.bedroom_bright:
						self.state.bedroom_light_state = False
						self.update_light_display("Bedroom")
						self.mqtt_handler.publish(TOPIC_PREFIX, "Bedroom_is_off")
					
		def turn_off_sittingroom_if_bright(self):
				if self.state.sittingroom_light_state and self.state.sittingroom_bright:
						self.state.sittingroom_light_state = False
						self.update_light_display("Sittingroom")
						self.mqtt_handler.publish(TOPIC_PREFIX, "Sittingroom_is_off")
					
		def turn_off_hall_if_bright(self):
				if self.state.hall_light_state and self.state.hall_bright:
						self.state.hall_light_state = False
						self.update_light_display("Hall")
						self.mqtt_handler.publish(TOPIC_PREFIX, "Hall_is_off")
					
		def turn_off_kitchen_if_bright(self):
				if self.state.kitchen_light_state and self.state.kitchen_bright:
						self.state.kitchen_light_state = False
						self.update_light_display("Kitchen")
						self.mqtt_handler.publish(TOPIC_PREFIX, "Kitchen_is_off")
					
		def toggle_controls(self):
				self.state.visible = not self.state.visible
				self.toggle_btn.config(text="Hide Controls ▲" if self.state.visible else "Show Controls ▼")
			
		# ---------- Async / Meross ----------
		def start_meross_polling(self):
				self.root.after(UPDATE_INTERVAL, self.update_meross_states)
			
		async def check_network(self):
				async def try_connect(host, port, timeout):
						try:
								reader, writer = await asyncio.wait_for(
										asyncio.open_connection(host, port), timeout=timeout
								)
								writer.close()
								await writer.wait_closed()
								return True
						except Exception:
								return False
					
				async def try_ntp():
						try:
								ntp_client = ntplib.NTPClient()
								ntp_client.request('pool.ntp.org', timeout=5)
								return True
						except Exception:
								return False
					
				tasks = [
						try_connect("iotx-eu.meross.com", 443, 5),
						try_connect(self.config.broker_address, MQTT_PORT, 5),
						try_ntp()
				]
				results = await asyncio.gather(*tasks, return_exceptions=True)
				return any(results)
	
		async def validate_credentials(self):
				try:
						async with asyncio.timeout(MEROSS_TIMEOUT):
								client = await MerossHttpClient.async_from_user_password(
										email=self.config.email,
										password=self.config.password,
										api_base_url=self.config.meross_api_url
								)
								await client.async_logout()
								return True
				except Exception as e:
						log_message(f"Credential validation failed: {str(e)}", critical=True)
						return False
			
		async def initialize_meross(self):
				self.meross_status_label.config(text="Validating Meross credentials...")
				if not await self.validate_credentials():
						self.meross_status_label.config(text="Meross Auth Failed: Invalid Credentials")
						return
				self.meross_status_label.config(text="Checking network connectivity...")
				for attempt, delay in enumerate(MEROSS_RETRY_DELAYS, 1):
						try:
								if not await self.check_network():
										self.meross_status_label.config(text=f"Network check failed, retrying ({attempt}/{len(MEROSS_RETRY_DELAYS)})...")
										await asyncio.sleep(delay)
										continue
								self.meross_status_label.config(text="Connecting to Meross cloud...")
								async with asyncio.timeout(MEROSS_TIMEOUT):
										client = await MerossHttpClient.async_from_user_password(
												email=self.config.email,
												password=self.config.password,
												api_base_url=self.config.meross_api_url
										)
										self.meross_manager = MerossManager(http_client=client)
										await self.meross_manager.async_init()
										self.meross_manager._devices = {}
										self.meross_status_label.config(text="Discovering Meross devices...")
										await self.meross_manager.async_device_discovery()
										devices = self.meross_manager.find_devices()
										for device in devices:
												log_message(f"Found device: name={device.name}, type={device.type}, uuid={device.uuid}", critical=True)
										plugs = [d for d in devices if d.type in SUPPORTED_DEVICE_TYPES]
										if not plugs:
												self.meross_status_label.config(text=f"No compatible Meross devices found. Supported types: {', '.join(SUPPORTED_DEVICE_TYPES)}")
												log_message(f"No compatible devices found. Supported types: {', '.join(SUPPORTED_DEVICE_TYPES)}", critical=True)
												return
										self.meross_status_label.config(text=f"Found {len(plugs)} Meross device(s)")
										await self.create_meross_buttons(plugs)
										self.update_alarm_menus()
										break
						except asyncio.TimeoutError:
								self.meross_status_label.config(text=f"Meross timeout, retrying ({attempt}/{len(MEROSS_RETRY_DELAYS)})...")
								if attempt < len(MEROSS_RETRY_DELAYS):
										await asyncio.sleep(delay)
						except Exception as e:
								log_message(f"Meross init failed: {str(e)}", critical=True)
								self.meross_status_label.config(text=f"Meross init failed: {str(e)}")
								if attempt < len(MEROSS_RETRY_DELAYS):
										await asyncio.sleep(delay)
								else:
										self.meross_status_label.config(text="Meross initialization failed")
										break
							
		async def create_meross_buttons(self, devices):
				row = 17
				col = 0
				for button in self.meross_buttons.values():
						button.destroy()
				self.state.meross_devices.clear()
				self.meross_buttons.clear()
				self.state.meross_display_names.clear()
				seen_uuids = set()
				for device in devices:
						try:
								async with asyncio.timeout(MEROSS_TIMEOUT):
										await device.async_update()
										uuid_ = device.uuid
										if uuid_ in seen_uuids:
												continue
										seen_uuids.add(uuid_)
										device_name = f"MerossDevice_{uuid_[-6:]}"
										display_name = device.name.strip() if device.name and device.name != "Smart Plug" else device_name
										channels = len(device.channels) if hasattr(device, 'channels') else 1
										for channel in range(channels):
												status = device.get_status(channel=channel) if hasattr(device, 'get_status') else device.is_on(channel=channel)
												status_text = "ON" if status else "OFF"
												key = f"{device_name}_ch{channel + 1}"
												self.state.meross_devices[key] = {
														'device': device,
														'channel': channel,
														'status': status,
														'lock': False
												}
												self.state.meross_display_names[key] = display_name
												button_text = f"{display_name} Ch{channel + 1} {status_text}"
												btn = tk.Button(
														self.root,
														text=button_text,
														width=10,
														command=lambda k=key: self.debounce_button(k),
														bg="green" if status else "red",
														fg="white" if status else "black"
												)
												btn.grid(row=row, column=col, padx=4, pady=5, sticky="ew")
												self.meross_buttons[key] = btn
												col += 1
												if col > 2:
														col = 0
														row += 1
												self.mqtt_handler.publish(TOPIC_PREFIX, f"{key}_{'on' if status else 'off'}")
												self.mqtt_handler.publish(PLUG_COMMAND_TOPIC, f"{device_name}_Outlet_{channel + 1}:{status_text}")
						except asyncio.TimeoutError:
								continue
						except Exception as e:
								log_message(f"Error creating button for {device}: {e}", critical=True)
								continue
				self.last_meross_row = row
				self.root.update_idletasks()
			
		def debounce_button(self, key):
				current_time = time.time()
				if key in self.state.last_button_times and current_time - self.state.last_button_times[key] < BUTTON_DEBOUNCE_INTERVAL:
						return
				self.state.last_button_times[key] = current_time
				self.loop.create_task(self.toggle_meross_device(key))
			
		async def toggle_meross_device(self, key, new_status=None):
				if key not in self.state.meross_devices:
						return
				device_info = self.state.meross_devices[key]
				if device_info['lock']:
						return
				device_info['lock'] = True
				try:
						async with asyncio.timeout(MEROSS_TIMEOUT):
								device = device_info['device']
								channel = device_info['channel']
								current_status = device_info['status']
								target_status = (not current_status) if new_status is None else new_status
								if target_status == current_status:
										return
							
								if target_status:
										await device.async_turn_on(channel=channel)
								else:
										await device.async_turn_off(channel=channel)
									
								await device.async_update()
								status = device.get_status(channel=channel) if hasattr(device, 'get_status') else device.is_on(channel=channel)
							
								if status != target_status:
										status = target_status
									
								self.state.meross_devices[key]['status'] = status
								self.update_meross_button(key, status)
							
								display_name = self.state.meross_display_names.get(key, f"MerossDevice_{device.uuid[-6:]}")
								status_text = "ON" if status else "OFF"
								self.mqtt_handler.publish(TOPIC_PREFIX, f"{key}_{'on' if status else 'off'}")
								self.mqtt_handler.publish(TOPIC_PREFIX, f"{display_name.replace(' ', '_')}_ch{channel + 1}_{'on' if status else 'off'}")
								self.mqtt_handler.publish(PLUG_COMMAND_TOPIC, f"{display_name}_Outlet_{channel + 1}:{status_text}")
							
				except (asyncio.TimeoutError, Exception) as e:
						log_message(f"Meross command timeout or error for {key}: {e}", critical=True)
						target_status = (not self.state.meross_devices[key]['status']) if new_status is None else new_status
						self.state.meross_devices[key]['status'] = target_status
						self.update_meross_button(key, target_status)
					
				finally:
						device_info['lock'] = False
					
		def update_meross_button(self, key, status):
				if key not in self.meross_buttons:
						return
				device_info = self.state.meross_devices[key]
				device = device_info['device']
				channel = device_info['channel']
				display_name = self.state.meross_display_names.get(key, f"MerossDevice_{device.uuid[-6:]}")
				status_text = "ON" if status else "OFF"
				self.meross_buttons[key].config(
						text=f"{display_name} Ch{channel + 1} {status_text}",
						bg="green" if status else "red",
						fg="white" if status else "black",
						width=10
				)
			
		def update_meross_states(self):
				self.loop.create_task(self.check_network())
				if self.meross_manager:
						asyncio.ensure_future(self.async_update_meross_states())
				self.root.after(UPDATE_INTERVAL, self.update_meross_states)
			
		async def async_update_meross_states(self):
				for attempt, delay in enumerate(MEROSS_RETRY_DELAYS, 1):
						try:
								if not await self.check_network():
										await asyncio.sleep(delay)
										continue
								async with asyncio.timeout(MEROSS_TIMEOUT):
										for key, device_info in list(self.state.meross_devices.items()):
												if device_info['lock']:
														continue
												device = device_info['device']
												channel = device_info['channel']
												device_name = f"MerossDevice_{device.uuid[-6:]}"
												display_name = self.state.meross_display_names.get(key, device_name)
												await device.async_update()
												new_status = device.get_status(channel=channel) if hasattr(device, 'get_status') else device.is_on(channel=channel)
												if new_status != device_info['status']:
														device_info['lock'] = True
														try:
																self.state.meross_devices[key]['status'] = new_status
																self.update_meross_button(key, new_status)
																self.mqtt_handler.publish(TOPIC_PREFIX, f"{key}_{'on' if new_status else 'off'}")
																self.mqtt_handler.publish(TOPIC_PREFIX, f"{display_name.replace(' ', '_')}_ch{channel + 1}_{'on' if new_status else 'off'}")
																self.mqtt_handler.publish(PLUG_COMMAND_TOPIC, f"{device_name}_Outlet_{channel + 1}:{'ON' if new_status else 'OFF'}")
														finally:
																device_info['lock'] = False
										break
						except asyncio.TimeoutError:
								if attempt < len(MEROSS_RETRY_DELAYS):
										await asyncio.sleep(delay)
						except Exception:
								if attempt < len(MEROSS_RETRY_DELAYS):
										await asyncio.sleep(delay)
								else:
										break
							
		def process_events(self):
				self.check_alarms()
				self.handle_motion()
				self.check_countdowns()
				self.update_countdown_labels()
				self.check_brightness()
				self.clock.config(text=time.strftime("%H:%M:%S"))
				self.root.after(1000, self.process_events)
			
		def tick(self):
				self.root.after(1000, self.process_events)
			
		# ---------- MQTT message router ----------
		def handle_mqtt_message(self, message: str):
				current_time = time.time()
				message_key = f"{TOPIC_PREFIX}:{message}"
				if message_key in self.state.last_message_times and current_time - self.state.last_message_times[message_key] < MESSAGE_DEBOUNCE_INTERVAL:
						return
				self.state.last_message_times[message_key] = current_time
			
				m = message
			
				if m == "Bedroom_Motion_is_1":
						self.state.bedroom_motion = True
						self.handle_motion()
						return
				if m == "Bedroom_Motion_is_0":
						self.state.bedroom_motion = False
						self.handle_motion()
						return
				if m == "Sittingroom_Motion_is_1":
						self.state.sittingroom_motion = True
						self.handle_motion()
						return
				if m == "Sittingroom_Motion_is_0":
						self.state.sittingroom_motion = False
						self.handle_motion()
						return
			
				if m == "Bedroom_light_is_True":
						self.state.bedroom_bright = True
						self.update_brightness_display()
						self.check_brightness()
						return
				if m == "Bedroom_light_is_False":
						self.state.bedroom_bright = False
						self.update_brightness_display()
						return
				if m == "Sittingroom_light_is_True":
						self.state.sittingroom_bright = True
						self.update_brightness_display()
						self.check_brightness()
						return
				if m == "Sittingroom_light_is_False":
						self.state.sittingroom_bright = False
						self.update_brightness_display()
						return
				if m == "Hall_light_is_True":
						self.state.hall_bright = True
						self.update_brightness_display()
						self.check_brightness()
						return
				if m == "Hall_light_is_False":
						self.state.hall_bright = False
						self.update_brightness_display()
						return
				if m == "Kitchen_light_is_True":
						self.state.kitchen_bright = True
						self.update_brightness_display()
						self.check_brightness()
						return
				if m == "Kitchen_light_is_False":
						self.state.kitchen_bright = False
						self.update_brightness_display()
						return
			
				if m == "Bedroom_is_True":
						self.state.bedroom_light_state = True
						self.update_light_display("Bedroom")
						return
				if m == "Bedroom_is_False":
						self.state.bedroom_light_state = False
						self.update_light_display("Bedroom")
						return
				if m == "Sittingroom_is_True":
						self.state.sittingroom_light_state = True
						self.update_light_display("Sittingroom")
						return
				if m == "Sittingroom_is_False":
						self.state.sittingroom_light_state = False
						self.update_light_display("Sittingroom")
						return
				if m == "Hall_is_True":
						self.state.hall_light_state = True
						self.update_light_display("Hall")
						return
				if m == "Hall_is_False":
						self.state.hall_light_state = False
						self.update_light_display("Hall")
						return
				if m == "Kitchen_is_True":
						self.state.kitchen_light_state = True
						self.update_light_display("Kitchen")
						return
				if m == "Kitchen_is_False":
						self.state.kitchen_light_state = False
						self.update_light_display("Kitchen")
						return
			
				if m.startswith("Bedroom_delay_=") or m.startswith("Bedroom_delay ="):
						try:
								val = int(m.split("=")[1].strip())
								if val >= 0 and val != self.state.bedroom_delay:
										button_key = "bedroom_delay_entry"
										if button_key in self.state.last_button_times and current_time - self.state.last_button_times[button_key] < BUTTON_DEBOUNCE_INTERVAL:
												return
										self.state.last_button_times[button_key] = current_time
										self.state.bedroom_delay = val
										self.bedroom_delay_entry.delete(0, tk.END)
										self.bedroom_delay_entry.insert(0, str(val))
										log_message(f"Synchronized Bedroom delay to: {val} via MQTT", critical=True)
										self.mqtt_handler.publish(TOPIC_PREFIX, f"Bedroom_delay = {val}")
						except Exception as e:
								log_message(f"Invalid Bedroom delay MQTT message: {m} - {str(e)}", critical=True)
						return
				if m.startswith("Sr_delay_=") or m.startswith("Sr_delay ="):
						try:
								val = int(m.split("=")[1].strip())
								if val >= 0 and val != self.state.sr_delay:
										button_key = "sittingroom_delay_entry"
										if button_key in self.state.last_button_times and current_time - self.state.last_button_times[button_key] < BUTTON_DEBOUNCE_INTERVAL:
												return
										self.state.last_button_times[button_key] = current_time
										self.state.sr_delay = val
										self.sittingroom_delay_entry.delete(0, tk.END)
										self.sittingroom_delay_entry.insert(0, str(val))
										log_message(f"Synchronized Sittingroom delay to: {val} via MQTT", critical=True)
										self.mqtt_handler.publish(TOPIC_PREFIX, f"Sr_delay = {val}")
						except Exception as e:
								log_message(f"Invalid Sittingroom delay MQTT message: {m} - {str(e)}", critical=True)
						return
			
				if m.startswith("Bedroom_PIR_=") or m.startswith("Bedroom_PIR ="):
						val = m.split("=")[1].strip().lower() == "true"
						self.state.bedroom_pir = val
						self.update_pir_display()
						return
				if m.startswith("Sittingroom_PIR_=") or m.startswith("Sittingroom_PIR ="):
						val = m.split("=")[1].strip().lower() == "true"
						self.state.sittingroom_pir = val
						self.update_pir_display()
						return
				if m.startswith("Any_PIR_=") or m.startswith("Any_PIR ="):
						val = m.split("=")[1].strip().lower() == "true"
						self.state.any_pir = val
						self.update_pir_display()
						return
			
				# Handle Meross device state changes
				for key, device_info in self.state.meross_devices.items():
						if m == f"{key}_on":
								if not device_info['lock'] and device_info['status'] != True:
										self.loop.create_task(self.toggle_meross_device(key, True))
								return
						if m == f"{key}_off":
								if not device_info['lock'] and device_info['status'] != False:
										self.loop.create_task(self.toggle_meross_device(key, False))
								return
						display_name = self.state.meross_display_names.get(key, f"MerossDevice_{device_info['device'].uuid[-6:]}")
						channel = device_info['channel'] + 1
						if m == f"{display_name.replace(' ', '_')}_ch{channel}_on":
								if not device_info['lock'] and device_info['status'] != True:
										self.loop.create_task(self.toggle_meross_device(key, True))
								return
						if m == f"{display_name.replace(' ', '_')}_ch{channel}_off":
								if not device_info['lock'] and device_info['status'] != False:
										self.loop.create_task(self.toggle_meross_device(key, False))
								return
					
				# Handle alarm additions
				if m.startswith("Add_Alarm:"):
						alarm = m[len("Add_Alarm:"):].strip()
						if alarm and alarm not in self.state.alarm_list:
								self.state.alarm_list.append(alarm)
								self.update_alarm_display()
								if ALARM_AUTOSAVE:
										save_alarms_to_file(self.state.alarm_list)
								log_message(f"Added alarm via MQTT: {alarm}", critical=True)
						return
			
				# Handle motion action updates
				if m.startswith("Motion_Action_=") or m.startswith("Motion_Action ="):
						action = m.split("=")[1].strip()
						if action != self.state.motion_action:
								self.state.motion_action = action
								self.motion_action_var.set(action)
								log_message(f"Updated motion action via MQTT: {action}", critical=True)
						return
			
				if m.startswith("Add_Alarm:"):
						alarm = m[10:].strip()
						if alarm and alarm not in self.state.alarm_list:
								self.state.alarm_list.append(alarm)
								self.update_alarm_display()
								if ALARM_AUTOSAVE:
										save_alarms_to_file(self.state.alarm_list)
						return
			
				match = re.match(r"^(?P<name>.+?)_is_(?P<val>on|off|1|0|true|false)$", m, flags=re.IGNORECASE)
				if match:
						name = match.group("name")
						val = match.group("val").lower()
						value = val in ("on", "1", "true")
						self.state.switch_states[name] = value
						if name == "Bedroom":
								self.state.bedroom_light_state = value
								self.update_light_display("Bedroom")
						elif name == "Sittingroom":
								self.state.sittingroom_light_state = value
								self.update_light_display("Sittingroom")
						elif name == "Hall":
								self.state.hall_light_state = value
								self.update_light_display("Hall")
						elif name == "Kitchen":
								self.state.kitchen_light_state = value
								self.update_light_display("Kitchen")
						return
			
				if ":" in m and "Outlet" in m:
						try:
								device_name, status = m.split(":", 1)
								if "_Outlet_" in device_name:
										base_name, outlet = device_name.split("_Outlet_")
										outlet_num = int(outlet)
										key = None
										for internal_key, display_name in self.state.meross_display_names.items():
												if base_name == display_name.replace(" ", "_"):
														key = internal_key
														break
										if key is None:
												key = f"{base_name}_ch{outlet_num}"
										if key in self.state.meross_devices:
												new_status = status.strip().lower() == "on"
												if new_status != self.state.meross_devices[key]['status']:
														self.loop.create_task(self.toggle_meross_device(key, new_status))
						except Exception:
								pass
						return
			
		# ---------- App loop ----------
		def tick(self):
				self.clock.config(text=time.strftime("%H:%M:%S"))
				self.check_alarms()
				self.handle_motion()
				self.check_countdowns()
				self.update_countdown_labels()
				self.update_brightness_display()
				self.check_brightness()
				self.root.update_idletasks()
				self.root.after(1000, self.tick)
			
		def process_events(self):
				self.loop.run_until_complete(asyncio.sleep(0.02))
				self.root.after(50, self.process_events)
			
# ------------------------
# Entrypoint
# ------------------------
if __name__ == "__main__":
		log_message(f"Python exec: {sys.executable}", critical=True)
		app = HomeAutomation()
		app.root.mainloop()