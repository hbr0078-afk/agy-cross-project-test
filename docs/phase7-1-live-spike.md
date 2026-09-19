# Phase 7-1 Live Spike Report: Antigravity Resource Ownership & Multi-Tenant Continuity

**Date:** 2026-09-19  
**Repository:** `hbr0078-afk/agy-cross-project-test`  
**Branch:** `review/phase1-5-security`  
**Environment:** Linux 6.17.0-1019-oracle (arm64), Python 3.12, Node.js v24.19.0  
**Endpoint:** `https://generativelanguage.googleapis.com/v1beta`  

---

## 1. Executive Summary & Verdict

- **Architecture Branch:** **`BRANCH C — Cross-Project Resource Access Forbidden (Tenant-Locked)`**
- **Discovery Verdict:** 
  Google Antigravity API의 `environment_id`, `interaction_id`, 및 원격 파일 스토리지(`environments/{env_id}/files`)는 **API Key를 발급한 Google Cloud Project/Tenant에 엄격하게 바인딩(Strictly Tenant-Bound)**되어 있습니다.
- 서로 다른 프로젝트에서 발급된 Key로 기존 Environment 또는 Interaction을 이어가려 할 경우, 인증 거부(403)가 아닌 **완전 은폐형 404 (`Requested entity was not found.`, code: `not_found`)** 오류가 발생합니다.
- 따라서 **Cross-Project Key Rollover 시 기존 Environment나 Interaction을 승계하는 것은 불가능**하며, 키 롤오버 시 반드시 **Key Group/Tenant Affinity 분리** 및 **세션 리셋 후 WorkspaceSync 재동기화** 정책을 따라야 합니다.

---

## 2. Agent ID Compatibility Finding

| Agent ID | API Status Code | Status in Body | Result |
|---|---|---|---|
| `antigravity-preview-05-2026` | `200 OK` | `completed` | Accepted (Production Code Default) |
| `antigravity-preview-09-2026` | `200 OK` | `completed` | Accepted (Recommended in Current Docs) |

- 기존 코드의 `antigravity-preview-05-2026`과 최신 문서의 `antigravity-preview-09-2026` **둘 다 실제 Google API에서 정상 허용(200 OK)**됩니다.
- 본 Phase 7-1에서는 기존 production 코드의 무결성을 유지하기 위해 `client.py`를 변경하지 않았습니다.

---

## 3. Key Mapping & Test Setup

- **`key1`**: Project-A (Google AI Studio Project 1)
- **`key2`**: Project-B (Google AI Studio Project 2)
*(보안 규정에 따라 실제 API Key 문자열 및 민감 헤더는 영구 격리 및 마스킹 처리됨)*

---

## 4. Live API Test Results & Measurements

### Test 1: Baseline Interaction under `key1`
- **Agent:** `antigravity-preview-09-2026`
- **Input:** `Remember the token: SPIKE_TOKEN_7A91. Reply exactly: BASELINE_OK`
- **HTTP Status:** `200 OK`
- **Environment ID (`ENV_A`):** `bdf4fedca8f6f327ecaf3acda02435d2`
- **Interaction ID (`INT_A`):** `v1_Chd2NUd1YW9hOEdZSE5yZmNQZ0syU3lRSRIXdjVHdWFvYThHWUhOcmZjUGdLMlN5UUk`
- **Output:** `completed` / `BASELINE_OK`

### Test 2: Same-Key Interaction Continuation (`key1` → `ENV_A` + `INT_A`)
- **Input:** `What token did I ask you to remember? Reply with only the token.`
- **HTTP Status:** `200 OK`
- **Semantic Result:** **`SPIKE_TOKEN_7A91`** 정확히 반환 (Context Preserved)

### Test 3: Cross-Project Interaction Continuation (`key2` → `ENV_A` + `INT_A`)
- **Input:** Recall previous token with `key2`
- **HTTP Status:** **`404 Not Found`**
- **Error Response Body:** `{"error": {"message": "Requested entity was not found.", "code": "not_found"}}`
- **Analysis:** `key2`는 `key1`의 프로젝트에 속한 `INT_A` 및 `ENV_A`에 대한 접근 권한이 없어 리소스 존재 자체가 은폐됨.

### Test 4: Cross-Project Environment Access Only (`key2` → `ENV_A`, no interaction id)
- **Input:** List files in `ENV_A` with `key2`
- **HTTP Status:** **`404 Not Found`**
- **Error Response Body:** `{"error": {"message": "Requested entity was not found.", "code": "not_found"}}`
- **Analysis:** Interaction continuity뿐만 아니라 **Environment 자체의 접근도 테넌트 격리**됨.

### Test 7: Direct Environments API Inspection (`GET /v1beta/environments/{ENV_A}`)
- **`key1` (Owner):** `200 OK` (Metadata: `id`, `created`, `updated`, `status: ACTIVE`, `file_count: 1`, `size_bytes: 4096`)
- **`key2` (Cross-Tenant):** **`404 Not Found`** (`Requested entity was not found.`)

### Test 8: Baseline & Same-Key Continuity under `key2`
- **`key2` Fresh Creation:** `200 OK` (`ENV_B`: `8206f9dfd63b389fd6daa396c62400ed`, `INT_B`: `v1_ChctcEd1YXZtZEFzS2o5dE1QblpHWG1ROBIXLXBHdWF2bWRBc0tqOXRNUG5aR1htUTg`)
- **`key2` Continuation:** `200 OK` (`SPIKE_KEY2_TOKEN_8821` 정확히 회상)
- **`key1` Cross-Check on `ENV_B`:** **`404 Not Found`** (대칭적 격리 검증 완료)

### Test 15: Remote Environment File API Inspection (`GET /v1beta/environments/{ENV_A}/files`)
- **`key1` (Owner):** `200 OK` (Directory metadata 및 workspace 경로 열람 성공)
- **`key2` (Cross-Tenant):** **`404 Not Found`** (`Environment 'bdf4fedca8f6f327ecaf3acda02435d2' not found.`)

---

## 5. Live Test Matrix

| Resource Boundary | Same Key (`key1` → `key1`) | Same Project Cross Key | Cross Project Key (`key1` → `key2`) |
|---|---|---|---|
| **Environment Access** | **PASS (200 OK)** | *PASS (동일 GCP Tenant)* | **FAIL (404 Not Found)** |
| **Interaction Continuity** | **PASS (200 OK)** | *PASS (동일 GCP Tenant)* | **FAIL (404 Not Found)** |
| **Remote Files API** | **PASS (200 OK)** | *PASS (동일 GCP Tenant)* | **FAIL (404 Not Found)** |

---

## 6. Architecture Implications for Phase 7

1. **Strict Tenant/Project Key Grouping:**
   - Key Pool은 평면적인 1차원 리스트(`key1..key10`)가 아니라, `tenant_id` 또는 `project_group` 메타데이터를 포함해야 합니다.
2. **Fail-over & Rollover Rule:**
   - 같은 테넌트 내의 예비 키로 롤오버할 때는 기존 `environment_id`와 `interaction_id`를 유지할 수 있습니다.
   - **이종 테넌트 키로 전환 시(Cross-Tenant Fallback):**
     - 기존 `environment_id`와 `previous_interaction_id`를 즉시 폐기(Session Reset).
     - 새 테넌트 키로 새 Environment 생성.
     - `WorkspaceSync`를 실행하여 로컬 최신 파일 트리를 원격 새 환경에 즉시 재동기화.
     - 새로운 Interaction 시작.
