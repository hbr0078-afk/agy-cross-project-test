# 치이(agy-router) MCP 연동 로드맵

Status: **계획 (실행 전)** · 2026-10-18
Owner: 부장 에구치 코코로 · 결정권자: 주호

## 0. 목표

치이(agy-router)가 MCP 도구를 사용할 수 있게 한다.
1차 목표: **로블록스 Studio MCP**(PC 노드가 보유)를 치이가 호출할 수 있게 한다.

## 1. 사실 확인 (공식 문서 기반, 검증 완료)

출처: `https://ai.google.dev/gemini-api/docs/antigravity-agent` (2026-10-13 갱신)

- Antigravity Agent API는 **함수 호출(function calling)** 을 지원한다.
  - 요청: `tools: [{"type":"function","name":...,"description":...,"parameters":{JSON Schema}}]`
  - 응답: `steps[]` 안에 `type=="function_call"` 스텝(`id`, `name`, 인자).
  - 반환: `input: [{"type":"function_result","name":...,"call_id":...,"result":{...}}]` +
    `previous_interaction_id` + `environment`(=environment_id)로 이어서 전송.
- Antigravity Agent API는 **원격 MCP 서버(streamable HTTP)** 를 네이티브로 지원한다.
  - 요청: `tools: [{"type":"mcp_server","name":"^[a-z0-9_-]+$","url":"https://...","headers":{},"allowed_tools":[]}]`
  - **제약: 원격(remote) HTTP만.** 로컬 stdio MCP 미지원.

현재 치이 코드 상태:
- `client.py`: payload에 `agent/input/environment/agent_config`만. `tools` 없음. 툴콜 루프 없음.
- `client.py` 응답 파싱: `output` / `steps[].content[].text` / `steps[].text`만. `function_call` 스텝 미처리.
- 저장소 전체 `mcp` / `tool_calls` / `function_call` 참조 **0건**.
- `web_ui.py`: 0.0.0.0:8080 대시보드(대시보드 전용, OpenAI 호환 아님).

## 2. 핵심 제약 (설계를 좌우)

- Antigravity 에이전트는 **Google 호스팅 Linux 샌드박스**에서 실행된다(`environment: "remote"`).
  → 네이티브 `mcp_server` 경로를 쓰려면 MCP 엔드포인트가 **인터넷에서 도달 가능**해야 함.
- 로블록스 Studio MCP는 **PC 노드의 로컬 stdio 서버**(Tailscale 사설 IP 100.121.231.91).
  → 그대로는 Google 샌드박스에서 도달 불가.

## 3. 두 가지 아키텍처

### A. 네이티브 원격 MCP (mcp_server)
`tools:[{type:mcp_server,url:...}]`로 치이가 직접 위임.

- 필요: MCP를 **공개 streamable-HTTP**로 노출(Tailscale Funnel / cloudflared 등) + stdio→HTTP 래퍼(supergateway/mcp-proxy).
- 장점: 치이 코드 변경 최소(툴 스키마만 넘김).
- 단점: **공개 노출 = 보안 리스크**. 로블록스 Studio MCP를 인터넷에 띄우는 건 위험.
- 판정: **비추천**(보안). 격리 테스트용으로만.

### B. 함수 호출 브리지 (function calling) — **권장**
치이가 MCP 도구 스키마를 `type:"function"`으로 선언 → 모델이 `function_call` 반환 →
치이가 **로컬에서** PC 노드 MCP(`mcp.tools.call.v1`)를 실행 → `function_result` 반환.

- 필요: 치이에 (1) 툴 스키마 주입, (2) `function_call` 파싱, (3) MCP 실행 어댑터, (4) 툴콜 루프.
- 장점: **공개 노출 불필요**. 로컬 MCP 그대로 활용. 키풀/스티키 바인딩과 자연 결합.
- 단점: 치이 쪽 구현량 있음(툴 루프 자체 구현).

## 4. 권장 경로: B (함수 호출 브리지) — 단계별

### Phase M1 — 툴 스키마 소스 확보
- PC 노드 MCP 도구 목록/스키마를 읽어 표준 툴 정의로 변환.
  (nodes describe의 `nodePluginTools[].parameters` 재사용)
- 산출물: `mcp_tools.py` — `list_tools()` → Antigravity `type:"function"` 배열.
- 검증: 스키마 JSON이 유효하고 도구 개수/이름이 노드와 일치.

### Phase M2 — 클라이언트 툴콜 지원
- `client.py`: `tools` payload 주입 + `steps[]`에서 `function_call`(id/name/args) 추출.
- `function_result` 전송 경로 추가(`previous_interaction_id`+`environment` 유지).
- 검증: 단위 테스트(가짜 응답)로 파싱/왕복.

### Phase M3 — MCP 실행 어댑터
- `mcp_bridge.py`: `call_tool(name, args)` → PC 노드 `mcp.tools.call.v1` 호출 → 결과 dict.
- 안전장치: allowed_tools 화이트리스트, 타임아웃, 결과 크기 상한.
- 검증: 로블록스 MCP 1개 도구 무해 호출(예: `get_studio_state`).

### Phase M4 — 에이전트 루프
- `agent_loop.py`: 요청 → function_call 나오면 M3 실행 → function_result 반환 → 반복(최대 N회).
- 키풀/쿨다운/스티키 바인딩과 결합(루프 전체가 동일 키·환경 유지).
- 검증: "Studio 상태를 알려줘" 같은 실제 목표 달성.

### Phase M5 — CLI/문서
- `cli.py` 서브커맨드(예: `mcp list`, `mcp call`, `run-agent`) + README 갱신.
- 검증: E2E 1건 + 전체 회귀 스위트 PASS.

## 5. 리스크 / 미해결

- Antigravity `function_call` 스텝의 **정확한 필드명**(`id`/`name`/`args`?)은 문서 예시 기반 추정 → M2에서 실측 필요.
- 툴콜 루프 턴 수/토큰 비용 증가 → 상한(N) 필요.
- `tools` 명시 시 에이전트 기본도구(code_execution 등)가 **대체되는지** 병존인지 미확정(genkit 이슈 #6711 시사) → 실측.
- 로블록스 MCP 조작은 게임 상태를 바꿀 수 있음 → 화이트리스트 필수.

## 6. 결정 필요 (주호)

1. 경로: **B 권장** 동의? (A는 보안상 비추천)
2. 첫 실증 도구: 읽기 전용(`get_studio_state` / `list_roblox_studios`)부터?
3. 구현 주체: 나(코코로) 직접 vs 나가토 유키 위임.
4. 커밋: 현재 `~/agy-router` 작업트리 dirty(client/config/runtime M) — M1 착수 전 정리/커밋 여부.
