import socket
import hmac
import hashlib

HOST = '127.0.0.1'
PORT = 8080

# 🌟 핵심: 서버랑 똑같은 비밀번호를 들고 있어야 함
SECRET_KEY = b'My_Super_Secret_Doorlock_Key_777'

def start_client():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))
    print("📡 [클라이언트] 서버에 연결되었습니다.")

    # 1. 서버가 준 마패 받기
    raw_nonce = client_socket.recv(1024)
    if raw_nonce[0:1] == b'\x02' and raw_nonce[-1:] == b'\x03':
        server_nonce = raw_nonce[1:-1]
        print(f"🔑 [클라이언트] 마패 수신 완료: {server_nonce.decode()}")

    # 2. 보낼 내용물(Payload) 조립
    msg = "FACE_DETECTED" # (예시: 얼굴 인식 성공)
    msg_bytes = msg.encode('utf-8')
    
    payload = (
        bytes([10]) +                      # CMD (명령어)
        server_nonce +                     # NONCE (서버가 준 마패)
        len(msg_bytes).to_bytes(2, 'big') + # LEN (데이터 길이)
        msg_bytes                          # DATA (실제 내용)
    )

    # 🌟 3. 내용물에 비밀 도장(HMAC 서명) 쾅 찍기! (SHA-256 사용)
    signature = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()

    # 4. 최종 패킷 발송: [시작] + [내용물] + [도장] + [끝]
    packet = b'\x02' + payload + signature + b'\x03'
    
    print("📡 [클라이언트] 위조 방지 도장(HMAC)이 찍힌 패킷 전송...")
    client_socket.sendall(packet)

    # 5. 결과 확인
    response = client_socket.recv(1024)
    if response == b'\x02\x01\x03':
        print("🎉 [최종결과] 문이 열립니다!")
    else:
        print("🚫 [최종결과] 인증 실패!")

    client_socket.close()

if __name__ == "__main__":
    start_client()