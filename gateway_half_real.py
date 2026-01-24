import time
import json
import serial
import os
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

GATEWAY_ID = "gw_rpi4" # ID của Gateway
LOG_FILE = "/home/duc/log.txt"
OFFLINE_CACHE_FILE = "/home/duc/offline_cache.jsonl"

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 9600

# AI Model
MODEL_PATH = '/home/duc/shrimp_model.onnx'
SCALER_PATH = '/home/duc/scaler_params.json'
INPUT_STEPS = 192
FEATURE_KEYS = ["temp", "ph", "sal", "turb"] 

# Pin Configuration (2S Li-ion)
BATTERY_MAX_V = 8.4
BATTERY_MIN_V = 6.0
SHUTDOWN_VOLTAGE = 6.3 

# Global state
history_buffer = deque(maxlen=INPUT_STEPS)
mqtt_connected = False
last_sequence_id = -1
packet_loss_total = 0

# --- CLASS: UPS MONITOR (Sử dụng INA219 thật) ---
class UPSMonitor:
    def __init__(self, addr=0x40):
        try:
            self.ina = INA219(shunt_ohms=0.1, address=addr)
            self.ina.configure()
            self.is_active = True
        except:
            self.is_active = False
            print("Warning: INA219 not found.")

    def read_status(self):
        if not self.is_active: return "ERR"
        try:
            voltage = self.ina.voltage()
            current = self.ina.current()
            pct = int(max(0, min(100, ((voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V)) * 100)))
            
            # Tự động bảo vệ pin
            if current < -5 and voltage < SHUTDOWN_VOLTAGE:
                os.system("sudo halt")
                return "HALT"
            
            return f"+{pct}" if current > 2 else f"-{pct}"
        except:
            return "ERR"

# --- HELPER FUNCTIONS ---
def parse_lora_data(raw_str):
    """ Parse: Seq, Node_ID, Location, Temp, pH, Sal, Turb """
    try:
        parts = [p.strip() for p in raw_str.replace("END", "").split(',')]
        if len(parts) >= 7:
            return {
                "seq": int(parts[0]), 
                "node_id": parts[1], 
                "loc": parts[2],
                "temp": float(parts[3]), 
                "ph": float(parts[4]), 
                "sal": float(parts[5]), 
                "turb": float(parts[6])
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
        # Cache bản tin tinh gọn khi mất mạng
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
    global mqtt_connected, last_sequence_id, packet_loss_total
    
    # 1. Khởi tạo AI & UPS
    try: predictor = ShrimpPredictor(model_path=MODEL_PATH, scaler_path=SCALER_PATH)
    except: predictor = None
    ups = UPSMonitor()
    
    # 2. Setup MQTT & LWT
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
        print(f"Serial Error: {e}")
        return

    print(f"Gateway {GATEWAY_ID} is running...")

    while True:
        # Đọc dữ liệu từ Serial
        line = ser.readline()
        if line:
            try:
                raw_data = line.decode('utf-8', errors='ignore').strip()
                if not raw_data: continue
                
                write_permanent_log(raw_data)
                parsed = parse_lora_data(raw_data)
                
                if parsed:
                    # Tính toán Packet Loss
                    curr_seq = parsed["seq"]
                    if last_sequence_id != -1 and curr_seq > last_sequence_id + 1:
                        packet_loss_total += (curr_seq - last_sequence_id - 1)
                    last_sequence_id = curr_seq

                    # Cập nhật Buffer cho AI
                    current_sensor = {
                        "temp": parsed["temp"], "ph": parsed["ph"], 
                        "sal": parsed["sal"], "turb": parsed["turb"]
                    }
                    history_buffer.append(current_sensor)

                    # Chạy dự đoán (Predict)
                    formatted_preds = []
                    if predictor and len(history_buffer) == INPUT_STEPS:
                        raw_input = [[item[k] for k in FEATURE_KEYS] for item in history_buffer]
                        pred_array = predictor.predict(raw_input)
                        if pred_array is not None:
                            # Lấy 32 bước kế tiếp (hoặc theo model output)
                            for i, row in enumerate(pred_array[:32]): 
                                formatted_preds.append({
                                    "step": i + 1,
                                    "temp": round(float(row[0]), 2),
                                    "ph": round(float(row[1]), 2),
                                    "sal": round(float(row[2]), 2),
                                    "turb": round(float(row[3]), 2)
                                })

                    # Đọc trạng thái Pin thực tế
                    batt_stat = ups.read_status()

                    # --- TẠO BẢN TIN THEO YÊU CẦU ---
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

                    # Gửi MQTT
                    if mqtt_connected:
                        client.publish(TOPIC_DATA, json.dumps(payload))
                        print(f"Published Node {parsed['node_id']} - Seq {curr_seq}")
                    else:
                        save_to_offline_cache(payload)
                        print(f"Cached Node {parsed['node_id']} - Seq {curr_seq}")

            except Exception as e:
                print(f"Error processing data: {e}")

if __name__ == "__main__":
    main()
