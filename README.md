# stock-monitor

남는 갤럭시 스마트폰을 홈 서버로 사용하는 개인용 실시간 주가 대시보드입니다.

- 대상: 한국·미국 주식 및 ETF, 관심 종목 최대 20개
- 시세: Toss Securities Open API의 REST 초기값 + WebSocket 실시간 체결
- 접속: 같은 집 Wi-Fi의 브라우저에서만 사용
- 실행: Termux 네이티브 Python 서버, SQLite
- AI 요약: 2단계 기능이며 Mac과 Codex가 켜져 있을 때만 매시간 실행

현재 상태는 **설계 검토 단계**입니다. 구현은 계획 승인 후 시작합니다.

구체적인 범위와 구조는 [프로젝트 계획](docs/PROJECT_PLAN.md)을 참고하세요.

개발·리뷰·배포 규칙은 [개발 및 브랜치 전략](docs/DEVELOPMENT.md)을 따릅니다.

