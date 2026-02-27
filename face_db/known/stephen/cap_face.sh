python3 -c "
import cv2, time
cam = cv2.VideoCapture(0)
for i in range(15):
    time.sleep(1)
    ret, frame = cam.read()
    if ret:
        cv2.imwrite(f'stephen_{i+1:03d}.jpg', frame)
        print(f'촬영 완료: stephen_{i+1:03d}.jpg')
cam.release()
"
