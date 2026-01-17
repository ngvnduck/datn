#include <HardwareSerial.h>
#include <WiFi.h>
#include "time.h"

// --- CẤU HÌNH WIFI (SỬA LẠI TÊN VÀ PASS CỦA BẠN) ---
const char* ssid     = "TEN_WIFI_CUA_BAN"; 
const char* password = "MAT_KHAU_WIFI";

// --- CẤU HÌNH THỜI GIAN (NTP) ---
const char* ntpServer = "pool.ntp.org";
const long  gmtOffset_sec = 7 * 3600;       // UTC+7 cho Việt Nam
const int   daylightOffset_sec = 0;

// --- CẤU HÌNH LORA ---
HardwareSerial LoRaSerial(1); // UART1
#define RX_PIN 16
#define TX_PIN 17

// --- BIẾN DỮ LIỆU ---
long seq_id = 1;          
String device_id = "gw_rpi4";
String location = "node01";

// Dữ liệu giả lập
float temp = 28;
float ph = 7.5;
float sal = 15.2;
float turb = 10.5;

void setup() {
  Serial.begin(115200);

  // 1. Khởi tạo UART cho LoRa
  LoRaSerial.begin(9600, SERIAL_8N1, RX_PIN, TX_PIN);
  Serial.println("\n--- ESP32 LoRa Transmitter ---");

  // 2. Kết nối WiFi
  Serial.print("Connecting to WiFi");
  WiFi.begin(ssid, password);
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    // 3. Cấu hình thời gian
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    Serial.println("Time synced with NTP server.");
  } else {
    Serial.println("\nWiFi connection failed. Time will be incorrect.");
  }
}

// === HÀM LẤY GIỜ ĐÃ SỬA ===
String getLocalTimeStr() {
  struct tm timeinfo;
  if(!getLocalTime(&timeinfo)){
    return "00:00:00"; 
  }
  char timeStringBuff[10]; // Giảm bộ nhớ đệm vì chuỗi giờ ngắn
  
  // %H: Giờ (00-23)
  // %M: Phút (00-59)
  // %S: Giây (00-59)
  strftime(timeStringBuff, sizeof(timeStringBuff), "%H:%M:%S", &timeinfo);
  
  return String(timeStringBuff);
}

void loop() {
  // Check kết nối
  if(WiFi.status() != WL_CONNECTED) {
     WiFi.reconnect();
  }

  String timeStr = getLocalTimeStr();

  // Tạo bản tin
  String packet = String(seq_id) + "," + 
                  device_id + "," + 
                  location + "," + 
                  String(temp, 2) + "," + 
                  String(ph, 2) + "," + 
                  String(sal, 2) + "," + 
                  String(turb, 2) + "," +
                  timeStr + "," +
                  "END"; 

  LoRaSerial.println(packet);
  Serial.println("Sending: " + packet);

  seq_id++;
  delay(20000); 
}