# 🔑 agy-router (애칭: 치이 / ちい / Chii)

**Antigravity Multi-Key Router & OpenAI 호환 로컬 어댑터**
공식명 `agy-router`, 애칭 **치이 (ちい / Chii)**.

Google Antigravity (Interactions API)의 까다로운 **상태 유지(Stateful) 규격**과 무료 티어 **할당량 제한(429 Rate Limit / 100k TPM)** 문제를 해결해 주는 멀티 키 풀(Multi-Key Pool) 라우터이자, **헤르메스(Hermes)**·**OpenClaw** 등 모든 OpenAI 호환 에이전트에 바로 연결할 수 있는 로컬 어댑터입니다.

> 📖 **상세 가이드 문서:** [`docs/CHII_HERMES_GUIDE.md`](docs/CHII_HERMES_GUIDE.md)

---

## 1. 치이(`agy-router`)의 핵심 동작 원리

일반적인 LLM 프록시와 달리 Antigravity API는 **서버가 대화와 샌드박스 상태를 기억하는 Stateful API**이기 때문에 단순히 API 키만 돌려막으면 400/404/500 에러가 발생합니다. 치이는 이를 4가지 핵심 구조로 해결합니다.

### ① OpenAI ↔ Antigravity 프로토콜 변환 (`openclaw-test-provider/adapter/adapter.py`)
* 에이전트가 보내는 표준 OpenAI 요청(`POST /v1/chat/completions`, `messages`, `tools`, `stream`)을 받아 Google Interactions API(`v1beta/interactions`) 규격으로 자동 번역합니다.
* 응답 역시 일반 텍스트 스트리밍(SSE)과 병렬 도구 호출(`tool_calls`) 규격으로 맞춰서 에이전트에게 돌려줍니다.

### ② 멀티 키 풀 & 지능형 롤오버 (`keys.py`, `openclaw-test-provider/adapter/key_pool.py`, `openclaw-test-provider/adapter/rollover_runner.py`)
* 여러 개의 API 키(`AGY_KEY_1` ~ `AGY_KEY_N`)를 풀(Pool)로 묶어 라운드로빈/LRU 방식으로 분산 호출합니다.
* **429 (할당량 초과):** 해당 키를 즉시 `COOLDOWN` 상태로 전환하고 1초 내에 다음 활성 키로 자동 재시도(Rollover)합니다.
* **401 / 403 (인증 오류):** 해당 키를 `INACTIVE` 처리하고 다음 키로 넘깁니다.
* **5xx (일시적 서버 오류):** 동일 키로 1회 재시도 후 실패 시 다른 키로 롤오버합니다.

### ③ 세션 연속성 및 테넌트 매핑 (`env_key_ref`)
* Antigravity가 발급하는 `environment_id`와 `interaction_id`는 최초 호출한 API 키(GCP 프로젝트)에 귀속됩니다.
* 치이는 대화별로 세션을 격리해 원래 키를 유지해 주고, 만약 그 키가 429 한도에 걸려 다른 키로 넘어가야 할 때는 이전 세션 ID를 깔끔하게 리셋해서 타 프로젝트 간 404 충돌을 방지합니다.

### ④ 도구 호출(Function Calling) 중 키 고정 (`pin_key_ref`) & `call_id` 매핑
* 에이전트가 도구(Tool)를 호출할 때(`requires_action`), Google 서버는 도구의 실행 결과(`function_result`)가 돌아올 때까지 해당 세션을 열어두고 기다립니다.
* 이때 중간에 API 키가 바뀌면 100% 에러가 나므로, 치이는 **도구 호출 체인이 시작되면 최종 텍스트 답변이 나올 때까지 해당 키를 강제로 고정(Pin)**하고, 에이전트가 바꾸거나 생략한 `tool_call_id`도 Google 원본 `call_id`로 정확히 복원해 줍니다.

---

## 2. 빠른 시작 (3단계 설치 및 실행)

### Step 1. 저장소 클론
Python 3.10+ 환경(Linux / macOS / WSL)이면 별도의 무거운 외부 패키지 설치 없이 표준 라이브러리만으로 즉시 동작합니다.
```bash
git clone https://github.com/hbr0078-afk/agy-cross-project-test.git ~/agy-router
cd ~/agy-router
```

### Step 2. `~/.bashrc`에 API 키 등록
서로 다른 GCP 프로젝트에서 발급받은 Gemini/Antigravity API 키를 2개 이상 등록할수록 429 방어력이 높아집니다. `~/.bashrc` 하단에 아래처럼 추가하세요:

```bash
# agy-router API Keys (1..N)
export AGY_KEY_1="AIzaSy..."
export AGY_KEY_2="AIzaSy..."
export AGY_KEY_3="AIzaSy..."
```

적용 및 키 풀 인식 확인:
```bash
chmod 600 ~/.bashrc
source ~/.bashrc
cd ~/agy-router
python3 cli.py key-pool discover
python3 cli.py key-pool list
```

### Step 3. 로컬 OpenAI 호환 어댑터 실행
`run_adapter.sh` 스크립트를 실행하면 `~/.bashrc`의 모든 `AGY_KEY_N`을 자동으로 읽어 들여 `127.0.0.1:8765` 포트에 OpenAI 호환 서버를 띄웁니다.

```bash
cd ~/agy-router/openclaw-test-provider
chmod +x run_adapter.sh
./run_adapter.sh
```
*(팁: `tmux`나 `systemd` 서비스로 등록해 두면 백그라운드에서 상시 구동할 수 있습니다.)*

---

## 3. 헤르메스(Hermes) 등 에이전트 연동 설정

치이 어댑터는 완벽한 **OpenAI 호환 엔드포인트**로 동작하므로, 헤르메스 에이전트의 모델 설정에서 **OpenAI Compatible (Custom Endpoint)** 항목에 아래 값만 입력하면 됩니다.

* **Provider:** `openai` (또는 `openai-compatible` / `custom`)
* **Base URL:** `http://127.0.0.1:8765/v1`
* **API Key:** `dummy-key` (실제 키 관리는 치이가 백엔드에서 수행하므로 아무 값이나 입력)
* **Model ID:** `antigravity-preview-09-2026`
* **Timeout 권장값:** `180` ~ `300`초 (깊은 추론 및 연속 도구 호출 대비)

### 연동 테스트 (cURL)
```bash
curl -X POST http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "antigravity-preview-09-2026",
    "messages": [{"role": "user", "content": "안녕! 한 줄로 답해줘."}],
    "stream": false
  }'
```

---

## 4. 📊 적정 컨텍스트(Context) 유지 기준 & 토큰(TPM) 절약 꿀팁

Antigravity API(무료 티어)를 사용할 때 가장 주의할 병목은 전체 컨텍스트 크기(1M)가 아니라 **분당 입력 토큰 한도(100,000 TPM)**입니다. 매 호출마다 시스템 프롬프트, 샌드박스 스펙, 도구(Tool) 스키마가 함께 전송되므로 컨텍스트 관리가 핵심입니다.

### ① 권장 컨텍스트 유지 라인 (Sweet Spot)
* **최적 구간:** **20,000 ~ 50,000 토큰** 이내 (속도·도구 정확도 최고 구간)
* **주의 구간 (자동 요약/Compaction 권장):** **60,000 ~ 70,000 토큰**
  * 이 구간부터는 도구를 2~3회 연속 호출하면 분당 합산 토큰(TPM)이 100k를 초과해 429 롤오버가 자주 발생합니다. 에이전트의 **자동 요약(Compaction) 임계값을 60k~70k 토큰**으로 설정하는 것을 권장합니다.
* **위험 구간:** **90,000 토큰 이상** (단일 요청만으로 100k TPM 한도에 도달할 수 있음)

### ② 치이 어댑터(`adapter.py`)에 내장된 자동 토큰 절약 기능
치이 어댑터는 100k TPM 초과를 막기 위해 다음 방어 로직을 자동으로 수행합니다 (환경변수로 조절 가능):
1. **과거 도구 결과 자동 압축 (`AGY_ADAPTER_MAX_OLD_TOOL_CHARS=3500`):** 최근 4개 메시지 이전의 오래된 도구 실행 결과(`role: tool`)가 3,500자를 넘으면 앞뒤 핵심만 남기고 가운데를 `...[trimmed]...`로 자동 생략합니다.
2. **한/영 가중치 기반 프롬프트 토큰 상한 방어 (`AGY_ADAPTER_MAX_PROMPT_TOKENS=75000`):** 단순 글자 수가 아닌 한글/CJK(글자당 약 1토큰)와 영문/ASCII(글자당 약 0.3토큰) 가중치를 계산하는 토큰 추정기(`_estimate_tokens`)로 약 75k 토큰 상한을 방어하며, 초과 시 시스템 프롬프트는 보존하고 오래된 대화 턴을 생략하면서 **생략된 메시지 개수(`N earlier message(s) omitted`)**를 명시적으로 남깁니다.
3. **멀티모달 이미지 개수 제한:** 이미지가 대화에 계속 쌓여 용량이 폭발하지 않도록 **최근 5개 이미지**만 전송합니다.

### ③ 에이전트 세팅 시 실전 토큰 절약 팁 5가지
1. **불필요한 도구(MCP / 내장 툴) 끄기:** 도구를 30~40개씩 켜두면 매 턴마다 도구 JSON 스키마만으로 1~2만 토큰이 소모됩니다. 자주 쓰는 **핵심 도구 10~15개 이내**만 활성화하거나 지연 로딩(Tool Search)을 활용하세요.
2. **도구 호출 시 배치/병렬 실행 유도:** 도구 체인이 진행되는 동안에는 키가 고정(`pin_key_ref`)되므로 한 질문에서 도구를 10번씩 잘게 쪼개 호출하면 한 키의 TPM이 빠르게 소진됩니다. 시스템 프롬프트에 *"명령어를 쪼개지 말고 한 번에 묶어서 실행하거나 병렬 도구 호출을 사용하라"*고 명시하면 턴 수를 크게 줄일 수 있습니다.
3. **긴 파일/로그 부분 읽기 강제:** 수천 줄짜리 코드나 로그를 통째로 읽지 않고 `grep`, `head`, `tail` 또는 줄 범위(`offset`/`limit`)로 필요한 구간만 읽도록 지침을 주세요.
4. **무거운 탐색/크롤링은 서브에이전트(Subagent)로 분리:** 대규모 코드 분석이나 반복 웹 검색은 독립된 서브에이전트(Isolated Context)에 위임하고 요약 결과만 메인 세션으로 받으면 메인 컨텍스트가 오염되지 않습니다.
5. **주제가 바뀔 때는 새 세션(`/new`, `/reset`) 시작:** 이전 작업과 무관한 새 주제로 넘어갈 때는 세션을 리셋하는 것이 응답 속도와 쿼터 방어에 훨씬 유리합니다.

---

## 5. 모니터링 및 CLI 진단 도구

* **실시간 어댑터 호출/도구 로그:** `tail -f /tmp/agy_adapter_debug.jsonl`
* **키 롤오버(429 교체) 로그:** `tail -f /tmp/agy_adapter_rollover.jsonl`
* **라우터 헬스체크 및 진단 CLI:**
  ```bash
  python3 cli.py health
  python3 cli.py diagnostics
  ```
