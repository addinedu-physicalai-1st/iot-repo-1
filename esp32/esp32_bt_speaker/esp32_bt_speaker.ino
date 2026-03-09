/*
 * ESP32-WROVER Bluetooth Speaker Streaming
 * =========================================
 * WiFi + Bluetooth A2DP를 이용한 웹 기반 음악 스트리밍
 *
 * 하드웨어: ESP32-WROVER (CH-340)
 * 스피커:   PLEIGO BS15 (BT 5.3)
 *
 * 필요 라이브러리 (아두이노 IDE):
 *   - ESP32-A2DP (https://github.com/pschatzmann/ESP32-A2DP)
 *   - arduino-libhelix (https://github.com/pschatzmann/arduino-libhelix)
 *
 * 보드 설정 (아두이노 IDE):
 *   - 보드: ESP32 Wrover Kit
 *   - Partition Scheme: Huge APP
 *   - Upload Speed: 921600
 */

#include <WiFi.h>
#include <esp_wifi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClient.h>
#include "config.h"
#include "BluetoothA2DPSource.h"
#include "MP3DecoderHelix.h"

// ===== Volume =====
static volatile int volumePercent = 80;  // 0~100

// ===== PCM Ring Buffer (PSRAM) =====
#define PCM_BUF_SIZE      (1024 * 128)   // 256KB 링 버퍼
#define PREBUFFER_SAMPLES (1024 * 64)    // ~740ms 프리버퍼 (끊김 방지)

static int16_t* pcmBuffer = nullptr;
static volatile size_t pcmWritePos  = 0;
static volatile size_t pcmReadPos   = 0;
static volatile size_t pcmAvailable = 0;
static SemaphoreHandle_t pcmMutex   = nullptr;
static volatile bool prebuffering   = true;

size_t pcmBufferWrite(const int16_t* data, size_t samples) {
    xSemaphoreTake(pcmMutex, portMAX_DELAY);
    size_t written = 0;
    while (written < samples && pcmAvailable < PCM_BUF_SIZE) {
        pcmBuffer[pcmWritePos] = data[written];
        pcmWritePos = (pcmWritePos + 1) % PCM_BUF_SIZE;
        pcmAvailable++;
        written++;
    }
    if (prebuffering && pcmAvailable >= PREBUFFER_SAMPLES) {
        prebuffering = false;
    }
    xSemaphoreGive(pcmMutex);
    return written;
}

size_t pcmBufferRead(int16_t* data, size_t samples) {
    xSemaphoreTake(pcmMutex, portMAX_DELAY);
    size_t readCount = 0;
    while (readCount < samples && pcmAvailable > 0) {
        data[readCount] = pcmBuffer[pcmReadPos];
        pcmReadPos = (pcmReadPos + 1) % PCM_BUF_SIZE;
        pcmAvailable--;
        readCount++;
    }
    xSemaphoreGive(pcmMutex);
    return readCount;
}

// ===== Bluetooth A2DP =====
BluetoothA2DPSource a2dpSource;
static int16_t lastSampleL = 0;
static int16_t lastSampleR = 0;

int32_t a2dp_callback(Frame* frame, int32_t frameCount) {
    if (prebuffering) {
        for (int i = 0; i < frameCount; i++) {
            frame[i].channel1 = 0;
            frame[i].channel2 = 0;
        }
        return frameCount;
    }

    int16_t buf[frameCount * 2];
    size_t got = pcmBufferRead(buf, frameCount * 2);
    int vol = (volumePercent * 256) / 100;

    for (int i = 0; i < frameCount; i++) {
        if (i * 2 < (int)got) {
            int16_t L = buf[i * 2];
            int16_t R = (i * 2 + 1 < (int)got) ? buf[i * 2 + 1] : L;
            L = (int16_t)(((int32_t)L * vol) >> 8);
            R = (int16_t)(((int32_t)R * vol) >> 8);
            frame[i].channel1 = L;
            frame[i].channel2 = R;
            lastSampleL = L;
            lastSampleR = R;
        } else {
            // 버퍼 부족 시 페이드아웃 (갑작스러운 끊김 방지)
            lastSampleL = (lastSampleL * 15) / 16;
            lastSampleR = (lastSampleR * 15) / 16;
            frame[i].channel1 = lastSampleL;
            frame[i].channel2 = lastSampleR;
        }
    }
    return frameCount;
}

// ===== MP3 Decoder =====
void mp3_data_callback(MP3FrameInfo& info, short* pcm_data, size_t len, void* ref) {
    int retries = 0;
    size_t written = 0;
    while (written < len && retries < 200) {
        written += pcmBufferWrite(pcm_data + written, len - written);
        if (written < len) {
            vTaskDelay(1);
            retries++;
        }
    }
}

static libhelix::MP3DecoderHelix mp3(mp3_data_callback);

// ===== Playlist =====
#define MAX_PLAYLIST 20
static String playlist[MAX_PLAYLIST];
static String plTitles[MAX_PLAYLIST];
static int plCount = 0;
static volatile int currentTrack = -1;

// ===== HTTP Streaming Task =====
static volatile bool streaming       = false;
static volatile bool streamRequested = false;
static volatile bool stopRequested   = false;
static String streamUrl    = "";
static String currentUrl   = "";
static String currentTitle = "";

void httpStreamTask(void* param) {
    Serial.println("[HTTP Task] Started on core " + String(xPortGetCoreID()));

    while (true) {
        if (streamRequested) {
            streamRequested = false;
            stopRequested   = false;
            currentUrl      = streamUrl;

            Serial.println("[HTTP] Connecting to relay server...");

            // 버퍼 초기화
            xSemaphoreTake(pcmMutex, portMAX_DELAY);
            pcmWritePos  = 0;
            pcmReadPos   = 0;
            pcmAvailable = 0;
            prebuffering = true;
            xSemaphoreGive(pcmMutex);

            WiFiClient wifiClient;
            wifiClient.setTimeout(60);
            HTTPClient http;
            http.setConnectTimeout(30000);
            http.setTimeout(60000);
            http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

            if (!http.begin(wifiClient, currentUrl)) {
                Serial.println("[HTTP] Failed to begin");
                streaming  = false;
                currentUrl = "";
                continue;
            }

            http.addHeader("User-Agent", "ESP32/1.0");
            http.addHeader("Connection", "keep-alive");

            int httpCode = http.GET();
            Serial.println("[HTTP] Response: " + String(httpCode));

            if (httpCode != HTTP_CODE_OK) {
                Serial.println("[HTTP] Error code: " + String(httpCode));
                http.end();
                streaming  = false;
                currentUrl = "";
                continue;
            }

            mp3.begin();

            WiFiClient* stream = http.getStreamPtr();
            stream->setTimeout(5);
            streaming = true;
            Serial.println("[HTTP] Streaming started");

            uint8_t mp3Buf[1024];
            unsigned long totalBytes = 0;
            int emptyCount = 0;

            while (!stopRequested) {
                if (!stream->connected() && !stream->available()) {
                    Serial.println("[HTTP] Stream disconnected");
                    break;
                }

                int avail = stream->available();
                if (avail > 0) {
                    int toRead = (avail > (int)sizeof(mp3Buf)) ? sizeof(mp3Buf) : avail;
                    int bytesRead = stream->readBytes(mp3Buf, toRead);
                    if (bytesRead > 0) {
                        mp3.write(mp3Buf, bytesRead);
                        totalBytes += bytesRead;
                        emptyCount = 0;
                    }
                } else {
                    emptyCount++;
                    if (emptyCount > 1000) {
                        Serial.println("[HTTP] No data timeout");
                        break;
                    }
                    vTaskDelay(pdMS_TO_TICKS(10));
                    continue;
                }
                vTaskDelay(1);
            }

            Serial.println("[HTTP] Stopped. Total: " + String(totalBytes) + " bytes");
            mp3.end();
            http.end();
            streaming  = false;
            currentUrl = "";
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

// ===== Web Server =====
WebServer server(80);

// ===== Web UI (임베디드 HTML) =====
const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IoT Music Player</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;max-width:480px;margin:0 auto}
h1{text-align:center;color:#ff4444;margin-bottom:20px;font-size:22px}
.card{background:#16213e;border-radius:12px;padding:16px;margin-bottom:16px}
.status{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:6px}
.g{background:#00ff88}.r{background:#ff4444}
input[type="text"]{width:100%;padding:12px;border:1px solid #333;border-radius:8px;background:#0f3460;color:#fff;font-size:14px;margin:8px 0}
.btn-row{display:flex;gap:8px;margin-top:8px}
button{padding:12px;border:none;border-radius:8px;font-size:15px;cursor:pointer;font-weight:600}
.ba{background:#ff4444;color:#fff;flex:1}
.bs{background:#555;color:#fff;flex:1}
label{color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px}
.ss{font-size:13px}
.vol-row{display:flex;align-items:center;gap:10px;margin-top:6px}
.vol-row span{min-width:32px;text-align:center;font-size:14px;color:#00d2ff;font-weight:600}
input[type="range"]{-webkit-appearance:none;appearance:none;flex:1;height:6px;border-radius:3px;background:#0f3460;outline:none}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:22px;height:22px;border-radius:50%;background:#00d2ff;cursor:pointer;border:2px solid #16213e}
.vol-btn{width:36px;height:36px;border-radius:50%;background:#0f3460;border:1px solid #1a5276;color:#00d2ff;font-size:18px;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;flex:none}
.vol-btn:active{background:#1a5276}
.nav-row{display:flex;gap:8px;margin-top:12px}
.nav-btn{flex:1;padding:14px;background:#0f3460;border:1px solid #1a5276;color:#00d2ff;border-radius:8px;font-size:20px;cursor:pointer;font-weight:700}
.nav-btn:active{background:#1a5276}
.now-playing{text-align:center;color:#00ff88;font-size:14px;margin-top:10px;min-height:20px}
.pl-item{display:flex;align-items:center;padding:8px;margin:4px 0;background:#0f3460;border-radius:8px;gap:8px}
.pl-item.active{border:1px solid #ff4444;background:#1a1a3e}
.pl-num{color:#555;font-size:12px;min-width:20px;text-align:center}
.pl-title{flex:1;font-size:13px;color:#ccc;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-title:hover{color:#fff}
.pl-del{background:none;border:none;color:#ff4444;font-size:16px;cursor:pointer;padding:4px 8px;flex:none;font-weight:400}
.pl-del:hover{color:#ff6666}
.msg{text-align:center;color:#ffaa00;font-size:12px;margin-top:8px;min-height:16px}
.pl-empty{text-align:center;color:#555;font-size:13px;padding:16px}
</style>
</head>
<body>
<h1>IoT Music Player</h1>
<div class="card">
<label>System Status</label>
<div class="status"><span>WiFi</span><span class="ss"><span class="dot g"></span><span id="wip">-</span></span></div>
<div class="status"><span>Bluetooth</span><span class="ss"><span class="dot" id="bd"></span><span id="bs">-</span></span></div>
<div class="status"><span>Stream</span><span class="ss" id="ss">-</span></div>
</div>
<div class="card">
<label>Now Playing</label>
<div class="now-playing" id="np">-</div>
<div class="nav-row">
<button class="nav-btn" onclick="nav('prev')">&#9664; Prev</button>
<button class="nav-btn" onclick="st()" style="background:#ff4444;color:#fff;flex:0.6;font-size:15px">Stop</button>
<button class="nav-btn" onclick="nav('next')">Next &#9654;</button>
</div>
</div>
<div class="card">
<label>Volume</label>
<div class="vol-row">
<button class="vol-btn" onclick="va(-10)">-</button>
<input type="range" id="vol" min="0" max="100" value="80" oninput="vs(this.value)">
<button class="vol-btn" onclick="va(10)">+</button>
<span id="vt">80%</span>
</div>
</div>
<div class="card">
<label>Add to Playlist</label>
<input type="text" id="yt" placeholder="YouTube URL">
<input type="text" id="tt" placeholder="Title (optional)">
<div class="btn-row">
<button class="ba" onclick="addPl()">Add</button>
</div>
<div class="msg" id="msg"></div>
</div>
<div class="card">
<label>Playlist (<span id="pc">0</span>/20)</label>
<div id="pl"></div>
<div class="btn-row" style="margin-top:12px">
<button class="bs" onclick="clearPl()">Clear All</button>
</div>
</div>
<script>
function addPl(){
 var u=document.getElementById('yt').value;
 if(!u)return;
 var t=document.getElementById('tt').value||u;
 document.getElementById('msg').textContent='Adding...';
 fetch('/playlist/add',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:'url='+encodeURIComponent(u)+'&title='+encodeURIComponent(t)})
 .then(function(r){return r.json()}).then(function(d){
  document.getElementById('msg').textContent=d.error||'Added!';
  document.getElementById('yt').value='';
  document.getElementById('tt').value='';
  loadPl();fs();
 }).catch(function(){document.getElementById('msg').textContent='Error';});
}
function clearPl(){
 fetch('/playlist/clear',{method:'POST'}).then(function(){loadPl();fs();});
}
function delPl(i){
 fetch('/playlist/del',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'idx='+i})
 .then(function(){loadPl();fs();});
}
function playPl(i){
 fetch('/play',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'idx='+i})
 .then(function(r){return r.json()}).then(function(){loadPl();fs();});
}
function nav(d){fetch('/'+d,{method:'POST'}).then(function(r){return r.json()}).then(function(){loadPl();fs();})}
function st(){fetch('/stop',{method:'POST'}).then(function(r){return r.json()}).then(function(){loadPl();fs();})}
function vs(v){
 document.getElementById('vt').textContent=v+'%';
 fetch('/volume',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'v='+v});
}
function va(d){
 var s=document.getElementById('vol');
 var n=Math.max(0,Math.min(100,parseInt(s.value)+d));
 s.value=n;vs(n);
}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function loadPl(){
 fetch('/playlist').then(function(r){return r.json()}).then(function(d){
  var h='';
  document.getElementById('pc').textContent=d.count;
  if(d.count===0){h='<div class="pl-empty">No tracks added</div>';}
  else{
   for(var i=0;i<d.count;i++){
    var ac=i===d.current?' active':'';
    h+='<div class="pl-item'+ac+'">';
    h+='<span class="pl-num">'+(i+1)+'</span>';
    h+='<span class="pl-title" onclick="playPl('+i+')">'+esc(d.titles[i])+'</span>';
    h+='<button class="pl-del" onclick="delPl('+i+')">x</button>';
    h+='</div>';
   }
  }
  document.getElementById('pl').innerHTML=h;
 });
}
function fs(){
 fetch('/status').then(function(r){return r.json()}).then(function(d){
  document.getElementById('wip').textContent=d.wifi;
  document.getElementById('bs').textContent=d.bt;
  document.getElementById('bd').className='dot '+(d.bt==='connected'?'g':'r');
  document.getElementById('ss').textContent=d.streaming?'Playing':'Stopped';
  document.getElementById('np').textContent=d.title||'-';
  document.getElementById('vol').value=d.volume;
  document.getElementById('vt').textContent=d.volume+'%';
 }).catch(function(){});
}
setInterval(fs,3000);fs();loadPl();
</script>
</body>
</html>
)rawliteral";

// ===== URL Encoding =====
String urlEncode(const String& str) {
    String encoded = "";
    for (unsigned int i = 0; i < str.length(); i++) {
        char c = str.charAt(i);
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            encoded += c;
        } else {
            char buf[4];
            snprintf(buf, sizeof(buf), "%%%02X", (unsigned char)c);
            encoded += buf;
        }
    }
    return encoded;
}

String makeRelayUrl(const String& ytUrl) {
    return "http://" + String(RELAY_SERVER) + ":" + String(RELAY_PORT)
           + "/stream?url=" + urlEncode(ytUrl);
}

// ===== Stream Control =====
void startStream(const String& url) {
    if (streaming) {
        stopRequested = true;
        int timeout = 0;
        while (streaming && timeout < 30) {
            delay(100);
            timeout++;
        }
    }
    streamUrl = url;
    streamRequested = true;
    Serial.println("[Web] Play: " + streamUrl);
}

void playTrack(int idx) {
    if (idx < 0 || idx >= plCount) return;
    currentTrack = idx;
    currentTitle = plTitles[idx];
    startStream(makeRelayUrl(playlist[idx]));
    Serial.println("[PL] Playing #" + String(idx) + ": " + plTitles[idx]);
}

// ===== Web Handlers =====
void handleRoot() {
    server.send(200, "text/html", INDEX_HTML);
}

void handlePlaylistAdd() {
    if (!server.hasArg("url")) {
        server.send(400, "application/json", "{\"error\":\"no url\"}");
        return;
    }
    if (plCount >= MAX_PLAYLIST) {
        server.send(400, "application/json", "{\"error\":\"playlist full (max 20)\"}");
        return;
    }
    playlist[plCount] = server.arg("url");
    plTitles[plCount] = server.hasArg("title") ? server.arg("title") : server.arg("url");
    plCount++;
    Serial.println("[PL] Added #" + String(plCount) + ": " + plTitles[plCount - 1]);
    server.send(200, "application/json", "{\"count\":" + String(plCount) + "}");
}

void handlePlaylistDel() {
    if (!server.hasArg("idx")) {
        server.send(400, "application/json", "{\"error\":\"no idx\"}");
        return;
    }
    int idx = server.arg("idx").toInt();
    if (idx < 0 || idx >= plCount) {
        server.send(400, "application/json", "{\"error\":\"invalid idx\"}");
        return;
    }
    for (int i = idx; i < plCount - 1; i++) {
        playlist[i] = playlist[i + 1];
        plTitles[i] = plTitles[i + 1];
    }
    plCount--;
    playlist[plCount] = "";
    plTitles[plCount] = "";
    if (currentTrack == idx) currentTrack = -1;
    else if (currentTrack > idx) currentTrack--;
    server.send(200, "application/json", "{\"count\":" + String(plCount) + "}");
}

void handlePlaylistClear() {
    for (int i = 0; i < plCount; i++) {
        playlist[i] = "";
        plTitles[i] = "";
    }
    plCount = 0;
    currentTrack = -1;
    server.send(200, "application/json", "{\"count\":0}");
}

void handlePlaylistGet() {
    String json = "{\"count\":" + String(plCount) + ",\"current\":" + String(currentTrack) + ",\"titles\":[";
    for (int i = 0; i < plCount; i++) {
        if (i > 0) json += ",";
        String t = plTitles[i];
        t.replace("\\", "\\\\");
        t.replace("\"", "\\\"");
        json += "\"" + t + "\"";
    }
    json += "]}";
    server.send(200, "application/json", json);
}

void handlePlay() {
    if (server.hasArg("idx")) {
        int idx = server.arg("idx").toInt();
        if (idx >= 0 && idx < plCount) {
            playTrack(idx);
            server.send(200, "application/json", "{\"status\":\"playing\"}");
            return;
        }
    }
    server.send(400, "application/json", "{\"error\":\"invalid idx\"}");
}

void handlePrev() {
    if (plCount == 0) {
        server.send(400, "application/json", "{\"error\":\"empty playlist\"}");
        return;
    }
    int idx = (currentTrack <= 0) ? plCount - 1 : currentTrack - 1;
    playTrack(idx);
    server.send(200, "application/json", "{\"status\":\"playing\"}");
}

void handleNext() {
    if (plCount == 0) {
        server.send(400, "application/json", "{\"error\":\"empty playlist\"}");
        return;
    }
    int idx = (currentTrack >= plCount - 1) ? 0 : currentTrack + 1;
    playTrack(idx);
    server.send(200, "application/json", "{\"status\":\"playing\"}");
}

void handleStop() {
    Serial.println("[Web] Stop");
    stopRequested = true;
    currentTitle = "";
    server.send(200, "application/json", "{\"status\":\"stopped\"}");
}

void handleVolume() {
    if (server.hasArg("v")) {
        int v = server.arg("v").toInt();
        if (v < 0) v = 0;
        if (v > 100) v = 100;
        volumePercent = v;
        Serial.println("[Vol] " + String(v) + "%");
    }
    server.send(200, "application/json", "{\"volume\":" + String(volumePercent) + "}");
}

void handleStatus() {
    bool btConn = a2dpSource.is_connected();
    String t = currentTitle;
    t.replace("\\", "\\\\");
    t.replace("\"", "\\\"");
    String json = "{\"wifi\":\"" + WiFi.localIP().toString() + "\","
                  "\"bt\":\"" + String(btConn ? "connected" : "disconnected") + "\","
                  "\"streaming\":" + String(streaming ? "true" : "false") + ","
                  "\"volume\":" + String(volumePercent) + ","
                  "\"track\":" + String(currentTrack) + ","
                  "\"title\":\"" + t + "\"}";
    server.send(200, "application/json", json);
}

// ===== Setup =====
void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println("===================================");
    Serial.println("IoT Music Player - BT Speaker");
    Serial.println("===================================");

    // PCM 버퍼 할당 (PSRAM 우선, 실패 시 힙)
    pcmBuffer = (int16_t*)ps_malloc(PCM_BUF_SIZE * sizeof(int16_t));
    if (!pcmBuffer) {
        Serial.println("[ERR] PSRAM alloc failed, using heap");
        pcmBuffer = (int16_t*)malloc(PCM_BUF_SIZE * sizeof(int16_t));
    }
    if (!pcmBuffer) {
        Serial.println("[ERR] Memory allocation failed! Restarting...");
        delay(3000);
        ESP.restart();
    }
    pcmMutex = xSemaphoreCreateMutex();
    Serial.println("[MEM] PCM buffer: " + String(PCM_BUF_SIZE * 2 / 1024) + "KB");

    // WiFi 연결
    Serial.print("[WiFi] Connecting to " + String(WIFI_SSID));
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 40) {
        delay(500);
        Serial.print(".");
        retries++;
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("[WiFi] Connected! IP: " + WiFi.localIP().toString());
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    } else {
        Serial.println("[WiFi] Connection failed! Restarting...");
        delay(5000);
        ESP.restart();
    }

    // Bluetooth A2DP 소스 시작
    Serial.println("[BT] Connecting to " + String(BT_SPEAKER_NAME) + "...");
    a2dpSource.set_auto_reconnect(true);
    a2dpSource.start(BT_SPEAKER_NAME, a2dp_callback);
    Serial.println("[BT] A2DP source started");
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);

    // HTTP 스트리밍 태스크 (코어 1에서 실행)
    xTaskCreatePinnedToCore(httpStreamTask, "httpStream", 16384, NULL, 2, NULL, 1);

    // 웹 서버 엔드포인트 등록
    server.on("/", HTTP_GET, handleRoot);
    server.on("/play", HTTP_POST, handlePlay);
    server.on("/stop", HTTP_POST, handleStop);
    server.on("/prev", HTTP_POST, handlePrev);
    server.on("/next", HTTP_POST, handleNext);
    server.on("/volume", HTTP_POST, handleVolume);
    server.on("/status", HTTP_GET, handleStatus);
    server.on("/playlist", HTTP_GET, handlePlaylistGet);
    server.on("/playlist/add", HTTP_POST, handlePlaylistAdd);
    server.on("/playlist/del", HTTP_POST, handlePlaylistDel);
    server.on("/playlist/clear", HTTP_POST, handlePlaylistClear);
    server.begin();

    Serial.println("[Web] Server: http://" + WiFi.localIP().toString());
    Serial.println("===================================");
}

void loop() {
    server.handleClient();
    delay(2);
}
