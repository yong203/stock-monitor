# stock-monitor

남는 갤럭시 스마트폰을 홈 서버로 사용하는 개인용 실시간 주가 대시보드입니다.

- 대상: 한국·미국 주식 및 ETF, 관심 종목 최대 20개
- 시세: Toss Securities Open API의 REST 초기값 + WebSocket 실시간 체결
- 접속: 같은 집 Wi-Fi의 브라우저에서만 사용
- 실행: Termux 네이티브 Python 서버, SQLite
- AI 보고서: Mac에서 `$stock-report`를 명시적으로 실행할 때 조사·분석 후 스마트폰에 저장

관심종목 실시간 시세, 시장 세션 화면, Termux 서비스·백업과 재부팅 자동 시작까지 Phase 1을 완료했습니다. Phase 2는 Toss 데이터와 공식 공시·뉴스를 종합한 근거 연결형 투자 보고서와 인라인 타임라인을 추가합니다.

구체적인 범위와 구조는 [프로젝트 계획](docs/PROJECT_PLAN.md)을 참고하세요.

개발·리뷰·배포 규칙은 [개발 및 브랜치 전략](docs/DEVELOPMENT.md)을 따릅니다.

AI 개발 에이전트는 루트의 [AGENTS.md](AGENTS.md)를 작업 계약으로 사용합니다.

Termux 설치와 Toss 연결 진단은 [Termux 실행 가이드](docs/TERMUX.md)를 참고하세요.
