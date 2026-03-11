import socket
import time

def modification_attack():
    print("😈 [해커] 데이터 변조(Data Modification) 연속 공격 시퀀스 개시...\n")
    
    # 🌟 무한 루프 시작: 계속해서 변조된 데이터를 보냅니다.
    while True:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(2.0)
        
        try:
            client_socket.connect(('127.0.0.1', 8080))

            # 1. 서버가 준 마패(Nonce) 받기
            raw_nonce = client_socket.recv(1024)
            if not raw_nonce.startswith(b'\x02'):
                continue # 실패하면 끊고 다음 루프로 넘어감
                
            server_nonce = raw_nonce[1:5] 
            # print(f"🔑 [해커] 마패 획득: {server_nonce.decode()}") # 화면이 너무 복잡해지면 주석 처리해도 좋습니다.

            # 2. 데이터 위조 (내용물 변경)
            # 🌟 서버의 DPI가 확실하게 "조작"으로 인식하고 출력하도록 명확한 단어로 변경!
            fake_msg = "HACKED_DOOR_999" 
            msg_bytes = fake_msg.encode('utf-8')
            
            # v3 패킷 구조: [CMD(1)] + [NONCE(4)] + [LEN(2)] + [DATA]
            payload = (
                bytes([10]) + 
                server_nonce + 
                len(msg_bytes).to_bytes(2, 'big') + 
                msg_bytes
            )
            
            # 3. 가짜 도장(HMAC) 찍기 (32바이트 채우기)
            fake_signature = b'A' * 32 
            
            # 4. 최종 패킷 조립: [0x02] + [내용] + [도장] + [0x03]
            packet = b'\x02' + payload + fake_signature + b'\x03'

            print("😈 [해커] 위조된 패킷 전송 중... (내용: HACKED_DOOR_999)")
            client_socket.sendall(packet)

            # 5. 서버 응답 확인
            response = client_socket.recv(1024)
            
            if len(response) >= 2:
                status = response[1]
                if status == 0:
                    print("🚩 [서버 응답]: 인증 실패! (서버가 조작을 감지하고 차단함)")
                elif status == 1:
                    print("🎉 [서버 응답]: 인증 성공! (보안 뚫림)")

        except Exception as e:
            # 에러가 나도 스크립트가 죽지 않고 계속 공격하도록 패스
            pass 
        finally:
            client_socket.close()
            
        # 🌟 핵심: 브루트포스와 차별화되도록 2.5초마다 한 번씩 툭툭 찔러봅니다.
        time.sleep(2.5) 

if __name__ == "__main__":
    modification_attack()