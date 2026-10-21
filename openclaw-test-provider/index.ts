/**
 * OpenClaw Model Provider Plugin: Antigravity Test Provider
 *
 * 테스트 전용 Provider 플러그인.
 *
 * 설계 (최소/저위험):
 *  - Python 어댑터(adapter/adapter.py)가 기존 cloud_wrapper.call_antigravity를
 *    재사용해 Antigravity API를 로컬 OpenAI 호환 엔드포인트로 노출한다.
 *  - 이 플러그인은 `catalog` 훅만으로 그 baseUrl을 가리키는 프로바이더를
 *    등록한다. 스트리밍/전송은 OpenClaw의 내장 openai-completions 트랜스포트가
 *    처리하므로 커스텀 createStreamFn이 필요 없다.
 *  - 세션 연속성(environment_id / previous_interaction_id)과 대화 간 격리는
 *    어댑터가 담당한다.
 *
 * 보안:
 *  - API 키는 이 플러그인/코드/로그에 기록하지 않는다.
 *  - 실제 키는 어댑터 프로세스 환경(AGY_KEY_1)에만 존재한다.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// Python API 어댑터 (별도 프로세스) OpenAI 호환 베이스 URL
const ADAPTER_BASE_URL = "http://127.0.0.1:8765/v1";

// 테스트 모델 ID
const MODEL_ID = "antigravity-preview-05-2026";

export default definePluginEntry({
  id: "antigravity-test",
  name: "Antigravity Test Provider",
  description:
    "Test-only OpenClaw Model Provider for Antigravity (via local adapter)",
  register(api) {
    api.registerProvider({
      id: "antigravity-test",
      label: "Antigravity Test",
      docsPath: "/providers/antigravity-test",
      envVars: ["AGY_KEY_1"],

      // 모델 카탈로그: 로컬 어댑터를 OpenAI 호환 프로바이더로 등록
      catalog: {
        order: "simple",
        run: async () => ({
          provider: {
            baseUrl: ADAPTER_BASE_URL,
            apiKey: "local-adapter",
            api: "openai-completions",
            models: [
              {
                id: MODEL_ID,
                name: "Antigravity Preview 05-2026 (Test)",
                reasoning: false,
                input: ["text"],
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
                contextWindow: 1048576,
                maxTokens: 65536,
              },
            ],
          },
        }),
      },
    });
  },
});
