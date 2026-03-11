import socket
import hmac
import hashlib
import time

HOST = '127.0.0.1'
PORT = 8080
# 🌟 해커가 과거의 통신 기록을 통째로 훔쳤다고 가정 (서버와 동일한 키로 완벽한 도장 생성)
SECRET_KEY = b'My_Super_Secret_Doorlock_Key_777' 

print("😈 [해커] 재전송 공격(Replay Attack) 시퀀스 개시...")
print("😈 [해커] 과거에 탈취한 '완벽한 인증 패킷'을 장전합니다.\n")

while True:
    try:
        # 1. 서버 접속
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((HOST, PORT))
        
        # 2. 서버가 현재 발급하는 새 마패(Nonce) 수신 
        # (해커는 이거 무시합니다. 어차피 훔쳐둔 옛날 패킷을 통째로 쏠 거니까요!)
        server_hello = client_socket.recv(1024)
        
        # 3. 완벽하게 조작된 옛날 패킷 만들기 (과거의 마패 '1111' 사용)
        old_nonce = b'1111'
        cmd = b'\x01' # 문 열어!
        data = b'Open_The_Door'
        data_len = len(data).to_bytes(2, byteorder='big')
        
        payload = cmd + old_nonce + data_len + data
        # 🌟 서버의 1차 관문을 완벽하게 통과하기 위해 진짜 도장을 찍음
        signature = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest() 
        
        fake_packet = b'\x02' + payload + signature + b'\x03'
        
        # 4. 공격!
        client_socket.sendall(fake_packet)
        
        # 5. 결과 확인
        response = client_socket.recv(1024)
        if response == b'\x02\x00\x03':
            print("🚩 [서버 응답]: 인증 실패! (서버가 재전송 공격을 눈치채고 차단했습니다.)")
        
        client_socket.close()
        time.sleep(1.5) # 영상에서 잘 보이게 1.5초마다 한 번씩 툭툭 던짐
        
    except Exception as e:
        print(f"오류: {e}")
        time.sleep(1.5)