import socket
import random

HOST = '0.0.0.0'
PORT = 8080

# 현재 유효한 일회용 번호를 저장하는 변수
expected_nonce = None

def start_server():
    global expected_nonce
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST, PORT))
    server_socket.listen(1)
    print(f"🔒 [서버] 보안 레벨 2 가동 중... (포트: {PORT})")

    conn, addr = server_socket.accept()
    print(f"✅ [서버] 연결됨! IP: {addr}")

    while True:
        try:
            # 1. 클라이언트가 연결되자마자 서버가 '일회용 번호'를 생성해서 보냄
            if expected_nonce is None:
                expected_nonce = random.randint(1000, 9999)
                # [시작(0x02)] + [Nonce(4byte)] + [끝(0x03)]
                nonce_packet = b'\x02' + str(expected_nonce).encode() + b'\x03'
                conn.sendall(nonce_packet)
                print(f"🔑 [서버] 일회용 보안번호 발급: {expected_nonce}")

            # 2. 패킷 수신 대기
            start_byte = conn.recv(1)
            if not start_byte: break

            if start_byte == b'\x02':
                # 헤더 읽기: [명령어(1)] + [보안번호(4)] + [데이터길이(2)]
                header = conn.recv(7)
                cmd = header[0]
                received_nonce = header[1:5].decode()
                data_len = int.from_bytes(header[5:7], byteorder='big')
                
                # 본문 읽기
                data = conn.recv(data_len)
                
                # 꼬리 읽기
                tail = conn.recv(2)
                end_byte = tail[1:2]

                # 보안 검증 (시작/끝 확인 + 일회용 번호 일치 확인)
                if end_byte == b'\x03' and received_nonce == str(expected_nonce):
                    print(f"✅ [인증성공] 보안번호 {received_nonce} 일치!")
                    print(f"  - 수신 데이터: {data.decode('utf-8')}")
                    
                    # 보안번호 폐기 (다음 통신을 위해)
                    expected_nonce = None 
                    conn.sendall(b'\x02' + b'\x01' + b'\x03') # 성공 응답
                else:
                    print(f"❌ [인증실패] 잘못된 접근 혹은 중복된 보안번호!")
                    expected_nonce = None # 실패 시에도 번호 교체
                    conn.sendall(b'\x02' + b'\x00' + b'\x03') # 실패 응답

        except Exception as e:
            print(f"에러 발생: {e}")
            break

    conn.close()
    server_socket.close()

if __name__ == "__main__":
    start_server()