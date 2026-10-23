# 🔑 치이 (`agy-router`) 원리 및 에이전트(Hermes 등) 연동 가이드

**치이 (Chii / 공식명: `agy-router`)**는 Google Antigravity (Interactions API)의 까다로운 상태 유지(Stateful) 규격과 무료 티어 쿼터(429 Rate Limit) 문제를 해결해 주는 **멀티 키 풀(Multi-Key Pool) 라우터 겸 OpenAI 호환 로컬 어댑터**입니다.

헤르메스(Hermes) 에이전트나 OpenClaw처럼 **OpenAI 호환 API(`/v1/chat/completions`)**를 지원하는 모든 에이전트 프레임워크에 바로 연결해서 쓸 수 있습니다.

---

## 1. 치이(`agy-router`)의 핵심 동작 원리

일반적인 프록시와 달리 Antigravity API는 **서버가 대화와 샌드박스 상태를 기억하는 Stateful API**이기 때문에 단순히 키만 돌려막으면 에러(400/404/500)가 터집니다. 치이는 이를 4가지 핵심 메커니즘으로 해결합니다.

### ① OpenAI ↔ Antigravity 프로토콜 변환 (`adapter.py`)
* 에이전트가 보내는 표준 OpenAI 요청(`POST /v1/chat/completions`, `messages`, `tools`, `stream`)을 받아 Google Interactions API(`v1beta/interactions`) 규격으로 자동 변환합니다.
* 응답 역시 일반 텍스트 스트리밍(SSE)과 병렬 도구 호출(`tool_calls`) 규격으로 맞춰서 에이전트에게 돌려줍니다.

### ② 멀티 키 풀 & 지능형 롤오버 (`keys.py`, `openclaw-test-provider/adapter/key_pool.py`, `openclaw-test-provider/adapter/rollover_runner.py`)
* 여러 개의 API 키(`AGY_KEY_1` ~ `AGY_KEY_N`)를 풀(Pool)로 묶어 라운드로빈/LRU 방식으로 분산 호출합니다.
* **429 (할당량 초과):** 해당 키를 즉시 `COOLDOWN` 상태로 전환하고 다른 활성 키로 자동 재시도(Rollover)합니다.
* **401 / 403 (인증 오류):** 해당 키를 `INACTIVE` 처리하고 다음 키로 넘깁니다.
* **5xx (일시적 서버 오류):** 같은 키로 1회 재시도 후 실패 시 다른 키로 롤오버합니다.

### ③ 세션 연속성 및 테넌트 고정 (`env_key_ref`)
* Antigravity는 대화가 시작되면 고유한 `environment_id`와 `interaction_id`를 발급합니다.
* 이 ID들은 **처음 생성한 API 키(GCP 프로젝트/테넌트)에 귀속**됩니다. 치이는 대화(세션)별로 해시 키를 만들어 `environment_id`와 `previous_interaction_id`, 그리고 **해당 세션을 연 API 키(`env_key_ref`)**를 매핑해 둡니다.
* 만약 중간에 그 키가 429 한도에 걸리면? 기존 `environment_id`를 깔끔하게 리셋하고 새 키에서 대화 맥락을 이어가도록 처리해 404 충돌을 막습니다.

### ④ 도구 호출(Function Calling) 중 키 고정 (`pin_key_ref`) & call_id 매핑
* 에이전트가 도구(Tool)를 호출할 때(`requires_action`), Google 서버는 도구의 실행 결과(`function_result`)가 돌아올 때까지 해당 세션을 열어두고 기다립니다.
* 이때 중간에 API 키가 바뀌면 100% 에러가 발생하므로, 치이는 **도구 호출 체인이 시작되면 최종 텍스트 답변이 나올 때까지 해당 키를 강제로 고정(Pinning)**합니다.
* 또한 에이전트 프레임워크가 도구 호출 ID(`call_id`)를 자체 포맷으로 덮어쓰더라도, 치이가 내부에 원본 Google `call_id`를 기억해 두었다가 결과 제출 시 원래 ID로 자동 복원해 줍니다.

---

## 2. 설치 및 환경 세팅 방법

### Step 1. 저장소 준비
Python 3.10 이상이 설치된 환경(Linux/WSL 권장)에 `agy-router` 폴더를 배치합니다. (외부 무거운 패키지 없이 Python 표준 라이브러리 위주로 동작해 세팅이 매우 가볍습니다.)

```bash
git clone <치이_저장소_경로_또는_압축해제> ~/agy-router
cd ~/agy-router
```

### Step 2. API 키 등록 (`~/.bashrc`)
Google AI Studio 등에서 발급받은 API 키들을 `~/.bashrc` 맨 아래에 등록합니다. (최소 2개 이상 등록해야 멀티 키 롤오버가 작동합니다. 서로 다른 GCP 프로젝트에서 발급한 키일수록 쿼터 분산 효과가 큽니다!)

```bash
# ~/.bashrc 하단에 추가 (1번부터 원하는 만큼 추가 가능)
export AGY_KEY_1="AIzaSy..."
export AGY_KEY_2="AIzaSy..."
export AGY_KEY_3="AIzaSy..."
```

적용 및 키 인식 확인:
```bash
chmod 600 ~/.bashrc
source ~/.bashrc
cd ~/agy-router
python3 cli.py key-pool discover
python3 cli.py key-pool list
```

### Step 3. 로컬 OpenAI 호환 어댑터 실행
`openclaw-test-provider/run_adapter.sh` 스크립트를 실행하면 `~/.bashrc`의 모든 `AGY_KEY_N`을 자동으로 읽어 들여 `127.0.0.1:8765` 포트에 OpenAI 호환 서버를 띄웁니다.

```bash
cd ~/agy-router/openclaw-test-provider
chmod +x run_adapter.sh
./run_adapter.sh
```
> **팁 (상시 구동):** 터미널을 꺼도 유지되게 하려면 `tmux`에 띄우거나 `systemd` 서비스(`agy-adapter.service`)로 등록해 두면 편합니다.

어댑터가 정상 실행되었는지 확인:
```bash
curl http://127.0.0.1:8765/v1/models
```

---

## 3. 헤르메스(Hermes) 에이전트 연동 설정

치이 어댑터는 완벽한 **OpenAI 호환 엔드포인트**로 동작하므로, 헤르메스 에이전트의 모델/프로바이더 설정에서 **Custom OpenAI (또는 Local LLM / OpenAI-compatible)** 항목에 아래 값만 넣어주면 끝납니다!

* **Provider Type:** `openai` (또는 `openai-compatible` / `custom`)
* **Base URL (API Endpoint):** `http://127.0.0.1:8765/v1`
* **API Key:** `dummy-key` (실제 키 관리는 치이가 백엔드에서 하므로 아무 문자열이나 넣어도 됨)
* **Model ID:** `antigravity-preview-09-2026`
  *(내부 기본 타겟 모델은 `gemini-4-argon` 등으로 매핑되어 작동합니다)*
* **Timeout 권장값:** `180` – `300`초 (Antigravity 추론 및 도구 호출 시 생각 시간이 길어질 수 있으므로 타임아웃을 넉넉히 잡는 것이 좋습니다)

### 연동 테스트 (cURL 예시)
헤르메스에 연결하기 전 아래 명령어로 답변이 잘 오는지 바로 테스트해 볼 수 있습니다:

```bash
curl -X POST http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "antigravity-preview-09-2026",
    "messages": [{"role": "user", "content": "안녕! 정상 작동하는지 한 줄로 답해줘."}],
    "stream": false
  }'
```

---

## 4. 적정 컨텍스트(Context) 유지 기준 & 토큰(TPM) 절약 꿀팁

Antigravity API(무료 티어 기준)를 쓸 때 가장 중요한 핵심 제약은 **총 컨텍스트 크기(1M)가 아니라 분당 입력 토큰 한도(100,000 TPM)**입니다.
Antigravity는 호출할 때마다 내부 시스템 프롬프트 + 샌드박스 스펙 + 연결된 도구(Tool) 스키마를 한꺼번에 묶어서 처리하기 때문에, 대화가 길어지면 100k TPM 벽에 부딪혀 `429 Rate Limit`이나 `TRANSIENT_ERROR` 타임아웃이 급증합니다.

### ① 권장 컨텍스트 유지 라인 (Sweet Spot)
* **최적 구간 (권장):** **20,000 – 50,000 토큰** 이내
  * 가장 빠르고 도구 호출 정확도가 높은 구간입니다.
* **주의 구간 (컴팩션/요약 권장):** **60,000 – 80,000 토큰**
  * 도구 연속 호출(Multi-step Tool Call)을 2–3번만 해도 누적 TPM이 100k를 넘겨 키 롤오버가 자주 발생합니다. 헤르메스 에이전트의 자동 요약(Compaction / Pruning) 임계치를 **60k – 70k 토큰** 선으로 설정해 두는 것이 가장 안전합니다.
* **위험 구간 (한도 초과):** **90,000 토큰 이상**
  * 단일 요청만으로도 무료 티어 100k TPM에 근접하여 어떤 키로 돌려도 429가 뜰 수 있습니다.

### ② 치이 어댑터(`adapter.py`)에 내장된 자동 다이어트 기능
치이는 에이전트가 실수로 거대한 히스토리를 던져도 죽지 않도록 자체 방어막을 갖고 있습니다 (환경변수로 조절 가능):
1. **오래된 도구 결과 자동 축약 (`AGY_ADAPTER_MAX_OLD_TOOL_CHARS=3500`):**
   * 최근 4개 메시지 이전의 과거 도구 실행 결과(`role: tool`)가 3,500자를 넘으면 앞뒤 핵심만 남기고 가운데를 `...[trimmed]...`으로 자동 압축합니다.
2. **한/영 가중치 기반 프롬프트 토큰 상한 가드 (`AGY_ADAPTER_MAX_PROMPT_TOKENS=75000`):**
   * 단순 글자 수가 아닌 한글/CJK(글자당 1토큰)와 영문/ASCII(글자당 0.3토큰) 비율을 반영한 토큰 추정기(`_estimate_tokens`)로 약 75k 토큰을 넘어가면 시스템 프롬프트는 보존하되, 오래된 대화부터 잘라내며 생략된 메시지 수(`N earlier message(s) omitted`)를 명시합니다.
3. **과거 이미지 개수 제한:**
   * 멀티모달 입력 시 과거 이미지가 계속 쌓여 용량이 터지는 것을 막기 위해 **최근 5개 이미지**만 유지합니다.
4. **무한 도구 루프 가드 (`AGY_ADAPTER_MAX_FC_STREAK=25`):**
   * 모델이 에러에 빠져 도구만 25번 연속 호출하면, 다음 턴엔 강제로 도구를 빼고 텍스트 답변을 유도해 토큰 증발을 차단합니다.

### ③ 헤르메스(Hermes) 에이전트 세팅 시 실전 토큰 절약 팁 5가지
1. **안 쓰는 도구(MCP / Built-in Tools) 끄기:**
   * 도구를 30–40개씩 등록해 두면 매 턴마다 도구 설명(JSON Schema)만으로 10,000–20,000 토큰을 기본 소모합니다. **꼭 필요한 핵심 도구(파일 읽기/쓰기, 쉘 실행, 검색 등 10–15개 이내)**만 활성화하거나, 지연 로딩(Deferred Tool Search) 기능을 켜세요.
2. **도구 출력(Tool Output) 길이 제한:**
   * 전체 파일 읽기나 긴 터미널 로그 출력이 컨텍스트를 가장 빨리 망가뜨립니다. 파일은 줄 단위(`offset`/`limit`)로 끊어 읽게 유도하고, `grep`/`head`/`tail`로 필요한 부분만 뽑아 쓰도록 시스템 프롬프트에 한 줄 넣어두세요.
3. **무거운 작업은 서브에이전트(Isolated Context)로 분리:**
   * 대형 코드 분석이나 웹 크롤링 같은 작업은 메인 대화창에서 바로 돌리지 말고, **독립된 일회용 서브에이전트(Subagent)**를 띄워 요약 결과만 메인 세션으로 돌려받게 하면 메인 컨텍스트를 평생 쾌적하게 유지할 수 있습니다.
4. **도구 연속 호출 중에는 세션이 같은 키에 묶임(`pin_key_ref`)을 기억하기:**
   * 한 턴에 도구를 10번 연속으로 부르면 **키가 롤오버되지 않고 한 키(한 GCP 프로젝트)에서 10번의 토큰을 모두 감당**해야 합니다. 따라서 "한 번에 스크립트로 묶어서 실행해" 또는 "병렬로 한 턴에 호출해"라고 지시하면 턴 수를 확 줄일 수 있습니다.
5. **주제가 바뀌면 과감하게 새 세션(`/new` 또는 `/reset`) 시작:**
   * 이전 작업과 무관한 새 코딩/질문을 할 때는 컨텍스트를 비우고 새로 시작하는 것이 응답 속도와 키 수명 모두에 압도적으로 유리합니다.

---

## 5. 트러블슈팅 & 모니터링 팁

어댑터가 돌아가는 동안 `/tmp` 경로에 실시간 상태 로그가 기록됩니다.

* **실시간 호출/도구 흐름 확인:** `tail -f /tmp/agy_adapter_debug.jsonl`
* **키 롤오버(429 교체) 기록 확인:** `tail -f /tmp/agy_adapter_rollover.jsonl`
* **키 풀 상태 확인 CLI:** `cd ~/agy-router && python3 cli.py health`
