import cv2
import time
import numpy as np
from flask import Flask, Response, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from multiprocessing import Queue
from .webrtc_producer import start_webrtc, send_command, ensure_normal_mode_once
import threading
from ultralytics import YOLO
import logging
import json
import os
from datetime import datetime
import asyncio
import traceback


logging.basicConfig(level=logging.INFO)

# Calculate path relative to this file
base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
socketio = SocketIO(app, cors_allowed_origins="*")
frame_queue = Queue(maxsize=10)
command_queue = Queue(maxsize=10)

# YOLO 모델 로드
try:
    model_path = os.path.join(template_dir, 'best.pt')
    yolo_model = YOLO(model_path)
    print(f"✅ YOLO 모델 로드 성공: {model_path}")
except Exception as e:
    print(f"❌ YOLO 모델 로드 실패: {e}")
    yolo_model = None


# WebRTC 프레임 수신 시작
start_webrtc(frame_queue, command_queue)

# 🔥 Fire 감지 추적 변수들에 alert_count 추가
fire_detection_start_time = None
fire_continuous_detection = False
fire_last_alert_time = None
fire_detection_active = True
fire_alert_count = 0  # 🆕 알림 카운터 추가
FIRE_DETECTION_THRESHOLD = 5.0
FIRE_CONFIDENCE_THRESHOLD = 0.5
FIRE_ALERT_INTERVAL = 5.0

# 🆕 YOLO 활성화 상태 변수
yolo_active = True

# 🆕 LIDAR 모드 전환 변수들
lidar_view_mode = False  # False: 비디오 모드, True: LIDAR 모드
lidar_enabled = False
lidar_task = None
lidar_connection = None
message_count = 0

# 🆕 LIDAR 상수들 (plot_lidar_stream.py와 동일)
ROTATE_X_ANGLE = np.pi / 2  # 90 degrees
ROTATE_Z_ANGLE = np.pi      # 180 degrees
minYValue = 0
maxYValue = 100


def check_fire_detection(current_boxes):
    """Fire 감지 상태 확인 및 알림 처리 (alert_count 버그 수정)"""
    global fire_detection_start_time, fire_continuous_detection, fire_last_alert_time, fire_alert_count
    
    # 현재 프레임에서 고신뢰도 Fire 탐지 여부 확인
    high_confidence_fire = False
    max_confidence = 0.0
    
    for box_info in current_boxes:
        if len(box_info) >= 6:
            _, _, _, _, label, confidence = box_info
            if label == "fire" and confidence >= FIRE_CONFIDENCE_THRESHOLD:
                high_confidence_fire = True
                max_confidence = max(max_confidence, confidence)
    
    current_time = time.time()
    
    if high_confidence_fire:
        if not fire_continuous_detection:
            # 🔧 새로운 화재 감지 시작
            fire_detection_start_time = current_time
            fire_continuous_detection = True
            fire_last_alert_time = None
            fire_alert_count = 0  # 🆕 카운터 초기화
            print(f"🔥 Fire 감지 시작! (신뢰도 {max_confidence:.2f})")
        
        detection_duration = current_time - fire_detection_start_time
        
        if detection_duration >= FIRE_DETECTION_THRESHOLD and fire_last_alert_time is None:
            # 🔧 첫 번째 알림 (5초 후)
            fire_alert_count = 1  # 🆕 첫 번째 알림
            print(f"🚨 화재 첫 알림! ({detection_duration:.1f}초 연속 감지)")
            # save_fire_alert(is_repeat=False, alert_count=fire_alert_count) # Removed Discord alert
            fire_last_alert_time = current_time
            
        elif (fire_last_alert_time is not None and 
              current_time - fire_last_alert_time >= FIRE_ALERT_INTERVAL):
            # 🔧 반복 알림 (5초마다)
            fire_alert_count += 1  # 🆕 카운터 증가
            print(f"🚨 화재 반복 알림 #{fire_alert_count}! (총 {detection_duration:.1f}초 연속 감지)")
            # save_fire_alert(is_repeat=True, alert_count=fire_alert_count) # Removed Discord alert
            fire_last_alert_time = current_time
            
    else:
        # Fire 감지 안됨 - 상태 초기화
        if fire_continuous_detection:
            detection_duration = current_time - fire_detection_start_time
            print(f"🔥 Fire 감지 종료 (총 {detection_duration:.1f}초 감지됨, 총 {fire_alert_count}회 알림)")
            
        fire_continuous_detection = False
        fire_detection_start_time = None
        fire_last_alert_time = None
        fire_alert_count = 0  # 🆕 카운터 초기화

# generate() 함수에서 YOLO 로직 완전 복원
def generate():
    """비디오 스트림 생성 - 해상도 제어 + 완전한 YOLO 로직"""
    last_detect_time = 0
    last_boxes = []
    last_boxes = []
    
    # 🆕 목표 해상도 설정
    TARGET_WIDTH = 640
    TARGET_HEIGHT = 360
    JPEG_QUALITY = 85  # JPEG 품질 (1-100)
    
    while True:
        if not frame_queue.empty():
            img = frame_queue.get()
            now = time.time()
            
            # 🆕 이미지 해상도 확인 및 조정
            original_height, original_width = img.shape[:2]
            
            if original_width != TARGET_WIDTH or original_height != TARGET_HEIGHT:
                # 비율 유지하면서 리사이즈
                aspect_ratio = original_width / original_height
                target_aspect_ratio = TARGET_WIDTH / TARGET_HEIGHT
                
                if aspect_ratio > target_aspect_ratio:
                    # 가로가 더 긴 경우
                    new_width = TARGET_WIDTH
                    new_height = int(TARGET_WIDTH / aspect_ratio)
                else:
                    # 세로가 더 긴 경우
                    new_height = TARGET_HEIGHT
                    new_width = int(TARGET_HEIGHT * aspect_ratio)
                
                # 이미지 리사이즈
                img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
                
                # 중앙 정렬을 위한 패딩 (필요한 경우)
                if new_width != TARGET_WIDTH or new_height != TARGET_HEIGHT:
                    # 검은색 배경에 중앙 정렬
                    pad_img = np.zeros((TARGET_HEIGHT, TARGET_WIDTH, 3), dtype=np.uint8)
                    start_y = (TARGET_HEIGHT - new_height) // 2
                    start_x = (TARGET_WIDTH - new_width) // 2
                    pad_img[start_y:start_y+new_height, start_x:start_x+new_width] = img
                    img = pad_img
                
                print(f"📺 해상도 조정: {original_width}x{original_height} → {TARGET_WIDTH}x{TARGET_HEIGHT}")
            
            # 🔧 YOLO 감지 (완전한 기존 로직 복원)
            if yolo_active and yolo_model and now - last_detect_time > 1.0:
                try:
                    # YOLO 추론 실행
                    results = yolo_model(img)
                    last_boxes = []
                    
                    for result in results:
                        boxes = result.boxes
                        if boxes is not None:
                            for box in boxes:
                                cls = int(box.cls[0])
                                label = yolo_model.names[cls]
                                if label in ["person", "fire"]:
                                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                                    confidence = float(box.conf[0])
                                    last_boxes.append((x1, y1, x2, y2, label, confidence))
                    
                    # 🔥 Fire 감지 체크 (기존 로직 완전 유지)
                    check_fire_detection(last_boxes)
                    last_detect_time = now
                    
                except Exception as e:
                    print(f"❌ YOLO 처리 오류: {e}")
                    last_boxes = []
                    
            elif not yolo_active:
                # YOLO 비활성화 시 빈 배열
                last_boxes = []
            
            
            # 🎯 YOLO 결과 표시 (기존 로직 완전 복원)
            if yolo_active and last_boxes:
                for box_info in last_boxes:
                    if len(box_info) == 6:
                        x1, y1, x2, y2, label, confidence = box_info
                    else:
                        x1, y1, x2, y2 = box_info[:4]
                        label = "person"
                        confidence = 0.0
                    
                    # 🔥 Fire 감지 시 색상 및 텍스트
                    if label == "fire":
                        color = (0, 0, 255)  # 빨간색
                        display_text = f"FIRE {confidence:.2f}"
                        
                        # 🔥 Fire 연속 감지 시 깜빡임 효과
                        if confidence >= FIRE_CONFIDENCE_THRESHOLD and fire_continuous_detection:
                            if int(time.time() * 2) % 2:  # 0.5초마다 깜빡임
                                color = (0, 255, 255)  # 노란색으로 깜빡임
                            display_text = f" FIRE {confidence:.2f} "
                            
                    elif label == "person":
                        color = (0, 255, 0)  # 초록색
                        display_text = f"PERSON {confidence:.2f}"
                    
                    # 바운딩 박스 및 텍스트 그리기
                    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(img, display_text, (x1, y1 - 10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            
            
            # 🔥 Fire 감지 상태 표시 (기존 로직 완전 복원)
            if yolo_active and fire_continuous_detection and fire_detection_start_time:
                detection_duration = time.time() - fire_detection_start_time
                status_text = f"Fire detecting: {detection_duration:.1f}s"
                
                if detection_duration >= FIRE_DETECTION_THRESHOLD:
                    status_color = (0, 0, 255)  # 빨간색
                    if fire_last_alert_time is not None:
                        elapsed_alert_time = time.time() - fire_last_alert_time
                        if elapsed_alert_time < FIRE_ALERT_INTERVAL:
                            status_text += f" (next alarm in {FIRE_ALERT_INTERVAL - elapsed_alert_time:.1f}s)"
                else:
                    status_color = (0, 165, 255)  # 주황색
                
                cv2.putText(img, status_text, (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
            
            
            # 🎯 YOLO 상태 표시 (기존 로직 완전 유지)
            yolo_status_text = f"YOLO: {'ON' if yolo_active else 'OFF'}"
            yolo_status_color = (0, 255, 0) if yolo_active else (0, 0, 255)
            cv2.putText(img, yolo_status_text, (10, 90), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, yolo_status_color, 2)
            
            # 🆕 JPEG 인코딩 품질 제어
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            ret, jpeg = cv2.imencode('.jpg', img, encode_params)
            
            if not ret:
                continue
                
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        else:
            time.sleep(0.01)

@app.route('/video_feed')
def video_feed():
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/move', methods=['POST'])
def move():
    data = request.get_json()
    direction = data.get('direction')
    send_command(command_queue, direction)
    return jsonify({'status': 'ok', 'direction': direction})

@app.route('/joystick', methods=['POST'])
def joystick():
    data = request.get_json()
    x = float(data.get('x', 0))
    z = float(data.get('z', 0))
    send_command(command_queue, ('joystick', x, z))
    return jsonify({'status': 'ok'})

@app.route('/start_control', methods=['POST'])
def start_control():
    ok = ensure_normal_mode_once()
    return jsonify({'status': 'ok' if ok else 'fail'})


# 🆕 YOLO 토글 엔드포인트 (ArUco 스캔과 동일한 단순 방식)
@app.route('/toggle_yolo', methods=['POST'])
def toggle_yolo():
    """YOLO 활성화/비활성화 토글 - ArUco 스캔과 동일한 단순 방식"""
    global yolo_active
    
    try:
        # 🔧 현재 상태 저장
        previous_state = yolo_active
        
        # 🔧 상태 토글 (단순하게)
        yolo_active = not yolo_active
        
        status_text = "활성화" if yolo_active else "비활성화"
        print(f"🎯 YOLO 상태 변경: {previous_state} → {yolo_active} ({status_text})")
        
        # 🔧 ArUco 스캔과 동일한 방식의 응답
        response_data = {
            'status': 'success',
            'yolo_active': yolo_active,
            'message': f'YOLO가 {status_text}되었습니다'
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ YOLO 토글 처리 오류: {e}")
        
        return jsonify({
            'status': 'error',
            'message': f'YOLO 토글 처리 실패: {str(e)}',
            'yolo_active': yolo_active
        })



def get_connection_status():
    """현재 WebRTC 연결 상태 반환"""
    try:
        # 🔧 _conn_holder import 추가
        from webrtc_producer import _conn_holder
        
        if _conn_holder and 'conn' in _conn_holder and _conn_holder['conn']:
            conn = _conn_holder['conn']
            
            # 연결 상태 확인
            status = {
                'connected': True,
                'has_datachannel': hasattr(conn, 'datachannel') and conn.datachannel is not None,
                'has_video': hasattr(conn, 'video') and conn.video is not None,
                'has_audio': hasattr(conn, 'audio') and conn.audio is not None,
                'connection_time': getattr(conn, '_connection_time', 'Unknown')
            }
            
            return status
        else:
            return {
                'connected': False,
                'has_datachannel': False,
                'has_video': False, 
                'has_audio': False,
                'connection_time': None
            }
    except Exception as e:
        print(f"❌ 연결 상태 확인 오류: {e}")
        return {
            'connected': False,
            'error': str(e)
        }

def is_connection_ready_for_audio():
    """오디오 브리지를 위한 연결 준비 상태 확인"""
    status = get_connection_status()
    return status.get('connected', False) and status.get('has_datachannel', False)

def get_robot_current_state():
    """현재 로봇 상태를 webrtc_producer에서 가져오기"""
    try:
        from webrtc_producer import get_robot_status
        status = get_robot_status()
        return status.get('robot_state', 'unknown')
    except Exception as e:
        print(f"⚠️ 로봇 상태 조회 실패: {e}")
        return 'unknown'

# 🆕 LIDAR 관련 함수들 추가

def rotate_points(points, x_angle, z_angle):
    """Rotate points around the x and z axes by given angles."""
    rotation_matrix_x = np.array([
        [1, 0, 0],
        [0, np.cos(x_angle), -np.sin(x_angle)],
        [0, np.sin(x_angle), np.cos(x_angle)]
    ])
    
    rotation_matrix_z = np.array([
        [np.cos(z_angle), -np.sin(z_angle), 0],
        [np.sin(z_angle), np.cos(z_angle), 0],
        [0, 0, 1]
    ])
    
    points = points @ rotation_matrix_x.T
    points = points @ rotation_matrix_z.T
    return points

async def lidar_callback_task(message):
    """Task to process incoming LIDAR data - plot_lidar_stream.py와 거의 동일"""
    global message_count, minYValue, maxYValue
    
    try:
        # 🔧 LIDAR 활성화 상태만 체크
        if not lidar_enabled:
            return
            
        # 🔧 첫 번째 메시지 수신 시 알림
        if message_count == 0:
            print("🎉 첫 번째 LIDAR 메시지 수신!")
            
        # 🔧 plot_lidar_stream.py와 동일한 skip 로직 (현재는 모든 메시지 처리)
        if message_count % 1 != 0:  # args.skip_mod 대신 1 사용
            message_count += 1
            return

        # 🔧 데이터 추출 (plot_lidar_stream.py와 동일)
        positions = message["data"]["data"].get("positions", [])
        origin = message["data"].get("origin", [])
        
        # 🔧 positions가 numpy 배열인지 확인하고 안전하게 처리
        positions_length = 0
        has_positions = False
        
        if positions is not None:
            if hasattr(positions, '__len__'):
                positions_length = len(positions)
                has_positions = positions_length > 0
            else:
                has_positions = False
        
        print(f"🔍 LIDAR 데이터 구조 확인: positions 길이={positions_length}, origin={origin}")
        
        if not has_positions:
            message_count += 1
            print(f"⚠️ 빈 LIDAR 데이터 (메시지 #{message_count})")
            return
            
        # 🔧 포인트 변환 (plot_lidar_stream.py와 동일)
        points = np.array([positions[i:i+3] for i in range(0, len(positions), 3)], dtype=np.float32)
        total_points = len(points)
        unique_points = np.unique(points, axis=0)
        
        if len(unique_points) == 0:
            message_count += 1
            print(f"⚠️ unique_points가 0개 (메시지 #{message_count})")
            return

        # 🔧 회전 및 필터링 (plot_lidar_stream.py와 동일)
        rotated_points = rotate_points(unique_points, ROTATE_X_ANGLE, ROTATE_Z_ANGLE)
        filtered_points = rotated_points[(rotated_points[:, 1] >= minYValue) & (rotated_points[:, 1] <= maxYValue)]
        
        if len(filtered_points) == 0:
            message_count += 1
            print(f"⚠️ filtered_points가 0개 (메시지 #{message_count})")
            return

        # 🔧 중심점 계산 (plot_lidar_stream.py와 동일)
        center_x = float(np.mean(filtered_points[:, 0]))
        center_y = float(np.mean(filtered_points[:, 1]))
        center_z = float(np.mean(filtered_points[:, 2]))

        # 🔧 중심점으로 오프셋 (plot_lidar_stream.py와 동일)
        offset_points = filtered_points - np.array([center_x, center_y, center_z])

        # 🔧 로그 메시지 (plot_lidar_stream.py와 동일)
        message_count += 1
        print(f"📡 LIDAR Message {message_count}: Total points={total_points}, Unique points={len(unique_points)}, Filtered={len(filtered_points)}")

        # 🔧 거리 기반 색상 스칼라 (plot_lidar_stream.py와 동일)
        scalars = np.linalg.norm(offset_points, axis=1)

        # 🔧 SocketIO로 LIDAR 데이터 전송 (LIDAR 모드일 때만)
        if lidar_view_mode:
            socketio.emit("lidar_data", {
                "points": offset_points.tolist(),
                "scalars": scalars.tolist(),
                "center": {"x": center_x, "y": center_y, "z": center_z}
            })
            print(f"📤 SocketIO로 {len(offset_points)}개 포인트 전송됨")
        else:
            print(f"⏸️ 비디오 모드 - LIDAR 데이터 처리만 수행: {len(offset_points)}개 포인트")

    except Exception as e:
        print(f"❌ LIDAR 콜백 오류: {e}")
        print(f"🔍 상세 오류: {traceback.format_exc()}")

async def lidar_webrtc_connection():
    """LIDAR WebRTC 연결 및 데이터 처리 - 자동 재연결 기능 포함"""
    global lidar_connection, message_count
    
    max_retries = 3
    retry_delay = 5
    last_message_count = 0
    connection_health_check_interval = 10  # 10초마다 연결 상태 확인
    
    while lidar_enabled:
        try:
            # � 기존 연결 재사용 시도
            from webrtc_producer import _conn_holder
            
            conn = None
            use_existing_connection = False
            
            if _conn_holder and 'conn' in _conn_holder and _conn_holder['conn']:
                potential_conn = _conn_holder['conn']
                print("🔗 기존 WebRTC 연결 상태 확인 중...")
                
                # 🆕 연결 상태 확인
                connection_state = 'unknown'
                if hasattr(potential_conn, '_peer_connection'):
                    connection_state = getattr(potential_conn._peer_connection, 'connectionState', 'unknown')
                    print(f"📡 WebRTC 연결 상태: {connection_state}")
                
                if connection_state in ['connected', 'connecting'] and hasattr(potential_conn, 'datachannel') and potential_conn.datachannel:
                    print("✅ 기존 WebRTC 연결을 LIDAR용으로 재사용")
                    conn = potential_conn
                    use_existing_connection = True
                else:
                    print(f"⚠️ 기존 연결 상태 불량 ({connection_state}) - 새 연결 생성 필요")
            
            # 새 연결 생성 (기존 연결이 없거나 상태가 불량한 경우)
            if not use_existing_connection:
                print("🔄 새로운 LIDAR 전용 WebRTC 연결 생성 중...")
                
                from go2_webrtc_connect.go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod
                from config.settings import SERIAL_NUMBER, UNITREE_USERNAME, UNITREE_PASSWORD
                
                conn = Go2WebRTCConnection(
                    WebRTCConnectionMethod.Remote,
                    serialNumber=SERIAL_NUMBER,
                    username=UNITREE_USERNAME,
                    password=UNITREE_PASSWORD
                )
                
                print("� LIDAR WebRTC 연결 시도...")
                await conn.connect()
                print("✅ LIDAR WebRTC 연결 성공")
            
            # 트래픽 저장 모드 비활성화
            print("🔄 트래픽 저장 모드 비활성화 시도...")
            try:
                if asyncio.iscoroutinefunction(conn.datachannel.disableTrafficSaving):
                    await asyncio.wait_for(
                        conn.datachannel.disableTrafficSaving(True),
                        timeout=5.0  # 5초 타임아웃
                    )
                    print("✅ 트래픽 저장 모드 비활성화됨 (비동기)")
                else:
                    conn.datachannel.disableTrafficSaving(True)
                    print("✅ 트래픽 저장 모드 비활성화됨 (동기)")
            except asyncio.TimeoutError:
                print("⚠️ 트래픽 저장 모드 설정 타임아웃 - 계속 진행")
            except Exception as traffic_err:
                print(f"⚠️ 트래픽 저장 모드 설정 건너뜀: {traffic_err}")
            
            # LIDAR 센서 ON 명령
            print("🔄 LIDAR 센서 활성화 시도...")
            conn.datachannel.pub_sub.publish_without_callback("rt/utlidar/switch", "on")
            print("✅ LIDAR 센서 'ON' 명령 전송됨")
            
            # 센서 초기화 대기
            print("⏳ LIDAR 센서 초기화 대기 중...")
            await asyncio.sleep(3)
            
            # LIDAR 데이터 구독
            print("🔄 LIDAR 데이터 구독 시도...")
            conn.datachannel.pub_sub.subscribe(
                "rt/utlidar/voxel_map_compressed",
                lambda message: asyncio.create_task(lidar_callback_task(message))
            )
            print("📡 LIDAR 데이터 구독 시작")
            print(f"📊 구독 토픽: rt/utlidar/voxel_map_compressed")
            
            lidar_connection = conn
            last_message_count = message_count
            
            # 🆕 연결 상태 모니터링 루프
            print("🔄 LIDAR 연결 상태 모니터링 시작...")
            health_check_counter = 0
            
            while lidar_enabled:
                await asyncio.sleep(2)
                health_check_counter += 1
                
                # 주기적으로 연결 상태 확인
                if health_check_counter >= (connection_health_check_interval // 2):
                    health_check_counter = 0
                    
                    # 메시지 수신 확인
                    if message_count == last_message_count:
                        print(f"⚠️ LIDAR 데이터 수신 중단 감지 (메시지 카운트: {message_count})")
                        print("🔄 연결 재시작 중...")
                        break
                    else:
                        print(f"✅ LIDAR 데이터 정상 수신 중 (메시지: {message_count})")
                        last_message_count = message_count
                    
                    # WebRTC 연결 상태 확인
                    if hasattr(conn, '_peer_connection'):
                        connection_state = getattr(conn._peer_connection, 'connectionState', 'unknown')
                        if connection_state in ['closed', 'failed', 'disconnected']:
                            print(f"❌ WebRTC 연결 끊어짐 감지: {connection_state}")
                            print("🔄 연결 재시작 중...")
                            break
            
            # while 루프가 정상 종료된 경우 (lidar_enabled가 False)
            if not lidar_enabled:
                print("🛑 LIDAR 모니터링 종료")
                break
                
        except Exception as e:
            print(f"❌ LIDAR WebRTC 연결 오류: {e}")
            print(f"🔍 상세 오류: {traceback.format_exc()}")
            
            # 연결 실패 시 재시도 대기
            if lidar_enabled:
                print(f"⏳ {retry_delay}초 후 LIDAR 연결 재시도...")
                await asyncio.sleep(retry_delay)
                continue
            else:
                break
    
    print("🏁 LIDAR WebRTC 연결 함수 종료")

def start_lidar_stream():
    """LIDAR 스트림 시작"""
    global lidar_enabled, lidar_task
    
    if lidar_enabled:
        print("⚠️ LIDAR 스트림이 이미 실행 중입니다")
        return False
    
    lidar_enabled = True
    
    def run_lidar():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(lidar_webrtc_connection())
    
    lidar_task = threading.Thread(target=run_lidar, daemon=True)
    lidar_task.start()
    print("🚀 LIDAR 스트림 시작됨")
    return True

def stop_lidar_stream():
    """LIDAR 스트림 중지"""
    global lidar_enabled, lidar_connection
    
    if not lidar_enabled:
        return False
        
    lidar_enabled = False
    
    try:
        if lidar_connection and hasattr(lidar_connection, 'datachannel'):
            lidar_connection.datachannel.pub_sub.publish_without_callback("rt/utlidar/switch", "off")
            print("📡 LIDAR 센서 비활성화됨")
    except Exception as e:
        print(f"⚠️ LIDAR 센서 비활성화 오류: {e}")
    
    print("🛑 LIDAR 스트림 중지됨")
    return True

# 🔄 LIDAR 뷰 토글 라우트
@app.route('/toggle_lidar_view', methods=['POST'])
def toggle_lidar_view():
    """비디오와 LIDAR 뷰 간 전환 - 완전한 연결/해제 방식"""
    global lidar_view_mode
    
    try:
        previous_mode = lidar_view_mode
        lidar_view_mode = not lidar_view_mode
        print(f"🔄 뷰 모드 전환: {previous_mode} → {'LIDAR' if lidar_view_mode else '비디오'}")
        
        if lidar_view_mode:
            # LIDAR 뷰로 전환 시 - 연결 시작
            print("🎯 LIDAR 뷰로 전환 - LIDAR 연결 시작")
            
            if not lidar_enabled:
                print("🚀 LIDAR 스트림 시작")
                if start_lidar_stream():
                    return jsonify({
                        'success': True,
                        'lidar_view_mode': lidar_view_mode,
                        'lidar_enabled': True,
                        'action': 'lidar_connected',
                        'message': 'LIDAR 뷰로 전환 (연결 시작됨)'
                    })
                else:
                    print("❌ LIDAR 스트림 시작 실패")
                    lidar_view_mode = previous_mode  # 실패 시 상태 복원
                    return jsonify({
                        'success': False,
                        'lidar_view_mode': previous_mode,
                        'lidar_enabled': False,
                        'action': 'connection_failed',
                        'error': 'LIDAR 연결 시작 실패'
                    })
            else:
                print("✅ LIDAR 스트림이 이미 실행 중")
                return jsonify({
                    'success': True,
                    'lidar_view_mode': lidar_view_mode,
                    'lidar_enabled': lidar_enabled,
                    'action': 'already_connected',
                    'message': 'LIDAR 뷰로 전환 (이미 연결됨)'
                })
        else:
            # 비디오 뷰로 전환 시 - 연결 완전 해제!
            print("📹 비디오 뷰로 전환 - LIDAR 연결 완전 해제")
            print("� LIDAR 스트림을 완전히 중지하고 리소스를 해제합니다")
            
            if lidar_enabled:
                if stop_lidar_stream():
                    print("✅ LIDAR 연결 완전 해제 성공")
                    return jsonify({
                        'success': True,
                        'lidar_view_mode': lidar_view_mode,
                        'lidar_enabled': False,
                        'action': 'lidar_disconnected',
                        'message': '비디오 뷰로 전환 (LIDAR 연결 해제됨)'
                    })
                else:
                    print("⚠️ LIDAR 연결 해제 실패")
                    return jsonify({
                        'success': True,
                        'lidar_view_mode': lidar_view_mode,
                        'lidar_enabled': lidar_enabled,
                        'action': 'disconnect_warning',
                        'message': '비디오 뷰로 전환 (LIDAR 해제 실패 - 백그라운드에서 계속 실행)'
                    })
            else:
                print("ℹ️ LIDAR가 이미 비활성화된 상태")
                return jsonify({
                    'success': True,
                    'lidar_view_mode': lidar_view_mode,
                    'lidar_enabled': False,
                    'action': 'already_disconnected',
                    'message': '비디오 뷰로 전환 (LIDAR 이미 해제됨)'
                })
        
    except Exception as e:
        print(f"❌ 뷰 토글 오류: {e}")
        # 오류 시 상태 복원
        lidar_view_mode = previous_mode
        return jsonify({
            'success': False, 
            'lidar_view_mode': previous_mode,
            'action': 'error',
            'error': str(e)
        })

# 🆕 LIDAR 제어 라우트들
@app.route('/start_lidar', methods=['POST'])
def start_lidar():
    """LIDAR 스트림 시작"""
    try:
        print("🚀 LIDAR 스트림 수동 시작 요청")
        if start_lidar_stream():
            return jsonify({'success': True, 'message': 'LIDAR 스트림이 시작되었습니다'})
        else:
            # 이미 실행 중이라면 상태 확인 후 재시작
            print("⚠️ LIDAR 스트림이 이미 실행 중 - 상태 확인 중...")
            global lidar_enabled, message_count
            
            # 메시지 카운트가 증가하지 않으면 재시작
            old_count = message_count
            import time
            time.sleep(3)
            
            if message_count == old_count:
                print("🔄 LIDAR 데이터 수신이 중단됨 - 재시작 중...")
                stop_lidar_stream()
                time.sleep(1)
                start_lidar_stream()
                return jsonify({'success': True, 'message': 'LIDAR 스트림이 재시작되었습니다'})
            else:
                return jsonify({'success': True, 'message': 'LIDAR 스트림이 정상 작동 중입니다'})
    except Exception as e:
        print(f"❌ LIDAR 시작 오류: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/stop_lidar', methods=['POST'])
def stop_lidar():
    """LIDAR 스트림 중지"""
    try:
        if stop_lidar_stream():
            return jsonify({'success': True, 'message': 'LIDAR 스트림이 중지되었습니다'})
        else:
            return jsonify({'success': False, 'message': 'LIDAR 스트림이 실행되지 않았습니다'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/lidar_status', methods=['GET'])
def lidar_status():
    """LIDAR 상태 확인"""
    global message_count, lidar_connection
    
    # 연결 상태 확인
    connection_state = 'unknown'
    if lidar_connection and hasattr(lidar_connection, '_peer_connection'):
        connection_state = getattr(lidar_connection._peer_connection, 'connectionState', 'unknown')
    
    return jsonify({
        'lidar_enabled': lidar_enabled,
        'lidar_view_mode': lidar_view_mode,
        'message_count': message_count,
        'connection_state': connection_state,
        'connection_healthy': connection_state in ['connected', 'connecting']
    })

@app.route('/restart_lidar', methods=['POST'])
def restart_lidar():
    """LIDAR 연결 강제 재시작"""
    try:
        print("🔄 LIDAR 연결 강제 재시작 요청")
        
        # 기존 연결 중지
        if lidar_enabled:
            print("🛑 기존 LIDAR 스트림 중지 중...")
            stop_lidar_stream()
            import time
            time.sleep(2)  # 완전히 중지되도록 대기
        
        # 새로운 연결 시작
        print("🚀 새로운 LIDAR 스트림 시작...")
        if start_lidar_stream():
            return jsonify({
                'success': True, 
                'message': 'LIDAR 연결이 재시작되었습니다',
                'restart_time': datetime.now().isoformat()
            })
        else:
            return jsonify({
                'success': False, 
                'message': 'LIDAR 재시작 실패'
            })
            
    except Exception as e:
        print(f"❌ LIDAR 재시작 오류: {e}")
        return jsonify({
            'success': False, 
            'error': str(e)
        })

# 🆕 SocketIO 이벤트 핸들러들
@socketio.on('connect')
def handle_connect():
    print('🔌 클라이언트 연결됨')
    emit('status', {'message': '서버에 연결되었습니다'})

@socketio.on('disconnect')
def handle_disconnect():
    print('🔌 클라이언트 연결 해제됨')

@socketio.on('check_args')
def handle_check_args():
    """LIDAR 뷰어 설정 전송"""
    typeFlag = 0b0101  # point cloud + iso camera
    typeFlagBinary = format(typeFlag, "04b")
    emit("check_args_ack", {"type": typeFlagBinary})

if __name__ == '__main__':
    print("🚀 웹 비디오 서버 시작")
    print("📊 LIDAR 3D 시각화 포함")
    print("🎮 조이스틱 제어 활성화")
    print("🔥 YOLO 화재/인물 탐지 활성화")
    
    try:
        # SocketIO 서버 실행 (기존 Flask 대신)
        socketio.run(app, host='0.0.0.0', port=5100, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n🛑 서버 종료")
        if lidar_enabled:
            stop_lidar_stream()
    except Exception as e:
        print(f"❌ 서버 실행 오류: {e}")
        if lidar_enabled:
            stop_lidar_stream()