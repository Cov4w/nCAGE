import os
import platform
from pathlib import Path
from dotenv import load_dotenv

# 현재 운영체제 감지
current_os = platform.system()

# 현재 파일의 부모 디렉터리 (프로젝트 루트) 구하기
# config/settings.py -> config -> cage_unitree -> src -> project_root
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

# 운영체제별로 .env 파일 경로 설정
if current_os == "Windows":
    # Windows에서의 경로 처리
    ENV_PATH = PROJECT_ROOT / '.env'
    load_dotenv(dotenv_path=str(ENV_PATH))
    print(f"Windows 환경에서 .env 파일 로드: {ENV_PATH}")
    
elif current_os == "Darwin":  # macOS
    # macOS에서의 경로 처리
    ENV_PATH = PROJECT_ROOT / '.env'
    # macOS에서는 절대 경로로 변환하여 로드
    absolute_env_path = ENV_PATH.resolve()
    load_dotenv(dotenv_path=str(absolute_env_path))
    print(f"macOS 환경에서 .env 파일 로드: {absolute_env_path}")
    
else:  # Linux 및 기타 Unix 계열
    # Linux에서의 경로 처리
    ENV_PATH = PROJECT_ROOT / '.env'
    # Unix 계열에서는 홈 디렉터리 기준으로도 확인
    if not ENV_PATH.exists():
        # 홈 디렉터리에서 .env 파일 찾기
        home_env_path = Path.home() / f"{PROJECT_ROOT.name}" / '.env'
        if home_env_path.exists():
            ENV_PATH = home_env_path
    
    absolute_env_path = ENV_PATH.resolve()
    load_dotenv(dotenv_path=str(absolute_env_path))
    print(f"Linux/Unix 환경에서 .env 파일 로드: {absolute_env_path}")

# 환경변수 로드 확인
print(f"현재 운영체제: {current_os}")
print(f".env 파일 경로: {ENV_PATH}")
print(f".env 파일 존재 여부: {ENV_PATH.exists()}")

SERIAL_NUMBER = os.getenv("SERIAL_NUMBER")
UNITREE_USERNAME = os.getenv("UNITREE_USERNAME")
UNITREE_PASSWORD = os.getenv("UNITREE_PASSWORD")