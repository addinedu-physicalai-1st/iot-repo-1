import socket
import random
import hmac
import hashlib

HOST = '0.0.0.0'
PORT = 8080

# 🌟 핵심: 서버와 ESP32만 아는 절대 비밀번호 (절대 밖으로 유출되면 안 됨!)
SECRET_KEY = b'My_Super_Secret_Doorlock_Key_777'

expected_nonce = None

def start_server():
    global expected_nonce
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)
    print(f"🔒 [서버] 최고 보안(v3: HMAC+Nonce) 가동 중... (포트: {PORT})")

    conn, addr = server_socket.accept()
    print(f"✅ [서버] 연결됨! IP: {addr}")

    while True:
        try:
            # 1. 난수(마패) 발급
            if expected_nonce is None:
                expected_nonce = str(random.randint(1000, 9999)).encode()
                conn.sendall(b'\x02' + expected_nonce + b'\x03')
                print(f"🔑 [서버] 일회용 마패 발급: {expected_nonce.decode()}")

            # 2. 패킷 수신
            start_byte = conn.recv(1)
            if not start_byte: break

            if start_byte == b'\x02':
                # v3 패킷 구조: [CMD(1)] + [NONCE(4)] + [LEN(2)] + [DATA] + [HMAC서명(32)]
                # 편의상 끝 바이트(0x03)가 나올 때까지 다 읽기
                content = b''
                while True:
                    char = conn.recv(1)
                    if char == b'\x03': break
                    content += char
                
                # 수신된 데이터 분리 (맨 뒤 32바이트가 도장, 나머지가 내용물)
                received_signature = content[-32:]
                payload = content[:-32]

                # 🌟 [보안 1단계: 위조 검사] 서버가 직접 도장을 찍어보고 비교함
                expected_signature = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()
                
                # hmac.compare_digest는 타이밍 공격(해킹 기법)까지 막아주는 안전한 비교 함수
                if not hmac.compare_digest(received_signature, expected_signature):
                    print("🚫 [해킹경고] 데이터가 위조되었거나 비밀번호가 틀립니다! (도장 불일치)")
                    expected_nonce = None
                    conn.sendall(b'\x02\x00\x03')
                    continue

                # 🌟 [보안 2단계: 재전송 공격 검사] 도장이 진짜면 내용을 까봄
                cmd = payload[0:1]
                received_nonce = payload[1:5]
                data_len = int.from_bytes(payload[5:7], byteorder='big')
                data = payload[7:]

                if received_nonce == expected_nonce:
                    print(f"✅ [인증성공] 위조 없는 완벽한 패킷입니다! (마패 {received_nonce.decode()} 일치)")
                    print(f"  - 수신 데이터: {data.decode('utf-8')}")
                    expected_nonce = None
                    conn.sendall(b'\x02\x01\x03') # 문 열림 승인
                else:
                    print("❌ [인증실패] 도장은 맞지만 지난번 마패입니다. (재전송 공격 차단)")
                    expected_nonce = None
                    conn.sendall(b'\x02\x00\x03')

        except Exception as e:
            print(f"에러 발생: {e}")
            break

    conn.close()
    server_socket.close()

if __name__ == "__main__":
    start_server()