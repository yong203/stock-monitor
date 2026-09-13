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

실시간 서비스는 토큰과 Toss WebSocket 연결을 프로세스 안에서 하나만 유지하므로 Uvicorn worker를 늘리지 않는다. 네트워크가 끊기면 마지막 가격을 유지한 채 자동 재연결한다. `/health/market-data`는 비밀값 없이 시세 연결과 시장 캘린더의 `calendar_status`를 함께 보여준다.

## 상시 서비스 설치

`termux-services`의 runit으로 웹 서버와 일일 백업을 각각 감독한다. 처음 전환할 때는 서비스 파일부터 비활성 상태로 설치한다.

```sh
cd "$HOME/stock-monitor"
./scripts/install-termux-services
```

기존에 수동 실행한 Uvicorn이 있으면 `ps`에서 명령행과 PID를 확인한 뒤 그 PID만 종료한다. 광범위한 `pkill`은 사용하지 않는다.

```sh
ps -A -o pid,ppid,args | awk '$3 ~ /(^|\/)uvicorn$/ {print}'
kill <확인한-PID>
./scripts/install-termux-services --enable
```

이후 같은 설치 스크립트를 다시 실행하면 기존 활성 상태를 보존하면서 서비스를 재시작한다. 상태와 로그는 다음처럼 확인한다.

```sh
sv status stock-monitor stock-monitor-backup
tail -f "$PREFIX/var/log/sv/stock-monitor/current"
```

앱이 비정상 종료되면 runit이 다시 시작한다. 로그는 서비스별로 1MB 또는 하루마다 순환하며 과거 파일 5개를 보관한다.

## 데이터베이스 백업

백업 서비스는 시작 직후 SQLite 온라인 백업을 만들고 이후 24시간마다 반복한다. 실패하면 5분 뒤 다시 시도한다. 백업은 기본 7개를 보관하고 각 파일 권한은 `600`이다.

```sh
"$HOME/stock-monitor/.venv/bin/stock-monitor" backup-db
ls -la "$HOME/.local/share/stock-monitor/backups"
```

Toss 자격 증명이나 실행 중인 서버를 중단하지 않고 백업할 수 있다. 복원은 서비스를 멈추고 현재 DB를 별도 보관한 뒤 검증된 백업 파일로 교체하는 운영 작업이므로 자동화하지 않는다.

## 스마트폰 재부팅 자동 시작

설치 스크립트는 `$HOME/.termux/boot/10-stock-monitor`도 준비한다. `termux-info`의 `TERMUX_VERSION`으로 설치 계열을 확인한다.

- **Google Play판:** 2024.10.24부터 [부팅 기능이 Termux 본 앱에 통합](https://github.com/termux-play-store)되었다. 별도 Termux:Boot를 설치하지 않는다.
- **F-Droid/GitHub판:** [공식 안내](https://github.com/termux/termux-boot/blob/master/README.md)에 따라 Termux와 같은 배포처의 Termux:Boot를 설치하고 앱 아이콘을 한 번 실행한다. 서로 다른 배포처의 앱을 섞지 않는다.

Samsung 설정에서 Google Play판은 Termux를, 별도 Boot 앱을 사용하는 계열은 두 앱 모두 배터리 `제한 없음`으로 지정한다. 그다음 스마트폰을 재부팅하고 잠금 해제 후 아래를 확인한다.

```sh
sv status stock-monitor stock-monitor-backup
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/market-data
```

현재 운영 폰은 Google Play판 `googleplay.2025.10.05`이므로 별도 앱 없이 재부팅 검증한다.
