#include <HardwareSerial.h>
#include <WiFi.h>
#include "time.h"
#include <sys/time.h> // Thư viện để lấy mili giây

// --- CẤU HÌNH WIFI ---
const char* ssid     = "Van Duc"; 
const char* password = "duc051103";

// --- CẤU HÌNH THỜI GIAN (NTP) ---
const char* ntpServer = "pool.ntp.org";
const long  gmtOffset_sec = 7 * 3600;
const int   daylightOffset_sec = 0;

// --- CẤU HÌNH LORA ---
HardwareSerial LoRaSerial(1); // UART1
#define RX_PIN 16
#define TX_PIN 17

// --- BIẾN DỮ LIỆU ---
long seq_id = 1;          
String device_id = "node01";
String location = "pond01";

float temp = 28.5;
float ph = 7.5;
float sal = 15.2;
float turb = 10.5;

void setup() {
  Serial.begin(115200);
  LoRaSerial.begin(9600, SERIAL_8N1, RX_PIN, TX_PIN);

  Serial.println("\n--- ESP32 LoRa Transmitter (High Precision Time) ---");
  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);
  
  WiFi.begin(ssid, password);
  
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 60) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    Serial.println("Waiting for NTP sync...");

    struct tm timeinfo;
    // Bắt buộc chờ có giờ mới chạy tiếp
    while(!getLocalTime(&timeinfo)){
        Serial.print(".");
        delay(200);
    }
    Serial.println("\nTime Synced!");
  } else {
    Serial.println("\nWiFi Failed! System time will be wrong.");
  }
}

// === HÀM LẤY GIỜ MỚI (CÓ MILI GIÂY) ===
String getDetailedTimeStr() {
  struct timeval tv;
  gettimeofday(&tv, NULL); 

  struct tm timeinfo;
  getLocalTime(&timeinfo);

  char timeBuff[20];
  strftime(timeBuff, sizeof(timeBuff), "%H:%M:%S", &timeinfo);

  char finalBuff[30];
  // Ghép chuỗi: "HH:MM:SS" + "." + "mili_giây"
  snprintf(finalBuff, sizeof(finalBuff), "%s.%03ld", timeBuff, tv.tv_usec / 1000);

  return String(finalBuff);
}

void loop() {
  // Check WiFi
  if(WiFi.status() != WL_CONNECTED) {
     WiFi.reconnect();
  }

  // Lấy giờ chi tiết
  String timeStr = getDetailedTimeStr();

  // Tạo bản tin
  // Ví dụ: 1,gw_rpi4,pond_01,28.50,7.50,15.20,10.50,13:11:22.185,END
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
  delay(10000); // Gửi mỗi 10 giây
}
