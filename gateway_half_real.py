import time
import json
import random
import serial
import os
import paho.mqtt.client as mqtt
from datetime import datetime
from collections import deque
from ai_core import ShrimpPredictor

# --- CONFIGURATION ---
BROKER = "5e1faa0dd65748b5b3c5b493359f92ab.s1.eu.hivemq.cloud"
PORT = 8883
USERNAME = "ducnv"
PASSWORD = "Ducnv213698"
TOPIC_DATA = "aquafarm/data"
TOPIC_STATUS = "aquafarm/status"
DEVICE_ID = "gw_rpi4"

LOG_FILE = "/home/duc/log.txt"
OFFLINE_CACHE_FILE = "/home/duc/offline_cache.jsonl"
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 9600

MODEL_PATH = '/home/duc/shrimp_model.onnx'
SCALER_PATH = '/home/duc/scaler_params.json'
INPUT_STEPS = 192
FEATURE_KEYS = ["temp", "ph", "sal", "turb"] 
HEARTBEAT_INTERVAL = 60 # Gửi heartbeat mỗi 60s

history_buffer = deque(maxlen=INPUT_STEPS)
mqtt_connected = False
last_sequence_id = -1
packet_loss_total = 0

# --- HELPER FUNCTIONS ---
def write_permanent_log(raw_msg):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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
            try: client.publish(TOPIC_DATA, line.strip()); time.sleep(0.1)
            except: pass
        os.remove(OFFLINE_CACHE_FILE)
    except: pass

def parse_lora_data(raw_str):
    """ Parse: Seq, ID, Loc, Temp, pH, Sal, Turb """
    try:
        parts = [p.strip() for p in raw_str.replace("END", "").split(',')]
        if len(parts) >= 7:
            return {
                "seq": int(parts[0]), "id": parts[1], "loc": parts[2],
                "temp": float(parts[3]), "ph": float(parts[4]), 
                "sal": float(parts[5]), "turb": float(parts[6])
            }
        return None
    except: return None

def create_random_sample():
    return {
        "temp": round(random.uniform(25.0, 32.0), 2), "ph": round(random.uniform(7.0, 8.5), 2),
        "sal": round(random.uniform(10.0, 25.0), 2), "turb": round(random.uniform(5.0, 50.0), 2)
    }

def init_buffer():
    print("Half-Real Mode: Pre-filling buffer...")
    for _ in range(INPUT_STEPS): history_buffer.append(create_random_sample())

# --- MAIN ---
def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("MQTT Connected.")
        mqtt_connected = True
        # Gửi Online ngay khi connect
        status_payload = json.dumps({"device_id": DEVICE_ID, "status": "ONLINE"})
        client.publish(TOPIC_STATUS, status_payload, qos=1, retain=True)
        flush_offline_cache(client)
    else:
        print(f"MQTT Failed: {rc}")
        mqtt_connected = False
        
def on_disconnect(c, u, r): global mqtt_connected; mqtt_connected = False

def main():
    global mqtt_connected, last_sequence_id, packet_loss_total
    
    # Init AI
    try: predictor = ShrimpPredictor(model_path=MODEL_PATH, scaler_path=SCALER_PATH)
    except: pass
    
    init_buffer()
    
    # MQTT Setup
    client = mqtt.Client()
    client.username_pw_set(USERNAME, PASSWORD); client.tls_set()
    
    # LWT (Last Will)
    will_payload = json.dumps({"device_id": DEVICE_ID, "status": "OFFLINE"})
    client.will_set(TOPIC_STATUS, will_payload, qos=1, retain=True)
    
    client.on_connect = on_connect; client.on_disconnect = on_disconnect
    try: client.connect(BROKER, PORT, 60); client.loop_start()
    except: pass
    
    # Serial Setup
    try: ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except: return

    # Init Heartbeat timer
    last_heartbeat_time = time.time()

    print(f"HALF-REAL MODE (Device: {DEVICE_ID}): Ready.")

    while True:
        # --- HEARTBEAT LOGIC ---
        if mqtt_connected and (time.time() - last_heartbeat_time > HEARTBEAT_INTERVAL):
            try:
                hb_payload = json.dumps({"device_id": DEVICE_ID, "status": "ONLINE"})
                client.publish(TOPIC_STATUS, hb_payload, qos=1, retain=True)
                last_heartbeat_time = time.time()
            except: pass
        # -----------------------

        # Battery giả
        batt_stat = "+98" 
        
        line = ser.readline()
        if line:
            try:
                raw_data = line.decode('utf-8', errors='ignore')
                
                # Log raw
                if raw_data.strip(): write_permanent_log(raw_data)

                decoded = raw_data.strip()
                if decoded:
                    parsed = parse_lora_data(decoded)
                    
                    if parsed:
                        # 1. Packet Loss
                        curr_seq = parsed["seq"]
                        if last_sequence_id != -1 and curr_seq != last_sequence_id + 1:
                            if curr_seq > last_sequence_id:
                                packet_loss_total += (curr_seq - last_sequence_id - 1)
                        last_sequence_id = curr_seq

                        # 2. Update Buffer
                        new_data = {"temp": parsed["temp"], "ph": parsed["ph"], "sal": parsed["sal"], "turb": parsed["turb"]}
                        history_buffer.append(new_data)

                        # 3. Predict
                        formatted_preds = []
                        raw_input = [[item[k] for k in FEATURE_KEYS] for item in history_buffer]
                        pred_array = predictor.predict(raw_input) if 'predictor' in locals() else None
                        
                        if pred_array is not None:
                            for i, row in enumerate(pred_array):
                                formatted_preds.append({"step": i+1, "temp": round(float(row[0]),2), "ph": round(float(row[1]),2), "sal": round(float(row[2]),2), "turb": round(float(row[3]),2)})
                        
                        # 4. Payload (No Timestamp)
                        payload = {
                            "device_id": parsed["id"],
                            "location": parsed["loc"],
                            "seq_id": parsed["seq"],
                            "current_data": new_data, 
                            "predict": formatted_preds,
                            "battery": batt_stat,
                            "packet_loss": packet_loss_total
                        }
                        
                        if mqtt_connected:
                            flush_offline_cache(client)
                            client.publish(TOPIC_DATA, json.dumps(payload))
                            print(f"Published (Seq: {curr_seq})")
                        else:
                            save_to_offline_cache(payload)
                            print(f"Saved Offline (Seq: {curr_seq})")
            except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    main()
