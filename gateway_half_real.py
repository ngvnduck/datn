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

# AI Model
MODEL_PATH = '/home/duc/shrimp_model.onnx'
SCALER_PATH = '/home/duc/scaler_params.json'
INPUT_STEPS = 192
FEATURE_KEYS = ["temp", "ph", "sal", "turb"] 

# Battery Config (2S Li-ion)
BATTERY_MAX_V = 8.4
BATTERY_MIN_V = 6.0
SHUTDOWN_VOLTAGE = 6.2

# Global state
history_buffer = deque(maxlen=INPUT_STEPS)
mqtt_connected = False
last_sequence_id = -1

# --- CLASS: UPS MONITOR (Updated fix for INA219) ---
class UPSMonitor:
    def __init__(self, addr=0x40, bus=1):
        try:
            # Thêm tham số busnum để tránh lỗi I2C
            self.ina = INA219(shunt_ohms=0.1, address=addr, busnum=bus)
            self.ina.configure()
            self.is_active = True
            print("INA219 Initialized Successfully.")
        except Exception as e:
            self.is_active = False
            print(f"INA219 Init Failed: {e}")

    def read_status(self):
        if not self.is_active: return "ERR"
        try:
            voltage = self.ina.voltage()
            current = self.ina.current() # mA
            
            # Tính % pin dựa trên dải điện áp 6.0V - 8.4V
            pct = int(max(0, min(100, ((voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V)) * 100)))
            
            # Bảo vệ hệ thống: Tắt máy nếu pin quá yếu khi đang xả (current < 0)
            if current < -10 and voltage < SHUTDOWN_VOLTAGE:
                print("CRITICAL BATTERY: Shutting down...")
                os.system("sudo halt")
                return "HALT"
            
            # Trả về định dạng + (đang sạc) hoặc - (đang xả)
            return f"+{pct}" if current > 5 else f"-{pct}"
        except:
            return "ERR"

# --- HELPER FUNCTIONS ---
def create_random_sample():
    """Tạo mẫu dữ liệu ngẫu nhiên để pre-fill buffer"""
    return {
        "temp": round(random.uniform(25.0, 32.0), 2),
        "ph": round(random.uniform(7.0, 8.5), 2),
        "sal": round(random.uniform(10.0, 25.0), 2),
        "turb": round(random.uniform(5.0, 50.0), 2)
    }

def init_buffer():
    """Làm đầy 192 buffer ban đầu bằng dữ liệu random"""
    print(f"Pre-filling buffer with {INPUT_STEPS} samples...")
    for _ in range(INPUT_STEPS):
        history_buffer.append(create_random_sample())

def parse_lora_data(raw_str):
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
    try:
        clean = {k: v for k, v in payload.items() if k not in ["predict", "battery"]}
        with open(OFFLINE_CACHE_FILE, "a") as f: f.write(json.dumps(clean) + "\n")
    except: pass

def flush_offline_cache(client):
    if not os.path.exists(OFFLINE_CACHE_FILE): return
    try:
        with open(OFFLINE_CACHE_FILE, "r") as f: lines = f.readlines()
        for line in lines:
            client.publish(TOPIC_DATA, line.strip())
            time.sleep(0.1)
        os.remove(OFFLINE_CACHE_FILE)
    except: pass

# --- MQTT CALLBACKS ---
def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        status_payload = json.dumps({"device_id": GATEWAY_ID, "status": "ONLINE"})
        client.publish(TOPIC_STATUS, status_payload, qos=1, retain=True)
        flush_offline_cache(client)
    else: mqtt_connected = False

def on_disconnect(c, u, r):
    global mqtt_connected
    mqtt_connected = False

# --- MAIN ---
def main():
    global mqtt_connected, last_sequence_id
    
    # 1. Khởi tạo Buffer & AI & UPS
    init_buffer()
    
    try: predictor = ShrimpPredictor(model_path=MODEL_PATH, scaler_path=SCALER_PATH)
    except: predictor = None
    
    ups = UPSMonitor(addr=0x40, bus=1)
    
    # 2. Setup MQTT
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
    except: pass
    
    # 3. Setup Serial
    try: ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"Serial Error: {e}"); return

    print(f"Gateway {GATEWAY_ID} is ready. Waiting for LoRa data...")

    while True:
        line = ser.readline()
        if line:
            try:
                raw_data = line.decode('utf-8', errors='ignore').strip()
                if not raw_data: continue
                
                write_permanent_log(raw_data)
                parsed = parse_lora_data(raw_data)
                
                if parsed:
                    # Cập nhật buffer (deque sẽ tự đẩy dữ liệu cũ nhất ra)
                    current_sensor = {
                        "temp": parsed["temp"], "ph": parsed["ph"], 
                        "sal": parsed["sal"], "turb": parsed["turb"]
                    }
                    history_buffer.append(current_sensor)

                    # Chạy dự đoán (Lúc này buffer luôn luôn đủ 192 mẫu)
                    formatted_preds = []
                    if predictor:
                        raw_input = [[item[k] for k in FEATURE_KEYS] for item in history_buffer]
                        pred_array = predictor.predict(raw_input)
                        if pred_array is not None:
                            for i, row in enumerate(pred_array[:32]): 
                                formatted_preds.append({
                                    "step": i + 1,
                                    "temp": round(float(row[0]), 2), "ph": round(float(row[1]), 2),
                                    "sal": round(float(row[2]), 2), "turb": round(float(row[3]), 2)
                                })

                    # Đọc Pin từ INA219
                    batt_stat = ups.read_status()

                    # Tạo Payload bản tin
                    payload = {
                        "device_id": GATEWAY_ID,
                        "node_id": parsed["node_id"],
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "location": parsed["loc"],
                        "seq_id": parsed["seq"],
                        "current_data": current_sensor,
                        "predict": formatted_preds,
                        "battery": batt_stat
                    }

                    # Gửi đi
                    if mqtt_connected:
                        client.publish(TOPIC_DATA, json.dumps(payload))
                        print(f"Sent: Node {parsed['node_id']} | Seq {parsed['seq']} | Batt {batt_stat}")
                    else:
                        save_to_offline_cache(payload)
                        print(f"Cached: Node {parsed['node_id']} | Seq {parsed['seq']}")

            except Exception as e:
                print(f"Process Error: {e}")

if __name__ == "__main__":
    main()
