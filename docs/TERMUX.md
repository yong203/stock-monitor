# Termux 실행

## 배치 경로

- 소스와 가상환경: `$HOME/stock-monitor`
- 비밀값: `$HOME/.config/stock-monitor/credentials.toml`
- 향후 데이터베이스: `$HOME/.local/share/stock-monitor/stock.db`

공유 저장소(`/sdcard`, `$HOME/storage`)에는 소스·비밀값·DB를 두지 않는다.

## Phase 0 준비

Termux에서 다음 패키지만 설치한다.

```sh
pkg update
pkg upgrade
pkg install --no-install-recommends python python-pip python-ensurepip-wheels git termux-services
```

프로젝트 디렉터리에서 가상환경과 의존성을 설치한다.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints.txt -e .
```

## 비밀값 입력

저장소의 예시 파일을 복사한 뒤 스마트폰에서 직접 값을 입력한다. 값은 대화, Git, 로그에 남기지 않는다.

```sh
mkdir -p "$HOME/.config/stock-monitor"
cp config/credentials.toml.example "$HOME/.config/stock-monitor/credentials.toml"
chmod 600 "$HOME/.config/stock-monitor/credentials.toml"
```

파일에는 토스증권 WTS의 `설정 > Open API`에서 발급한 `client_id`, `client_secret` 두 값만 둔다.
같은 화면의 `허용 IP 관리`에는 집 Wi-Fi의 현재 공인 IP를 등록한다. LTE 전환이나 통신사·공유기 환경 변화로 공인 IP가 바뀌면 다시 등록해야 한다.

## 연결 진단

```sh
stock-monitor diagnose-toss
```

성공하면 `toss_connection_ok`와 확인한 심볼 `005930`을 출력한다. 주요 실패 코드는 다음과 같다.

- `credentials_*`: 파일 누락·형식·권한 문제
- `invalid_credentials`: client ID 또는 secret 문제
- `public_ip_not_allowed`: Toss에 현재 공인 IP가 허용되지 않음
- `market_data_forbidden`: 시세 API 권한 또는 Toss edge 차단
- `rate_limited`: API 호출 한도 초과
- `network_error`: DNS·TLS·연결 시간 초과
- `remote_error`: Toss 장애 또는 예상하지 못한 응답

프로세스 종료 코드는 설정 2, 안전하지 않은 설정 3, 인증 10, 공인 IP 11, 호출 한도 12, 네트워크 13, 원격 응답 14, 시세 접근 거부 15다.

진단은 새 액세스 토큰을 발급해 기존 토큰을 무효화한다. Phase 1 서비스가 실행 중일 때는 이 명령을 별도로 실행하지 않는다.

## 상태 확인

최초 설치 또는 종목 목록 갱신 시 Toss에서 활성 종목을 동기화한다. 7개 시장을 호출하므로 약 7초가 걸린다.

```sh
stock-monitor sync-instruments
```

`diagnose-toss`와 `sync-instruments`는 모두 새 토큰을 발급한다. Phase 1B 이후 상시 시세 서비스가 실행 중일 때는 두 CLI를 별도로 실행하지 않고 서비스 내부 갱신만 사용한다.

대시보드 서버를 실행한다.

```sh
uvicorn stock_monitor.app:app --host 0.0.0.0 --port 8000 --workers 1
```

같은 Wi-Fi의 다른 기기에서 다음 주소로 확인한다.

```text
http://<스마트폰의 고정 LAN IP>:8000/health/live
http://<스마트폰의 고정 LAN IP>:8000/health/configuration
http://<스마트폰의 고정 LAN IP>:8000/health/market-data
```

대시보드는 `http://<스마트폰의 고정 LAN IP>:8000`, 관심종목 관리는 `/watchlist`에서 연다.

실시간 서비스는 토큰과 Toss WebSocket 연결을 프로세스 안에서 하나만 유지하므로 Uvicorn worker를 늘리지 않는다. 네트워크가 끊기면 마지막 가격을 유지한 채 자동 재연결하며, `/health/market-data`에서 비밀값 없는 연결 상태를 확인할 수 있다.

자동 시작과 상시 서비스 구성은 실시간 MVP를 배포하는 Phase 1에서 진행한다.
