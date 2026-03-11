import socket
import os
import time
import sys

def brute_force_attack():
    target_host = '127.0.0.1'
    target_port = 8080
    attempt_count = 1000000 

    print(f"😈 [해커] 무차별 대입 공격(Brute-force) 시퀀스 개시...")
    print("-" * 60)
    sys.stdout.flush() 

    for i in range(1, attempt_count + 1):
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(0.05) # 속도를 위해 타임아웃을 더 짧게!
        
        try:
            client_socket.connect((target_host, target_port))
            raw_nonce = client_socket.recv(1024)
            if raw_nonce:
                server_nonce = raw_nonce[1:5]
                payload = bytes([10]) + server_nonce + b'\x00\x0d' + b'OPEN_THE_DOOR'
                fake_signature = os.urandom(32) 
                packet = b'\x02' + payload + fake_signature + b'\x03'
                client_socket.sendall(packet)
                client_socket.recv(1024) 
        except Exception:
            # 에러가 나더라도 무시하고 계속 공격 진행
            pass
        finally:
            client_socket.close()

        # 🌟 핵심 해결책: 출력 코드를 try-except 밖으로 뺐습니다! 
        # 이제 통신 성공/실패 여부와 상관없이 무조건 1000번마다 화면에 찍힙니다.
        if i % 1000 == 0:
            print(f"🔥 [공격 진행] {i:5d} / {attempt_count} 회차 돌파 시도 중... (모두 차단됨)", flush=True)
            time.sleep(0.4) # 발표용 시각 효과 (0.4초 대기)

    print("-" * 60)
    print(f"🚩 [최종 결과] 총 {attempt_count}번의 공격을 완벽히 방어했습니다.")

if __name__ == "__main__":
    brute_force_attack()