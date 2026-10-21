# agy-router

**Antigravity Multi-Key Router** — 공식명 `agy-router`, 애칭 **치이 (ちい / Chii)**.

여러 Antigravity API 키를 풀(pool)로 관리하며 요청을 알맞은 키로 넘겨주는 로컬 라우터.
쉽게 말해 "열쇠지기" — 그래서 애칭이 치이.

## 이름 규칙

- 공식명: `agy-router` (CLI prog, 저장소, 문서에서 항상 이 이름)
- 애칭: `치이` / `ちい` / `Chii` (사람이 부를 때만. 코드 식별자로 쓰지 않음)

## 주요 기능

- 멀티 키 풀: 로테이션, 쿨다운, 429/401 시 자동 롤오버
- 스티키 바인딩: 프로젝트/세션별 활성 키 고정
- 워크스페이스 동기화, 스냅샷, Git 체크포인트
- 관측성: `health`, `diagnostics` CLI

## 키 등록

`~/.bashrc`에 `export AGY_KEY_N="AIza..."` (N=1~10).
우선순위: `AGY_KEY_N`(env) > `GEMINI_API_KEY_N`(env) > bashrc.

## 사용

```bash
python3 cli.py --help
python3 cli.py health
python3 cli.py test-agent --key-index 2
```

설정: `~/.agy-router/config.json` (CLI > ENV > FILE > DEFAULT)
