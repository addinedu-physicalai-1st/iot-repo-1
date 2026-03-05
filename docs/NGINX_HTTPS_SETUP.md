# nginx HTTPS 역방향 프록시 설정

원격 접속 시 브라우저 마이크(`getUserMedia`)를 사용하려면 **HTTPS**가 필요합니다.  
nginx 역방향 프록시로 HTTPS를 제공하는 방법입니다.

## 요구사항

- nginx 설치: `sudo apt install nginx` (Ubuntu/Debian)
- openssl (인증서 생성용, 대부분 기본 설치됨)

## 빠른 시작 (자체 서명 인증서)

### 1. SSL 인증서 생성

```bash
./scripts/ssl_generate_cert.sh
```

### 2. Python 서버 실행

```bash
./run_server.sh
```

### 3. nginx 실행

```bash
./scripts/run_nginx.sh
```

### 4. 방화벽 열기 (원격 접속 시 필수)

같은 네트워크의 다른 기기에서 접속하려면 **포트 443**을 열어야 합니다:

```bash
# UFW 사용 시
sudo ufw allow 443/tcp
sudo ufw reload
sudo ufw status
```

### 5. 접속

- **로컬**: https://localhost
- **원격**: https://서버IP (예: https://192.168.0.189)

자체 서명 인증서이므로 브라우저에서 "안전하지 않음" 경고가 뜹니다.  
**고급** → **계속 진행(안전하지 않음)** 으로 접속하면 됩니다.

### nginx 중지

```bash
./scripts/run_nginx.sh stop
```

---

## 시스템 nginx에 등록 (선택)

프로젝트 외부에서 nginx를 서비스로 실행하려면:

```bash
# 사이트 설정 복사
sudo cp nginx/nginx.conf /etc/nginx/sites-available/iot

# 심볼릭 링크 (활성화)
sudo ln -sf ../sites-available/iot /etc/nginx/sites-enabled/

# 기본 사이트 비활성화 (포트 80/443 충돌 시)
# sudo rm /etc/nginx/sites-enabled/default

# SSL 경로 수정: /etc/nginx/sites-available/iot 에서
# ssl_certificate     /home/jr/dev_ws/iot-repo-1/nginx/ssl/iot.pem;
# ssl_certificate_key /home/jr/dev_ws/iot-repo-1/nginx/ssl/iot-key.pem;
# (절대 경로로 변경)

sudo nginx -t && sudo systemctl reload nginx
```

---

## Let's Encrypt (실제 도메인 사용 시)

도메인이 있고 80/443 포트가 외부에 열려 있다면 무료 인증서 사용 가능:

```bash
# certbot 설치
sudo apt install certbot python3-certbot-nginx

# 인증서 발급 (도메인 예: iot.example.com)
sudo certbot certonly --standalone -d iot.example.com

# nginx 설정에서 경로 변경:
# ssl_certificate     /etc/letsencrypt/live/iot.example.com/fullchain.pem;
# ssl_certificate_key /etc/letsencrypt/live/iot.example.com/privkey.pem;
```

---

## 포트 정리

| 포트 | 용도 |
|------|------|
| 80   | HTTP → HTTPS 리다이렉트 |
| 443  | HTTPS (nginx) → localhost:8000 프록시 |
| 8000 | FastAPI (내부용, nginx가 프록시) |
| 9000 | ESP32 TCP (직접 연결) |

원격에서는 **443(HTTPS)** 으로만 접속하면 됩니다.
