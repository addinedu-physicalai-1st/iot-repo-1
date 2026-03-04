import socket

HOST = '127.0.0.1'
PORT = 8080

def start_client():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))
    print("📡 [클라이언트] 연결 성공!")

    # 1. 서버가 보낸 일회용 보안번호 받기
    raw_nonce = client_socket.recv(1024)
    if raw_nonce[0:1] == b'\x02' and raw_nonce[-1:] == b'\x03':
        server_nonce = raw_nonce[1:-1].decode()
        print(f"🔑 [클라이언트] 서버로부터 보안번호 수신: {server_nonce}")

    # 2. 보안번호를 포함한 패킷 조립
    msg = "V_SIGN_DETECTED"
    msg_bytes = msg.encode('utf-8')
    
    packet = (
        b'\x02' +                          # 시작
        bytes([10]) +                      # 명령어
        server_nonce.encode() +            # 서버에서 받은 일회용 번호 (중요!)
        len(msg_bytes).to_bytes(2, 'big') + # 데이터 길이
        msg_bytes +                        # 데이터
        b'\x00' +                          # 체크섬
        b'\x03'                            # 종료
    )
    
    print("📡 [클라이언트] 보안 패킷 전송...")
    client_socket.sendall(packet)

    # 3. 결과 확인
    response = client_socket.recv(1024)
    if response == b'\x02\x01\x03':
        print("🎉 [최종결과] 인증 완료! 문이 열립니다.")
    else:
        print("🚫 [최종결과] 인증 실패!")

    client_socket.close()

if __name__ == "__main__":
    start_client()