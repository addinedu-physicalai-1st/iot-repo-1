import socket

# 서버 설정 (내 컴퓨터의 모든 IP에서 접속 허용, 포트는 8080)
HOST = '0.0.0.0'
PORT = 8080

def start_server():
    # TCP 소켓 생성
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)
    print(f"🔒 [서버] 대기 중... (포트: {PORT})")

    # 클라이언트(ESP32)가 접속할 때까지 대기
    conn, addr = server_socket.accept()
    print(f"✅ [서버] ESP32(클라이언트) 연결됨! IP: {addr}")

    while True:
        try:
            # 1. 패킷의 '시작 바이트(1byte)'만 먼저 읽어보기
            start_byte = conn.recv(1)
            if not start_byte:
                break

            # 중요부분 1!'시작'을 알리는 특수 기호(0x02)가 맞는지 확인
            if start_byte == b'\x02': 
                print("\n[수신] 🟢 패킷 시작(0x02) 감지!")
                
                # 2. 명령어(1byte) + 데이터 길이(2byte) 읽기
                header = conn.recv(3)
                cmd = header[0]
                data_len = int.from_bytes(header[1:3], byteorder='big')
                
                # 3. 데이터 길이만큼 실제 내용(Payload) 읽기
                data = conn.recv(data_len)
                
                # 4. 체크섬(1byte) + 종료 바이트(1byte) 읽기
                tail = conn.recv(2)
                end_byte = tail[1:2]

                # 중요부분 2! '끝'을 알리는 특수 기호(0x03)로 잘 닫혔는지 검증
                if end_byte == b'\x03': 
                    print(f"  - 명령어 번호: {cmd}")
                    print(f"  - 전달받은 내용: {data.decode('utf-8')}")
                    print("[완료] 🟢 정상적인 패킷입니다. (끝 바이트 0x03 확인 완료)")
                    
                    # 서버 -> ESP32로 "문 열어라!" 응답 보내기 (양방향 통신 증명)
                    # [시작(0x02)] + [성공코드(0x01)] + [끝(0x03)]
                    response = b'\x02' + b'\x01' + b'\x03' 
                    conn.sendall(response)
                    print("[송신] ESP32로 응답 전송 완료\n")
                else:
                    print("[에러] 🔴 비정상적인 패킷 (끝 바이트가 다름)")

        except ConnectionResetError:
            break

    conn.close()
    server_socket.close()
    print("서버 종료됨.")

if __name__ == "__main__":
    start_server()