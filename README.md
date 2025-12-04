# nCAGE: Unitree Go2 WebRTC Control System

## 1. 설치 방법 (Installation)

이 프로젝트는 두 단계의 설치 과정이 필요합니다. 메인 패키지를 설치한 후 `go2_webrtc_connect` 서브 모듈을 먼저 설치해야 합니다.

### 1.1 가상환경 생성 및 활성화

```bash
# 가상환경 생성
python3 -m venv venv

# 가상환경 활성화
source venv/bin/activate
```

### 1.2 nCAGE 패키지 설치

```bash
pip install -e .
```

### 1.3 go2_webrtc_connect 설치 (필수!)

내부 WebRTC 드라이버가 올바르게 작동하려면 해당 디렉토리에서 직접 설치를 진행해야 합니다.

```bash
cd src/cage_unitree/go2_webrtc_connect
pip install -e .
cd ../../..  # 다시 프로젝트 루트로 이동
```


## 2. 실행 방법 (Usage)

설치가 완료되면 다음 명령어로 웹 비디오 서버를 실행할 수 있습니다.

```bash
# 가상환경이 활성화된 상태에서 실행
python3 -m cage_unitree.web_video_server
```

## 3. 환경 변수 설정 (.env)

로봇 연결을 위해 프로젝트 루트에 `.env` 파일을 생성하고 Unitree 계정 정보를 입력해야 합니다.

```bash
# .env 파일 예시
UNITREE_USERNAME=your_email@example.com
UNITREE_PASSWORD=your_password
SERIAL_NUMBER=your_robot_serial_number
```

