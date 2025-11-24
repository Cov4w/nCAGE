import asyncio
import threading
import time
import logging
import json
import av
import os
from multiprocessing import Queue
from cage_unitree.go2_webrtc_connect.go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod
from cage_unitree.go2_webrtc_connect.go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD
from aiortc import MediaStreamTrack
from cage_unitree.go2_webrtc_connect.go2_webrtc_driver.util import TokenManager


# 디버깅 
# from pathlib import Path
# print("=== 환경 디버깅 ===")
# print(f"현재 작업 디렉터리: {os.getcwd()}")
# print(f"스크립트 위치: {__file__}")
# print(f"프로젝트 루트: {Path(__file__).parent}")
# 디버깅


from cage_unitree.config.settings import SERIAL_NUMBER, UNITREE_USERNAME, UNITREE_PASSWORD

# 디버깅
# print("=== 환경변수 확인 ===")
# print(f"SERIAL_NUMBER: {SERIAL_NUMBER}")
# print(f"UNITREE_USERNAME: {UNITREE_USERNAME}")
# print(f"UNITREE_PASSWORD: {'설정됨' if UNITREE_PASSWORD else 'None'}")
# 디버깅

logging.basicConfig(level=logging.FATAL)
logging.basicConfig(level=logging.INFO)

_conn_holder = {}

# 최신 조이스틱 값만 저장 (sitdown/situp 등은 큐 사용)
latest_joystick = None
robot_state = "unknown"  # 초기 상태는 unknown
robot_state_history = []  # 🆕 상태 이력 추적
robot_state_lock = threading.Lock()  # 🆕 상태 변경 동기화
latest_bms_state = None  # BMS 상태 저장용

def start_webrtc(frame_queue, command_queue):
    av.logging.set_level(av.logging.ERROR) 
    # av.logging.set_level(av.logging.DEBUG) # 로깅을 디버그 레벨로 변경

    async def recv_camera_stream(track: MediaStreamTrack):
        while True:
            try:
                frame = await track.recv()
                img = frame.to_ndarray(format="bgr24")
                frame_queue.put(img)
            except Exception as e:
                logging.error(f"Frame decode error: {e}")

    # BMS 상태를 수신하는 콜백 함수 수정
    def lowstate_callback(message):
        global latest_bms_state
        try:
            current_message = message['data']
            latest_bms_state = current_message['bms_state']
            print(f"[BMS 업데이트] SOC: {latest_bms_state['soc']}%, 전류: {latest_bms_state['current']}mA")
            
            # 파일로 BMS 상태 저장 (현재 로봇 상태 포함)
            bms_file = os.path.join(os.getcwd(), '.bms_state.json')
            with open(bms_file, 'w') as f:
                json.dump({
                    'timestamp': time.time(),
                    'bms_state': latest_bms_state,
                    'robot_state': get_robot_state_safe(),  # 🆕 동기화된 상태 사용
                    'connection_status': 'connected'
                }, f)
                
        except Exception as e:
            print(f"[BMS] 상태 파싱 오류: {e}")

    async def _ensure_normal_mode(conn):
        try:
            response = await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
            )
            if response['data']['header']['status']['code'] == 0:
                data = json.loads(response['data']['data'])
                current_motion_switcher_mode = data['name']
                print(f"[모드 확인] 현재 모드: {current_motion_switcher_mode}")
            else:
                print("[모드 확인] 현재 모드 조회 실패")

            if current_motion_switcher_mode != "normal":
                print(f"[모드 전환] {current_motion_switcher_mode} → normal 모드로 변경 시도")
                response2 = await conn.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC["MOTION_SWITCHER"],
                    {"api_id": 1002, "parameter": {"name": "normal"}}
                )
                await asyncio.sleep(5)
                response3 = await conn.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
                )
                if response3['data']['header']['status']['code'] == 0:
                    data = json.loads(response3['data']['data'])
                    print(f"[모드 확인] 변경 후 모드: {data['name']}")
                else:
                    print("[모드 확인] 변경 후 모드 조회 실패")
        except Exception as e:
            print(f"[모드 확인] 에러 발생: {e}")

    async def handle_command(conn):
        global latest_joystick
        while True:
            # sitdown/situp 등은 큐에서 처리
            if not command_queue.empty():
                direction = command_queue.get()
                
                if direction == "sitdown":
                    print("Performing 'StandDown' movement...")
                    await conn.datachannel.pub_sub.publish_request_new(
                        RTC_TOPIC["SPORT_MOD"],
                        {"api_id": SPORT_CMD["StandDown"]}
                    )
                    update_robot_state("sitdown", "sitdown_command")
                    
                elif direction == "situp":
                    print("Performing 'StandUp' movement...")
                    await conn.datachannel.pub_sub.publish_request_new(
                        RTC_TOPIC["SPORT_MOD"],
                        {"api_id": SPORT_CMD["StandUp"]}
                    )
                    print("Performing 'BalanceStand' movement...")
                    await conn.datachannel.pub_sub.publish_request_new(
                        RTC_TOPIC["SPORT_MOD"],
                        {"api_id": SPORT_CMD["BalanceStand"]}
                    )
                    update_robot_state("situp", "situp_command")
                    
                elif direction == "sit":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'Sit' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Sit"]}
                        )
                        update_robot_state("sit", "sit_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")
                        
                        print("Performing 'Sit' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Sit"]}
                        )
                        update_robot_state("sit", "sit_command")
                        
                # 🆕 standup 명령 추가 (ArUco 복구용)
                elif direction == "standup":
                    current_state = get_robot_state_safe()
                    print(f"Performing 'StandUp' recovery from {current_state}...")
                    
                    # sit 또는 sitdown에서 standup으로 복구
                    if current_state in ["sit", "sitdown"]:
                        # StandUp → BalanceStand 시퀀스
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        print("✅ StandUp 명령 완료")
                        
                        # 0.5초 대기 후 BalanceStand
                        await asyncio.sleep(0.5)
                        
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        print("✅ BalanceStand 명령 완료")
                        
                        update_robot_state("situp", "aruco_recovery")
                    else:
                        # 이미 서있는 상태라면 BalanceStand만
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        print("✅ BalanceStand 명령 완료 (이미 서있는 상태)")
                        update_robot_state("situp", "balance_adjustment")
                elif direction == "dance1":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'Dance1' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Dance1"]}
                        )
                        update_robot_state("dance1", "dance1_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")

                        print("Performing 'Dance1' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Dance1"]}
                        )
                        update_robot_state("dance1", "dance1_command")
                elif direction == "dance2":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'Dance2' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Dance2"]}
                        )
                        update_robot_state("dance2", "dance2_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")

                        print("Performing 'Dance2' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Dance2"]}
                        )
                        update_robot_state("dance2", "dance2_command")
                elif direction == "jump":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'JumpFront' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["FrontJump"]}
                        )
                        update_robot_state("jump", "jump_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")

                        print("Performing 'JumpFront' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["FrontJump"]}
                        )
                        update_robot_state("jump", "jump_command")
                elif direction == "heart":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'FingerHeart' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["FingerHeart"]}
                        )
                        update_robot_state("heart", "heart_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")

                        print("Performing 'FingerHeart' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["FingerHeart"]}
                        )
                        update_robot_state("heart", "heart_command")
                elif direction == "hello":
                    current_state = get_robot_state_safe()
                    if current_state == "situp":
                        print("Performing 'Hello' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Hello"]}
                        )
                        update_robot_state("hello", "hello_command")
                    else:
                        print("Not situp, switching to situp first...")
                        # StandUp → BalanceStand → Sit
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["StandUp"]}
                        )
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["BalanceStand"]}
                        )
                        update_robot_state("situp", "auto_situp_for_sit")

                        print("Performing 'Hello' movement...")
                        await conn.datachannel.pub_sub.publish_request_new(
                            RTC_TOPIC["SPORT_MOD"],
                            {"api_id": SPORT_CMD["Hello"]}
                        )
                        update_robot_state("hello", "hello_command")
                
                # 기타 명령은 필요시 추가
            
            # 최신 조이스틱 값만 사용
            if latest_joystick is not None:
                _, x, z = latest_joystick
                print(f"Joystick command (latest): x={x}, z={z}")
                response = await conn.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC["SPORT_MOD"],
                    {"api_id": SPORT_CMD["Move"], "parameter": {"x": float(x), "y": 0, "z": float(z)}}
                )
                print("Move response:", response)
                
            await asyncio.sleep(0.1)  # 100ms마다 체크

    async def main_webrtc():
        global _conn_holder
        token_manager = TokenManager()
        token = token_manager.get_token()
        conn = Go2WebRTCConnection(
            WebRTCConnectionMethod.Remote,
            serialNumber=SERIAL_NUMBER,
            username=UNITREE_USERNAME,
            password=UNITREE_PASSWORD
        )
        
        # 연결 저장
        _conn_holder['conn'] = conn
        
        # 연결 수행
        await conn.connect()
        
        # 🔧 연결 후 채널 상태 확인 및 초기화
        print("🔍 WebRTC 연결 후 채널 상태 확인...")
        
        # 비디오 채널 활성화
        if hasattr(conn, 'video') and conn.video:
            conn.video.switchVideoChannel(True)
            conn.video.add_track_callback(recv_camera_stream)
            print("✅ 비디오 채널 활성화됨")
        else:
            print("❌ 비디오 채널 없음")
        
        # 🆕 오디오 채널 확인 및 활성화 시도
        if hasattr(conn, 'audio') and conn.audio:
            try:
                # 오디오 채널 활성화 시도
                if hasattr(conn.audio, 'switchAudioChannel'):
                    conn.audio.switchAudioChannel(True)
                    print("✅ 오디오 채널 활성화됨")
                else:
                    print("⚠️ 오디오 채널에 switchAudioChannel 메서드 없음")
            except Exception as e:
                print(f"⚠️ 오디오 채널 활성화 실패: {e}")
        else:
            print("❌ 오디오 채널 없음")
        
        # 🆕 데이터채널 확인
        if hasattr(conn, 'datachannel') and conn.datachannel:
            print("✅ 데이터채널 확인됨")
            
            # pub_sub 시스템 확인
            if hasattr(conn.datachannel, 'pub_sub') and conn.datachannel.pub_sub:
                print("✅ pub_sub 시스템 확인됨")
            else:
                print("❌ pub_sub 시스템 없음")
        else:
            print("❌ 데이터채널 없음")
            print(f"🔍 conn 속성들: {[attr for attr in dir(conn) if not attr.startswith('_')]}")
        
        # 🆕 연결 상태 파일에 저장 (채널 확인 후)
        save_webrtc_connection_status()
        
        # BMS 상태 구독 추가 (데이터채널이 있는 경우에만)
        if hasattr(conn, 'datachannel') and conn.datachannel and hasattr(conn.datachannel, 'pub_sub'):
            print("[BMS] LOW_STATE 구독 시작...")
            conn.datachannel.pub_sub.subscribe(RTC_TOPIC['LOW_STATE'], lowstate_callback)
        else:
            print("[BMS] 데이터채널이 없어서 BMS 구독 건너뜀")
        
        await _ensure_normal_mode(conn)
        
        # 명령 핸들러는 데이터채널이 있는 경우에만 시작
        if hasattr(conn, 'datachannel') and conn.datachannel:
            asyncio.create_task(handle_command(conn))
        else:
            print("⚠️ 데이터채널이 없어서 명령 핸들러 시작하지 않음")
        
        # 주기적으로 상태 업데이트
        async def update_status_periodically():
            while True:
                await asyncio.sleep(30)  # 30초마다 상태 업데이트
                save_webrtc_connection_status()
        
        asyncio.create_task(update_status_periodically())
        
        # 루프가 살아있도록 대기
        while True:
            await asyncio.sleep(1)

    # 메인 루프를 별도 스레드에서 실행
    def run_loop():
        asyncio.run(main_webrtc())

    threading.Thread(target=run_loop, daemon=True).start()

# BMS 상태를 외부에서 가져오는 함수들 추가
def get_bms_state():
    """현재 BMS 상태를 파일에서 읽어서 반환"""
    try:
        bms_file = os.path.join(os.getcwd(), '.bms_state.json')
        if os.path.exists(bms_file):
            with open(bms_file, 'r') as f:
                data = json.load(f)
                # 5분 이내 데이터만 유효
                if time.time() - data['timestamp'] < 300:
                    return data['bms_state']
        return None
    except Exception as e:
        print(f"BMS 상태 파일 읽기 오류: {e}")
        return None

def get_robot_state_safe():
    """동기화된 로봇 상태 조회"""
    with robot_state_lock:
        return robot_state

def update_robot_state(new_state, reason="command"):
    """로봇 상태 업데이트 (동기화 및 이력 추가)"""
    global robot_state, robot_state_history
    
    with robot_state_lock:
        old_state = robot_state
        robot_state = new_state
        
        # 상태 이력 추가
        robot_state_history.append({
            'timestamp': time.time(),
            'old_state': old_state,
            'new_state': new_state,
            'reason': reason
        })
        
        # 이력은 최근 10개만 유지
        if len(robot_state_history) > 10:
            robot_state_history = robot_state_history[-10:]
        
        print(f"🤖 로봇 상태 변경: {old_state} → {new_state} (이유: {reason})")

def get_robot_status():
    """로봇의 전체 상태를 파일에서 읽어서 반환 - 개선됨"""
    try:
        current_state = get_robot_state_safe()
        
        bms_file = os.path.join(os.getcwd(), '.bms_state.json')
        bms_data = None
        connection_status = 'disconnected'
        
        if os.path.exists(bms_file):
            with open(bms_file, 'r') as f:
                data = json.load(f)
                # 5분 이내 데이터만 유효
                if time.time() - data['timestamp'] < 300:
                    bms_data = data.get('bms_state')
                    connection_status = data.get('connection_status', 'disconnected')
        
        return {
            'robot_state': current_state,
            'bms_state': bms_data,
            'connection_status': connection_status,
            'state_history': robot_state_history[-5:] if robot_state_history else []  # 최근 5개 이력
        }
    except Exception as e:
        print(f"로봇 상태 파일 읽기 오류: {e}")
        return {
            'robot_state': get_robot_state_safe(),
            'bms_state': None,
            'connection_status': 'disconnected',
            'state_history': []
        }


async def get_robot_bms_status():
    """Discord 봇에서 사용할 BMS 상태 가져오기 (비동기)"""
    # 연결이 되어 있다면 최신 BMS 상태 반환
    if latest_bms_state is not None:
        return latest_bms_state
    
    # 연결이 안되어 있거나 BMS 데이터가 없다면 잠시 대기
    max_wait = 10  # 최대 10초 대기
    for _ in range(max_wait):
        await asyncio.sleep(1)
        if latest_bms_state is not None:
            return latest_bms_state
    
    return None

# 외부에서 명령을 큐에 넣는 함수
def send_command(command_queue, direction):
    global latest_joystick
    if isinstance(direction, tuple) and direction[0] == 'joystick':
        latest_joystick = direction  # 최신 값으로 덮어쓰기
    else:
        command_queue.put(direction)  # sitdown, situp 등은 기존 큐 사용

# 외부에서 normal 모드 전환을 요청할 때 호출
def ensure_normal_mode_once():
    import asyncio
    conn = _conn_holder.get('conn')
    if conn is None:
        print("No connection yet.")
        return False
    async def switch():
        await asyncio.sleep(1)  # 연결이 완전히 될 때까지 잠깐 대기
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
        )
        response = await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
        )
        current_motion_switcher_mode = "normal"
        if response['data']['header']['status']['code'] == 0:
            data = json.loads(response['data']['data'])
            current_motion_switcher_mode = data['name']
        if current_motion_switcher_mode != "normal":
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"],
                {"api_id": 1002, "parameter": {"name": "normal"}}
            )
            await asyncio.sleep(10)
    threading.Thread(target=lambda: asyncio.run(switch()), daemon=True).start()
    return True

def save_webrtc_connection_status():
    """WebRTC 연결 상태를 파일에 저장 - 개선됨"""
    try:
        conn = _conn_holder.get('conn')
        
        # 더 정확한 상태 확인
        connected = conn is not None
        has_datachannel = False
        has_audio = False
        has_video = False
        connection_state = "unknown"
        
        if conn:
            # PeerConnection 상태 확인
            if hasattr(conn, 'pc') and conn.pc:
                connection_state = getattr(conn.pc, 'connectionState', 'unknown')
                print(f"🔍 PeerConnection 상태: {connection_state}")
            
            # 데이터채널 확인
            if hasattr(conn, 'datachannel') and conn.datachannel is not None:
                has_datachannel = True
                print(f"✅ 데이터채널 확인됨: {type(conn.datachannel)}")
            else:
                print(f"❌ 데이터채널 없음: datachannel={getattr(conn, 'datachannel', 'None')}")
            
            # 오디오채널 확인
            if hasattr(conn, 'audio') and conn.audio is not None:
                has_audio = True
                print(f"✅ 오디오채널 확인됨: {type(conn.audio)}")
            else:
                print(f"❌ 오디오채널 없음: audio={getattr(conn, 'audio', 'None')}")
            
            # 비디오채널 확인
            if hasattr(conn, 'video') and conn.video is not None:
                has_video = True
                print(f"✅ 비디오채널 확인됨: {type(conn.video)}")
        
        status_data = {
            'connected': connected,
            'has_datachannel': has_datachannel,
            'has_audio': has_audio,
            'has_video': has_video,
            'connection_state': connection_state,
            'connection_time': time.time(),
            'serial_number': SERIAL_NUMBER,
            'ready_for_voice_bridge': connected and connection_state == 'connected',
            'process_id': os.getpid(),  # 🆕 프로세스 ID 추가
            'connection_holder_status': 'active' if _conn_holder.get('conn') else 'empty'
        }
        
        with open('.webrtc_connection_status.json', 'w') as f:
            json.dump(status_data, f)
            
        print(f"📝 WebRTC 연결 상태 저장 (PID: {os.getpid()}):")
        print(f"   연결: {'✅' if connected else '❌'}")
        print(f"   PeerConnection: {connection_state}")
        print(f"   데이터채널: {'✅' if has_datachannel else '❌'}")
        print(f"   오디오채널: {'✅' if has_audio else '❌'}")
        print(f"   비디오채널: {'✅' if has_video else '❌'}")
        print(f"   음성브리지 준비: {'✅' if status_data['ready_for_voice_bridge'] else '❌'}")
        
    except Exception as e:
        print(f"❌ WebRTC 연결 상태 저장 실패: {e}")
        import traceback
        print(f"🔍 상세 오류: {traceback.format_exc()}")

if __name__ == "__main__":
    frame_queue = Queue(maxsize=10)
    command_queue = Queue(maxsize=10)
    start_webrtc(frame_queue, command_queue)

    # 예시: 키보드 입력으로 명령 전달
    while True:
        if not frame_queue.empty():
            img = frame_queue.get()
            print(img.shape)
        else:
            time.sleep(0.01)
        direction = input("Enter direction (sitdown/situp): ")
        send_command(command_queue, direction)
