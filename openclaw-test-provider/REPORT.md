# Antigravity Test Provider — 작업 결과 보고서

작성: 2026-10-19 (KST) / 에구치 코코로
대상: `antigravity-test` OpenClaw Model Provider (테스트 전용)

---

## 1. 목표

Antigravity(Google Interactions API)를 **OpenClaw의 모델로 등록**하여,
OpenClaw 표준 경로(`provider/model` 지정)로 호출 가능하게 만든다.

## 2. 최종 결과

**성공.** OpenClaw가 `antigravity-test/antigravity-preview-05-2026` 모델을
해석하고, 실제 추론 요청을 Antigravity까지 보내 응답을 받아온다.

실제 실행 증거 (게이트웨이 경로):

```
[provider-transport-fetch] [model-fetch] response
  provider=antigravity-test api=openai-completions
  model=antigravity-preview-05-2026 status=200 elapsedMs=9309
  contentType=text/event-stream; charset=utf-8
model: antigravity-test/antigravity-preview-05-2026
outputs: 1
출력: "게이트웨이 경유 테스트가 정상적으로 완료되었습니다."
```

카탈로그 확인:

```
$ openclaw models list --provider antigravity-test --all
Model                                      Input      Ctx         Local Auth  Tags
antigravity-test/antigravity-preview-05... text       1049k       yes   yes
```

## 3. 아키텍처

```
OpenClaw (Gateway / agent)
   │  provider=antigravity-test, api=openai-completions
   ▼
models.providers["antigravity-test"].baseUrl = http://127.0.0.1:8765/v1
   │
   ▼
adapter/adapter.py  (Python, 로컬 프로세스)
   │  기존 cloud_wrapper.call_antigravity 재사용
   ▼
https://generativelanguage.googleapis.com/v1beta/interactions
```

- **OpenClaw 코어 수정 없음.** 설정 + 플러그인(카탈로그) + 로컬 어댑터만 사용.
- 스트리밍/전송은 OpenClaw 내장 `openai-completions` 트랜스포트가 처리
  → 커스텀 `createStreamFn` 불필요(리스크 최소화).
- 세션 연속성(`environment_id` / `previous_interaction_id`)과
  대화 간 격리는 **어댑터**가 담당한다.

## 4. 생성/수정 파일

| 경로 | 역할 |
| --- | --- |
| `openclaw-test-provider/index.js` | Provider 플러그인 (plain ESM, `catalog` 훅만 등록) |
| `openclaw-test-provider/adapter/adapter.py` | Antigravity → 로컬 OpenAI 호환 어댑터 |
| `openclaw-test-provider/package.json` | 플러그인 패키지 메타 (`openclaw.extensions`) |
| `openclaw-test-provider/openclaw.plugin.json` | 플러그인 매니페스트 |
| `openclaw-test-provider/config/patch.json5` | 적용한 설정 패치(검증 완료) |
| `openclaw-test-provider/index.ts` | 초기 TS 초안 (현재 미사용, 참고용) |

## 5. 어댑터 엔드포인트

- `GET  /v1/models` — OpenAI 호환 모델 목록
- `POST /v1/chat/completions` — OpenAI 호환 채팅 (`stream` 지원, SSE)
- `POST /v1/interactions` — 레거시 직접 호출(검증용)

세션 키: 대화의 **첫 user 메시지 해시** → 같은 대화는 상호작용 이어짐,
다른 대화는 상태가 섞이지 않음.
`ThreadingHTTPServer` 사용(동시 요청 처리).

## 6. 설정 변경 내역 (추가만, 기존 항목 보존)

`~/.openclaw/openclaw.json`:

1. `models.providers["antigravity-test"]` 추가
   - `baseUrl: http://127.0.0.1:8765/v1`
   - `api: openai-completions`, `timeoutSeconds: 300`
   - `apiKey: "***"` (더미 — 실제 키 아님)
   - `models: [{ id: "antigravity-preview-05-2026", ... }]`
2. `agents.defaults.modelPolicy.allow` 에
   `"antigravity-test/antigravity-preview-05-2026"` 문자열 **추가**
   (기존 16개 항목 그대로 유지)

적용: `openclaw config patch --file config/patch.json5`
→ `Applied 6 config update(s). Change will apply without restarting the gateway.`

플러그인 설치: `openclaw plugins install --link --force .`
→ `antigravity-test` registered / enabled.

**변경하지 않은 것:** `agents.defaults.model`, 기존 aliases, fallbacks,
기존 provider(`custom-127-0-0-1-3000`, `google-interactions`), 채널, 기존 플러그인.

## 7. 보안 처리

- 제공받은 API 키는 **어댑터 프로세스 환경변수(`AGY_KEY_1`)로만** 전달.
- 소스 코드 / 로그 / 테스트 결과 / 이 문서 어디에도 키 값을 기록하지 않음.
- 어댑터는 키를 응답·로그에 노출하지 않음(`log_message` 억제).
- config의 `apiKey`는 실제 키가 아닌 더미 문자열.

## 8. 실행 / 재기동 방법

어댑터 실행(키 필요):

```bash
cd /home/ubuntu/agy-router/openclaw-test-provider
AGY_KEY_1=<키> python3 adapter/adapter.py
```

호출 테스트:

```bash
openclaw capability model run \
  --model antigravity-test/antigravity-preview-05-2026 \
  --prompt "한 줄 인사"
```

## 9. 남은 이슈 / 주의

1. **어댑터 의존성** — 어댑터가 꺼져 있으면 모델 호출이 실패한다.
   (현재 백그라운드 세션 `mellow-otter`로 실행 중)
2. **게이트웨이 재시작 미실행** — `openclaw` 툴이
   `Plugin inventory has retired` 오류를 반환해 재시작을 못 했다.
   config는 "재시작 없이 적용"으로 보고되었고, CLI 경로 검증은 통과했다.
   Discord 세션에서 이 모델을 직접 선택하려면 게이트웨이 재시작이 필요할 수 있다.
3. `index.ts`(초안)는 현재 미사용. 빌드 없는 `index.js`로 대체됨.

## 10. 롤백

```bash
openclaw plugins uninstall antigravity-test
openclaw config unset models.providers.antigravity-test
# modelPolicy.allow 에서 antigravity-test/... 항목 제거
```
