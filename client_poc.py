import socket
import time

# 접속할 서버 주소 (같은 컴퓨터니까 127.0.0.1)
HOST = '127.0.0.1' 
PORT = 8080

def start_client():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))
    print("📡 [클라이언트] 서버에 연결되었습니다!")

    # 서버로 보낼 가짜 데이터 (예: V자 제스처 인식함!)
    msg = "V_SIGN_DETECTED"
    msg_bytes = msg.encode('utf-8')
    
    # === 대망의 비트 단위 커스텀 패킷 조립 ===
    start_byte = b'\x02'                           # 1. 시작 알림
    cmd = bytes([10])                              # 2. 명령어 (10번 = 제스처 인증 요청)
    data_len = len(msg_bytes).to_bytes(2, 'big')   # 3. 데이터 길이
    checksum = bytes([0])                          # 4. 체크섬 (PoC라 일단 0으로 둠)
    end_byte = b'\x03'                             # 5. 끝 알림

    # 블록 장난감 조립하듯 하나로 합치기
    packet = start_byte + cmd + data_len + msg_bytes + checksum + end_byte
    
    print("📡 [클라이언트] 조립된 커스텀 패킷을 서버로 전송합니다...")
    time.sleep(1) # 극적인 효과를 위해 1초 대기
    client_socket.sendall(packet)

    # 서버가 잘 받았다고 보내는 응답 대기 (양방향)
    response = client_socket.recv(1024)
    print(f"📡 [클라이언트] 서버 응답 수신: {response}")

    client_socket.close()

if __name__ == "__main__":
    start_client()