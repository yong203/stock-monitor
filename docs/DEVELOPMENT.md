# 개발 및 브랜치 전략

## 원칙

이 프로젝트는 1인 개발과 스마트폰 단일 운영 환경에 맞춘 가벼운 trunk-based/GitHub Flow를 사용한다.

- `main`은 항상 배포 가능한 상태로 유지
- 직접 push하지 않고 짧은 작업 브랜치와 Pull Request 사용
- PR은 검증 완료 후 squash merge
- 실제 스마트폰 배포는 `main`의 버전 태그에서만 수행
- 장기 `develop`·`release` 브랜치는 만들지 않음

## 브랜치

| 패턴 | 용도 | 예시 |
|---|---|---|
| `feat/*` | 사용자 기능 | `feat/live-price-cards` |
| `fix/*` | 버그 및 운영 장애 수정 | `fix/toss-reconnect` |
| `refactor/*` | 동작 변경 없는 구조 개선 | `refactor/token-manager` |
| `test/*` | 테스트만 변경 | `test/market-session` |
| `docs/*` | 문서만 변경 | `docs/termux-runbook` |
| `chore/*` | 도구·의존성·CI·운영 설정 | `chore/github-actions` |

모든 작업 브랜치는 최신 `main`에서 만들고 병합 후 삭제한다. 한 브랜치에는 한 가지 목적만 담는다.

## 작업 흐름

```text
main → 작업 브랜치 → 작은 커밋 → 검증 → PR → squash merge → main
                                                          ↓
                                                  버전 태그 → 스마트폰 배포
```

1. 이슈 또는 구현 목표와 완료 조건을 먼저 정함
2. 최신 `main`에서 작업 브랜치 생성
3. Conventional Commits 형식으로 커밋
4. 테스트·정적 검사·비밀값 검사를 통과시킨 뒤 PR 생성
5. PR에서 범위, 검증 결과, 배포 영향, DB 변경 여부 확인
6. squash merge 후 원격·로컬 작업 브랜치 삭제

## 커밋과 PR

- 커밋 형식: `<type>: <짧은 설명>`
- 주요 type: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- PR은 가능한 작게 유지하고 기능과 무관한 정리를 섞지 않음
- 병합 조건: 필수 검증 통과, 미해결 대화 없음, 비밀값 미포함
- 긴급 수정도 `fix/*` PR을 사용하며 `main`에 직접 반영하지 않음

## 버전과 배포

- Semantic Versioning 사용: 초기 개발은 `v0.x.y`, 첫 안정판은 `v1.0.0`
- 배포 후보 커밋에 annotated tag를 생성하고 GitHub Release 기록
- 스마트폰에는 태그가 가리키는 커밋만 배포
- 배포 전에 DB 백업 및 상태 확인, 배포 뒤 API·화면·Toss 연결 점검
- 실패하면 직전 정상 태그를 다시 배포. `main` 기록을 강제로 되돌리지 않음
- DB 마이그레이션은 가능한 한 한 버전 이상 하위 호환되게 작성

## GitHub 보호 정책

- `main` 변경은 Pull Request만 허용
- force push와 브랜치 삭제 금지
- 대화가 모두 해결되어야 병합 가능
- CI가 추가되면 테스트·lint·비밀값 검사를 필수 상태 검사로 지정
- 관리자 우회는 운영 장애 복구용으로만 사용

