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

# --- CẤU HÌNH HỆ THỐNG ---
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

# Cấu hình AI
MODEL_PATH = '/home/duc/shrimp_model.onnx'
SCALER_PATH = '/home/duc/scaler_params.json'
INPUT_STEPS = 192
FEATURE_KEYS = ["temp", "ph", "sal", "turb"] 

# Cấu hình Pin (2S Li-ion 21700)
BATTERY_MAX_V = 8.4
BATTERY_MIN_V = 6.0
SHUTDOWN_VOLTAGE = 6.2

# Trạng thái hệ thống
history_buffer = deque(maxlen=INPUT_STEPS)
mqtt_connected = False

# --- LỚP QUẢN LÝ PIN (INA219) ---
class UPSMonitor:
    def __init__(self, addr=0x40, bus=1):
        try:
            # Khởi tạo đúng bus I2C cho Raspberry Pi
            self.ina = INA219(shunt_ohms=0.1, address=addr, busnum=bus)
            self.ina.configure()
            self.is_active = True
            print("INA219: Kết nối thành công.")
        except Exception as e:
            self.is_active = False
            print(f"INA219: Lỗi khởi tạo ({e})")

    def read_status(self):
        if not self.is_active: return "ERR"
        try:
            voltage = self.ina.voltage()
            current = self.ina.current()
            pct = int(max(0, min(100, ((voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V)) * 100)))
            
            # Bảo vệ Pin: Tắt máy nếu pin quá yếu
            if current < -10 and voltage < SHUTDOWN_VOLTAGE:
                os.system("sudo halt")
                return "HALT"
            
            # +: Đang sạc, -: Đang xả
            return f"+{pct}" if current > 5 else f"-{pct}"
        except:
            return "ERR"

# --- HÀM HỖ TRỢ ---
def init_buffer_with_random():
    """Làm đầy buffer 192 mẫu ngẫu nhiên để AI chạy được ngay"""
    print(f"Đang khởi tạo {INPUT_STEPS} mẫu dữ liệu ngẫu nhiên...")
    for _ in range(INPUT_STEPS):
        sample = {
            "temp": round(random.uniform(26.0, 30.0), 2),
            "ph": round(random.uniform(7.5, 8.2), 2),
            "sal": round(random.uniform(15.0, 20.0), 2),
            "turb": round(random.uniform(10.0, 30.0), 2)
        }
        history_buffer.append(sample)

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

def flush_offline_cache(client):
    if not os.path.exists(OFFLINE_CACHE_FILE): return
    try:
        print("Đang đẩy dữ liệu từ cache lên MQTT...")
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
        print("MQTT: Đã kết nối HiveMQ.")
        status_payload = json.dumps({"device_id": GATEWAY_ID, "status": "ONLINE"})
        client.publish(TOPIC_STATUS, status_payload, qos=1, retain=True)
        flush_offline_cache(client)
    else: mqtt_connected = False

def on_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False

# --- CHƯƠNG TRÌNH CHÍNH ---
def main():
    global mqtt_connected
    
    # 1. Khởi tạo
    init_buffer_with_random()
    try: 
        predictor = ShrimpPredictor(model_path=MODEL_PATH, scaler_path=SCALER_PATH)
    except: 
        predictor = None
        print("AI Core: Không tìm thấy model.")
    
    ups = UPSMonitor(addr=0x40, bus=1)
    
    # 2. Cấu hình MQTT
    client = mqtt.Client()
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set()
    client.will_set(TOPIC_STATUS, json.dumps({"device_id": GATEWAY_ID, "status": "OFFLINE"}), qos=1, retain=True)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    
    try:
        client.connect(BROKER, PORT, 60)
        client.loop_start()
    except: pass
    
    # 3. Mở Serial
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"Lỗi Serial: {e}"); return

    print("Hệ thống đã sẵn sàng...")

    while True:
        line = ser.readline()
        if line:
            try:
                raw_data = line.decode('utf-8', errors='ignore').strip()
                if not raw_data: continue
                
                # Ghi log thô
                with open(LOG_FILE, "a") as f:
                    f.write(f"[{datetime.now()}] {raw_data}\n")

                parsed = parse_lora_data(raw_data)
                if parsed:
                    # Cập nhật sensor data thực tế vào buffer
                    current_sensor = {
                        "temp": parsed["temp"], "ph": parsed["ph"], 
                        "sal": parsed["sal"], "turb": parsed["turb"]
                    }
                    history_buffer.append(current_sensor)

                    # --- CHẠY AI DỰ ĐOÁN ---
                    formatted_preds = []
                    if predictor:
                        raw_input = [[item[k] for k in FEATURE_KEYS] for item in history_buffer]
                        pred_array = predictor.predict(raw_input)
                        if pred_array is not None:
                            for i, row in enumerate(pred_array[:32]): 
                                # ĐỊNH DẠNG MẢNG SỐ: [step, temp, ph, sal, turb]
                                formatted_preds.append([
                                    i + 1, 
                                    round(float(row[0]), 2), 
                                    round(float(row[1]), 2),
                                    round(float(row[2]), 2), 
                                    round(float(row[3]), 2)
                                ])

                    batt_stat = ups.read_status()
                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    # --- GỬI DỮ LIỆU ---
                    if mqtt_connected:
                        # Bản tin đầy đủ
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
                        print(f"MQTT -> Node {parsed['node_id']} (Seq: {parsed['seq']})")
                    else:
                        # Chỉ lưu dữ liệu thật khi mất mạng
                        cache_payload = {
                            "device_id": GATEWAY_ID,
                            "node_id": parsed["node_id"],
                            "timestamp": ts_now,
                            "location": parsed["loc"],
                            "seq_id": parsed["seq"],
                            "current_data": current_sensor
                        }
                        with open(OFFLINE_CACHE_FILE, "a") as f:
                            f.write(json.dumps(cache_payload) + "\n")
                        print(f"Cache -> Node {parsed['node_id']}")

            except Exception as e:
                print(f"Lỗi xử lý: {e}")

if __name__ == "__main__":
    main()
