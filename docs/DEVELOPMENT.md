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
- Python 골격은 `.python-version`과 `[dev]` 의존성을 함께 정의해 로컬과 CI가 같은 검사를 실행하게 함
- 긴급 수정도 `fix/*` PR을 사용하며 `main`에 직접 반영하지 않음

## 버전과 배포

- Semantic Versioning 사용: 초기 개발은 `v0.x.y`, 첫 안정판은 `v1.0.0`
- 배포 후보 커밋에 annotated tag를 생성하고 GitHub Release 기록
- 스마트폰에는 태그가 가리키는 커밋만 배포
- 배포 전에 DB 백업 및 상태 확인, 배포 뒤 API·화면·Toss 연결 점검
- 실패하면 직전 정상 태그를 다시 배포. `main` 기록을 강제로 되돌리지 않음
- DB 마이그레이션은 가능한 한 한 버전 이상 하위 호환되게 작성

## 저장소 보호 정책

- GitHub 병합 방식은 squash merge만 허용하고 병합된 작업 브랜치는 자동 삭제
- 현재 GitHub 요금제는 비공개 저장소의 서버 측 branch protection을 지원하지 않음
- 이를 보완해 저장소의 `pre-push` hook으로 이 Mac에서 `main` 직접 push를 차단
- 새 개발 환경에서는 `git config core.hooksPath .githooks`를 한 번 실행
- GitHub Pro로 전환하면 PR 필수, force push·삭제 금지, 대화 해결 필수를 서버에서도 적용
- CI가 추가되면 테스트·lint·비밀값 검사를 필수 상태 검사로 지정

로컬 hook은 `--no-verify`로 우회할 수 있으므로 최종 보호 수단이 아니라 실수 방지 장치다. 요금제 변경 전까지 PR 사용은 프로젝트 운영 규칙으로 강제한다.
