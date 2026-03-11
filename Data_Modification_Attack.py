import socket

def modification_attack():
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 서버 타임아웃 설정 (응답 없을 때 무한 대기 방지)
    client_socket.settimeout(2.0)
    
    try:
        client_socket.connect(('127.0.0.1', 8080))

        # 1. 서버가 준 마패(Nonce) 받기
        raw_nonce = client_socket.recv(1024)
        if not raw_nonce.startswith(b'\x02'):
            print("❌ 서버로부터 올바른 마패를 받지 못했습니다.")
            return
            
        server_nonce = raw_nonce[1:5] # \x02 다음 4자리 추출
        print(f"🔑 [해커] 마패 획득: {server_nonce.decode()}")

        # 2. 데이터 위조 (내용물 변경)
        fake_msg = "OPEN_THE_DOOR" 
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

        print("😈 [해커] 위조된 패킷 전송 중...")
        client_socket.sendall(packet)

        # 5. 서버 응답 확인
        response = client_socket.recv(1024)
        
        # [해결책] 인덱스로 직접 상태 값 확인 (\x02\x00\x03 에서 \x00이 인덱스 1번)
        if len(response) >= 2:
            status = response[1]
            if status == 0:
                print("🚩 [서버 응답]: 인증 실패! 다시 시도해주세요. (보안 시스템에 의해 차단됨)")
            elif status == 1:
                print("🎉 [서버 응답]: 인증 성공! (이 메시지가 뜨면 보안 뚫린 겁니다)")
        else:
            print(f"🚩 [서버 응답]: 알 수 없는 응답 - {response}")

    except Exception as e:
        print(f"❌ 에러 발생: {e}")
    finally:
        client_socket.close()

if __name__ == "__main__":
    modification_attack()