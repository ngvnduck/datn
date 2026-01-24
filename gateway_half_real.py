import time
import json
import serial
import os
import random
import paho.mqtt.client as mqtt
from datetime import datetime
from collections import deque
from ai_core import ShrimpPredictor
from ina219 import INA219

# --- CONFIGURATION ---
BROKER = "5e1faa0dd65748b5b3c5b493359f92ab.s1.eu.hivemq.cloud"
PORT = 8883
USERNAME = "ducnv"
PASSWORD = "Ducnv213698"
TOPIC_DATA = "aquafarm/data"
TOPIC_STATUS = "aquafarm/status"

GATEWAY_ID = "gw_rpi4"
LOG_FILE = "/home/duc/log.txt"
OFFLINE_CACHE_FILE = "/home/duc/offline_cache.jsonl"
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 9600

# AI Model Configuration
MODEL_PATH = '/home/duc/shrimp_model.onnx'
SCALER_PATH = '/home/duc/scaler_params.json'
INPUT_STEPS = 192
FEATURE_KEYS = ["temp", "ph", "sal", "turb"] 

# Battery Config (2S Li-ion 21700)
BATTERY_MAX_V = 8.4
BATTERY_MIN_V = 6.0
SHUTDOWN_VOLTAGE = 6.2

# Global State
history_buffer = deque(maxlen=INPUT_STEPS)
mqtt_connected = False
last_sequence_id = -1

# --- CLASS: UPS MONITOR (INA219 Fixed) ---
class UPSMonitor:
    def __init__(self, addr=0x40, bus=1):
        try:
            # Khởi tạo với busnum=1 để khớp với Raspberry Pi
            self.ina = INA219(shunt_ohms=0.1, address=addr, busnum=bus)
            self.ina.configure()
            self.is_active = True
            print("INA219 System: ONLINE")
        except Exception as e:
            self.is_active = False
            print(f"INA219 System: FAILED ({e})")

    def read_status(self):
        if not self.is_active: return "ERR"
        try:
            voltage = self.ina.voltage()
            current = self.ina.current()
            pct = int(max(0, min(100, ((voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V)) * 100)))
            
            # Tự động tắt máy bảo vệ pin
            if current < -10 and voltage < SHUTDOWN_VOLTAGE:
                os.system("sudo halt")
                return "HALT"
            
            return f"+{pct}" if current > 5 else f"-{pct}"
        except:
            return "ERR"

# --- HELPER FUNCTIONS ---
def create_random_sample():
    """Tạo dữ liệu ngẫu nhiên để pre-fill buffer"""
    return {
        "temp": round(random.uniform(26.0, 30.0), 2),
        "ph": round(random.uniform(7.5, 8.2), 2),
        "sal": round(random.uniform(15.0, 20.0), 2),
        "turb": round(random.uniform(10.0, 30.0), 2)
    }

def init_buffer():
    """Làm đầy 192 mẫu ban đầu"""
    print(f"Pre-filling history buffer with {INPUT_STEPS} samples...")
    for _ in range(INPUT_STEPS):
        history_buffer.append(create_random_sample())

def parse_lora_data(raw_str):
    """Parse: Seq, Node_ID, Location, Temp, pH, Sal, Turb"""
    try:
        parts = [p.strip() for p in raw_str.replace("END", "").split(',')]
        if len(parts) >= 7:
            return {
                "seq": int(parts[0]), "node_id": parts[1], "loc": parts[2],
                "temp": float(parts[3]), "ph": float(parts[4]), 
                "sal": float(parts[5]), "turb": float(parts[6])
            }
        return None
    except: return None

def write_permanent_log(raw_msg):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f: f.write(f"[{ts}] {raw_msg.strip()}\n")
    except: pass

def save_to_offline_cache(payload):
    """Lưu dữ liệu vào file khi mất mạng"""
    try:
        with open(OFFLINE_CACHE_FILE, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except: pass

def flush_offline_cache(client):
    """Gửi dữ liệu tồn kho khi có mạng lại"""
    if not os.path.exists(OFFLINE_CACHE_FILE): return
    try:
        print("Flushing offline cache...")
        with open(OFFLINE_CACHE_FILE, "r") as f: lines = f.readlines()
        for line in lines:
            if line.strip():
                client.publish(TOPIC_DATA, line.strip())
                time.sleep(0.05)
        os.remove(OFFLINE_CACHE_FILE)
    except: pass

# --- MQTT CALLBACKS ---
def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print("Connected to HiveMQ Cloud.")
        status_payload = json.dumps({"device_id": GATEWAY_ID, "status": "ONLINE"})
        client.publish(TOPIC_STATUS, status_payload, qos=1, retain=True)
        flush_offline_cache(client)
    else:
        mqtt_connected = False
        print(f"Connection failed with code {rc}")

def on_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False

# --- MAIN PROCESS ---
def main():
    global mqtt_connected
    
    # 1. Khởi tạo các thành phần
    init_buffer()
    try: 
        predictor = ShrimpPredictor(model_path=MODEL_PATH, scaler_path=SCALER_PATH)
    except: 
        predictor = None
        print("AI Model: Not found, running without prediction.")
    
    ups = UPSMonitor(addr=0x40, bus=1)
    
    # 2. Cấu hình MQTT
    client = mqtt.Client()
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set()
    will_payload = json.dumps({"device_id": GATEWAY_ID, "status": "OFFLINE"})
    client.will_set(TOPIC_STATUS, will_payload, qos=1, retain=True)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    
    try:
        client.connect(BROKER, PORT, 60)
        client.loop_start()
    except:
        print("MQTT Initial Connection: Failed")
    
    # 3. Mở Serial
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"Serial Error: {e}")
        return

    print(f"Gateway {GATEWAY_ID} started successfully.")

    while True:
        line = ser.readline()
        if line:
            try:
                raw_data = line.decode('utf-8', errors='ignore').strip()
                if not raw_data: continue
                
                write_permanent_log(raw_data)
                parsed = parse_lora_data(raw_data)
                
                if parsed:
                    # Cập nhật dữ liệu cảm biến mới vào buffer
                    current_sensor = {
                        "temp": parsed["temp"], "ph": parsed["ph"], 
                        "sal": parsed["sal"], "turb": parsed["turb"]
                    }
                    history_buffer.append(current_sensor)

                    # --- CHẠY AI DỰ ĐOÁN ---
                    # Định dạng gọn: [step, temp, ph, sal, turb]
                    formatted_preds = []
                    if predictor:
                        raw_input = [[item[k] for k in FEATURE_KEYS] for item in history_buffer]
                        pred_array = predictor.predict(raw_input)
                        if pred_array is not None:
                            for i, row in enumerate(pred_array[:32]): 
                                formatted_preds.append([
                                    i + 1, round(float(row[0]), 2), round(float(row[1]), 2),
                                    round(float(row[2]), 2), round(float(row[3]), 2)
                                ])

                    # Đọc trạng thái pin & thời gian
                    batt_stat = ups.read_status()
                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    # --- XỬ LÝ GỬI DỮ LIỆU ---
                    if mqtt_connected:
                        # Bản tin đầy đủ khi ONLINE
                        payload = {
                            "device_id": GATEWAY_ID,
                            "node_id": parsed["node_id"],
                            "timestamp": ts_now,
                            "location": parsed["loc"],
                            "seq_id": parsed["seq"],
                            "current_data": current_sensor,
                            "predict": formatted_preds,
                            "battery": batt_stat
                        }
                        client.publish(TOPIC_DATA, json.dumps(payload))
                        print(f"Sent Online: Node {parsed['node_id']} | Seq {parsed['seq']}")
                    else:
                        # CHỈ LƯU DATA THẬT KHI OFFLINE
                        cache_payload = {
                            "device_id": GATEWAY_ID,
                            "node_id": parsed["node_id"],
                            "timestamp": ts_now,
                            "location": parsed["loc"],
                            "seq_id": parsed["seq"],
                            "current_data": current_sensor
                        }
                        save_to_offline_cache(cache_payload)
                        print(f"Cached Offline (Real Data Only): Node {parsed['node_id']}")

            except Exception as e:
                print(f"Loop Error: {e}")

if __name__ == "__main__":
    main()
