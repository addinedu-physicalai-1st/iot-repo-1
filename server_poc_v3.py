import socket
import random
import hmac
import hashlib
import time
import logging  # 🌟 현업 블랙박스를 위한 로그 모듈 추가!

# 🌟 로그 파일 설정 (이 코드가 security_audit.log 파일을 자동으로 만듭니다)
logging.basicConfig(
    filename='security_audit.log',  # 저장될 파일 이름
    level=logging.INFO,             # 기록할 위험도 레벨
    format='%(asctime)s | 공격 IP: %(name)s | %(message)s', # 로그에 적힐 양식 (시간 | IP | 내용)
    datefmt='%Y-%m-%d %H:%M:%S'     # 시간 표기 형식
)

HOST = '0.0.0.0'
PORT = 8080
SECRET_KEY = b'My_Super_Secret_Doorlock_Key_777'

def start_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)
    
    print("\033[0m" + f"🔒 [서버] 최고 보안(v3: HMAC+Nonce+Logging) 가동 중... (포트: {PORT})")
    print("📁 [시스템] 모든 방어 기록은 'security_audit.log' 파일에 자동 저장됩니다.")

    hmac_fail_count = 0    
    replay_fail_count = 0  
    last_fail_time = 0     

    while True: 
        conn, addr = server_socket.accept()
        client_ip = addr[0]  # 접속한 놈의 IP 주소 빼오기
        
        # IP 주소를 로그에 넣기 위해 임시 로거 생성
        logger = logging.getLogger(client_ip)
        
        expected_nonce = None 

        try:
            while True: 
                if expected_nonce is None:
                    expected_nonce = str(random.randint(1000, 9999)).encode()
                    conn.sendall(b'\x02' + expected_nonce + b'\x03')

                start_byte = conn.recv(1)
                if not start_byte: 
                    break 

                if start_byte == b'\x02':
                    content = b''
                    while True:
                        char = conn.recv(1)
                        if not char: break 
                        if char == b'\x03': break
                        content += char
                    
                    if not content: continue

                    received_signature = content[-32:]
                    payload = content[:-32]

                    # [관문 1] 도장(HMAC) 검사
                    expected_signature = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()
                    
                    if not hmac.compare_digest(received_signature, expected_signature):
                        # 🌟 DPI 심층 분석 (데이터 변조 잡아내기)
                        try:
                            tampered_cmd = payload[0:1]
                            tampered_data = payload[7:]
                            
                            if tampered_data not in [b'Open_The_Door', b'OPEN_THE_DOOR', b'']:
                                msg = f"패킷 내용 조작(Data Mod) 감지! (조작 데이터: {tampered_data.decode('utf-8', 'ignore')})"
                                print(f"\033[96m🛠️ [DPI 심층분석] {msg}\033[0m")
                                logger.warning(f"[위협 레벨: 높음] {msg}") # 📝 로그 파일에 쓰기!
                                
                                expected_nonce = None
                                conn.sendall(b'\x02\x00\x03')
                                continue 
                        except:
                            pass 

                        # 🌟 브루트포스 판별 로직
                        current_time = time.time() 
                        
                        if current_time - last_fail_time > 2.0:
                            hmac_fail_count = 0 
                            
                        last_fail_time = current_time 
                        hmac_fail_count += 1 

                        if hmac_fail_count >= 10000: 
                            msg = "무차별 대입(Brute-Force) 공격 한계치 도달! 통신 영구 차단!"
                            print(f"\n\033[41m\033[97m [시스템 경고] {msg} \033[0m\n")
                            logger.error(f"[위협 레벨: 치명적] {msg}") # 📝 로그 파일에 쓰기!
                            
                            conn.sendall(b'\x02\x00\x03')
                            hmac_fail_count = 0 
                            break 
                        elif hmac_fail_count > 3:
                            msg = f"무차별 대입(Brute-Force) 공격 진행 중... (누적: {hmac_fail_count}회)"
                            print(f"\033[91m🔥 [보안 경고] {msg}\033[0m")
                            # 브루트포스는 로그가 너무 커질 수 있으니 10번마다 한 번씩만 파일에 기록 (용량 조절)
                            if hmac_fail_count % 10 == 0:
                                logger.warning(f"[위협 레벨: 높음] {msg}") 
                        else:
                            msg = f"비정상 패킷 감지! (누적: {hmac_fail_count}회)"
                            print(f"\033[93m🚫 [보안 경고] {msg}\033[0m")

                        expected_nonce = None
                        conn.sendall(b'\x02\x00\x03')
                        continue

                    # [관문 2] 마패(Nonce) 검사 (재전송 공격 검사)
                    cmd = payload[0:1]
                    received_nonce = payload[1:5]
                    data_len = int.from_bytes(payload[5:7], byteorder='big')
                    data = payload[7:]

                    if received_nonce == expected_nonce:
                        print(f"\033[92m✅ [인증성공] 위조 없는 완벽한 패킷입니다! (마패 {received_nonce.decode()} 일치)\033[0m")
                        print(f"\033[92m  - 수신 데이터: {data.decode('utf-8')}\033[0m")
                        logger.info("[정상 접근] 문 열림 승인 완료") # 📝 정상 접근도 로그 남김!
                        
                        hmac_fail_count = 0
                        replay_fail_count = 0
                        expected_nonce = None
                        conn.sendall(b'\x02\x01\x03')
                    else:
                        replay_fail_count += 1  
                        msg = f"탈취된 과거 패킷입니다! 재전송(Replay) 공격 차단! (누적 방어: {replay_fail_count}회)"
                        print(f"\033[95m🔄 [보안 2단계 방어] {msg}\033[0m")
                        logger.warning(f"[위협 레벨: 중간] {msg}") # 📝 로그 파일에 쓰기!
                        
                        expected_nonce = None
                        conn.sendall(b'\x02\x00\x03')

        except Exception as e:
            pass 
        finally:
            conn.close() 

if __name__ == "__main__":
    start_server()