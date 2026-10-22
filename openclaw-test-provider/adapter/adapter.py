#!/usr/bin/env python3
"""Antigravity Test API Adapter (별도 프로세스)

기존 /home/ubuntu/agy-router/cloud_wrapper.call_antigravity 를 재사용하여
Antigravity Cloud Interactions API를 로컬 OpenAI 호환 엔드포인트로 노출한다.

테스트 전용. 단일 테스트 API 키만 사용한다.
API 키는 환경변수 AGY_KEY_1 (또는 GEMINI_API_KEY)에서만 읽으며,
코드/로그/응답 어디에도 기록하지 않는다.

엔드포인트:
  GET  /v1/models               -> OpenAI 호환 모델 목록
  POST /v1/chat/completions     -> OpenAI 호환 채팅 (stream 지원)
  POST /v1/interactions         -> 레거시 직접 호출 (검증용)

세션 연속성:
  - 대화(첫 user 메시지) 별로 environment_id / previous_interaction_id 를 보관
  - 같은 대화의 연속 호출은 이전 상호작용을 이어받는다
  - 다른 대화는 키가 달라 상태가 섞이지 않는다
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
# 어댑터 자신 + 기존 agy-router 저장소 루트(cloud_wrapper.py 위치)를 import 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, _HERE)

from cloud_wrapper import call_antigravity  # noqa: E402
from key_pool import KeyPoolManager, get_api_key_by_index  # noqa: E402
from rollover_runner import RolloverRunner  # noqa: E402

# --- 단일 키(기존 경로 보존용) ---
AGY_KEY = os.environ.get("AGY_KEY_1") or os.environ.get("GEMINI_API_KEY")
if not AGY_KEY:
    # OpenClaw 설정 파일에서 google-interactions 키 fallback 조회
    try:
        cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            AGY_KEY = (
                cfg.get("models", {})
                .get("providers", {})
                .get("google-interactions", {})
                .get("apiKey")
            )
    except Exception:
        pass

# --- 멀티키 풀(신규): 2개 이상 발견 시 활성화 ---
_KEY_POOL = KeyPoolManager()
_ROLLOVER = RolloverRunner(_KEY_POOL)
try:
    _DISCOVERED = _KEY_POOL.discover_keys()
except Exception:
    _DISCOVERED = []
_MULTIKEY = os.environ.get("AGY_ADAPTER_MULTIKEY", "auto").lower() == "auto" and len(_DISCOVERED) > 1

if not AGY_KEY and not _DISCOVERED:
    print(
        "ERROR: no API key found (checked AGY_KEY_1 / GEMINI_API_KEY / "
        "AGY_KEY_N / GEMINI_API_KEY_N / ~/.bashrc / openclaw.json)",
        file=sys.stderr,
    )
    sys.exit(1)

PORT = int(os.environ.get("ANTIGRAVITY_ADAPTER_PORT", "8765"))
MODEL_ID = "antigravity-preview-09-2026"
MODEL_REF = f"antigravity-test/{MODEL_ID}"

# 대화별 세션 상태: conversation_key -> {environmentId, previousInteractionId}
_SESSIONS: dict[str, dict] = {}

# 무한루프 가드: 연속 function_call 최대 허용 횟수(초과 시 툴 없이 강제 텍스트 응답).
_MAX_FC_STREAK = int(os.environ.get("AGY_ADAPTER_MAX_FC_STREAK", "25"))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in (None, "text"):
                parts.append(str(p.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _conv_key(messages) -> str:
    """첫 user 메시지를 기준으로 대화 식별자 생성 (대화 간 격리)."""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            text = _text_of(m.get("content"))
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return "default"


# OpenClaw가 대화 데이터 뒤에 덧붙이는 런타임 컨텍스트 캐리어(비-사용자 텍스트)를 식별하는 마커.
# OpenClaw는 툴 결과 다음에 이 캐리어를 user 역할로 꼬리에 재배치한다
# (internal-runtime-context.ts: relocateCurrentRuntimeContextCarrierToTail).
_RUNTIME_CTX_MARKERS = (
    "OpenClaw runtime context:",
    "OpenClaw runtime context (internal):",
    "OpenClaw runtime context",
    "\u27e6openclaw:ctx\u27e7",
    "Continue the OpenClaw runtime event.",
    "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>",
    "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>",
)

_RUNTIME_CTX_RE = re.compile(
    r"(?:^|\n)(?:<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>[\s\S]*?(?:<<<END_OPENCLAW_INTERNAL_CONTEXT>>>|$)|OpenClaw runtime context(?: \(internal\))?:?[\s\S]*?(?:<<<END_OPENCLAW_INTERNAL_CONTEXT>>>|End OpenClaw runtime context\.?|$)|Conversation info: \u27e6openclaw:ctx\u27e7[\s\S]*?(?:End OpenClaw runtime context\.?|$)|Recent chat history: \u27e6openclaw:ctx\u27e7[\s\S]*?(?:End OpenClaw runtime context\.?|$))",
    re.IGNORECASE
)


def _clean_user_text(text: str) -> str:
    """사용자 메시지에서 OpenClaw 메타데이터/런타임 컨텍스트 블록을 제거하고 순수 발화만 추출."""
    t = (text or "").strip()
    if not t:
        return ""
    cleaned = _RUNTIME_CTX_RE.sub("", t).strip()
    lines = []
    for line in cleaned.splitlines():
        if any(m in line for m in _RUNTIME_CTX_MARKERS):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_runtime_context_text(text: str) -> bool:
    """OpenClaw가 생성한 Pure 런타임 컨텍스트/메타데이터 블록인지 판정 (사용자 발화 포함 시 False)."""
    t = (text or "").strip()
    if not t:
        return False
    # 순수 발화가 완전히 비어있고 런타임 마커만 존재하는 경우에만 Pure 런타임 컨텍스트로 취급
    cleaned = _clean_user_text(t)
    return len(cleaned) == 0 and any(m in t for m in _RUNTIME_CTX_MARKERS)


def _pending_tool_results(messages: list) -> list:
    """아직 Antigravity에 제출되지 않은 툴 결과 메시지 목록을 반환.

    OpenClaw는 assistant(tool_calls) -> tool(result) 뒤에 런타임 컨텍스트 user 캐리어를
    꼬리에 재배치하므로, 단순히 messages[-1].role == 'tool' 로 판정하면 툴 결과 턴을
    놓치고 무한루프에 빠진다. 따라서 '마지막 assistant(tool_calls)' 이후의 tool 메시지를
    런타임 컨텍스트 user 블록을 건너뛰며 수집한다. 진짜 새 user 턴이 끼어들면 [] 반환.
    """
    last_asst = -1
    for i, m in enumerate(messages):
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and m.get("tool_calls")
        ):
            last_asst = i
    if last_asst < 0:
        return []
    results = []
    for m in messages[last_asst + 1:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            results.append(m)
        elif role == "user" and _is_runtime_context_text(_text_of(m.get("content"))):
            continue  # 순수 런타임 컨텍스트 캐리어만 툴 결과 턴을 방해하지 않는다
        elif role == "user":
            return []  # 진짜 새 사용자 턴 -> 툴 결과 턴이 아님
    return results


def _find_assistant_tool_call(messages: list, tool_call_id: str, func_name: str):
    """assistant(tool_calls)에서 id/함수명이 일치하는 항목을 찾아 반환."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if tool_call_id and tc.get("id") == tool_call_id:
                return tc
            if func_name and fn.get("name") == func_name:
                return tc
    return None


def _extract_images_from_content(content) -> list[dict]:
    """OpenAI 호환 메시지 content에서 이미지 블록(data URI, URL, base64)을 추출하여
    Antigravity input 포맷({"type": "image", "mime_type": ..., "data": ...})으로 변환한다."""
    images: list[dict] = []
    if not isinstance(content, list):
        return images

    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "image_url":
            img_obj = part.get("image_url")
            url = img_obj.get("url", "") if isinstance(img_obj, dict) else str(img_obj or "")
            if not url:
                continue
            if url.startswith("data:"):
                try:
                    header, b64_data = url.split(",", 1)
                    mime_type = header.split(";")[0][5:] or "image/png"
                    images.append({
                        "type": "image",
                        "mime_type": mime_type,
                        "data": b64_data.strip(),
                    })
                except Exception:
                    pass
            elif url.startswith(("http://", "https://")):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        raw_bytes = resp.read()
                        mime_type = resp.headers.get_content_type() or "image/png"
                        if mime_type == "application/octet-stream":
                            guessed, _ = mimetypes.guess_type(url)
                            mime_type = guessed or "image/png"
                        images.append({
                            "type": "image",
                            "mime_type": mime_type,
                            "data": base64.b64encode(raw_bytes).decode("ascii"),
                        })
                except Exception:
                    pass
        elif ptype == "image":
            # Anthropic/OpenClaw style {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}} or direct {"data": ..., "mime_type": ...}
            src = part.get("source") if isinstance(part.get("source"), dict) else part
            b64_data = src.get("data")
            mime_type = src.get("media_type") or src.get("mime_type") or "image/png"
            if b64_data and isinstance(b64_data, str):
                images.append({
                    "type": "image",
                    "mime_type": mime_type,
                    "data": b64_data.strip(),
                })
        elif ptype == "input_image":
            b64_data = part.get("image_base64") or part.get("data")
            mime_type = part.get("mime_type") or part.get("media_type") or "image/png"
            if b64_data and isinstance(b64_data, str):
                images.append({
                    "type": "image",
                    "mime_type": mime_type,
                    "data": b64_data.strip(),
                })
    return images


# 단일 요청 입력 페이로드 상한(글자 수). 무료 티어 100k TPM 한도를 절대 넘지 않도록 방어
_MAX_PROMPT_CHARS = int(os.environ.get("AGY_ADAPTER_MAX_PROMPT_CHARS", "240000"))
_MAX_OLD_TOOL_RESULT_CHARS = int(os.environ.get("AGY_ADAPTER_MAX_OLD_TOOL_CHARS", "3500"))


def _extract_prompt(messages: list):
    """OpenClaw 전달 메시지들에서 실제 프롬프트/대화 내용 및 멀티모달 이미지를 추출.
    
    OpenClaw는 시스템 프롬프트, 대화 히스토리, 툴 결과, 런타임 메타데이터 블록(⟦openclaw:ctx⟧)을 함께 전달한다.
    단순 마지막 user 블록만 취하면 메타데이터 블록만 남아 질문이 증발할 수 있으므로,
    전체 메시지 흐름을 종합하여 Antigravity 모델에 유의미한 프롬프트를 구성한다.
    이미지가 포함된 경우 Antigravity 멀티모달 배열([{"type": "text", ...}, {"type": "image", ...}])로 반환한다.
    단일 호출이 무료 티어 100k 토큰 한도를 초과하지 않도록 오래된 대용량 툴 결과를 축약하고 전체 길이를 제한한다.
    """
    sys_blocks: list[str] = []
    conv_blocks: list[str] = []
    images: list[dict] = []

    # 마지막 4개 메시지는 최근 맥락이므로 툴 결과를 축약하지 않음
    recent_cutoff = max(0, len(messages) - 4)

    for idx, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content")
        raw_text = _text_of(content).strip()
        msg_images = _extract_images_from_content(content)
        if msg_images:
            images.extend(msg_images)
        if role == "system":
            if raw_text:
                sys_blocks.append(f"[System Instructions]\n{raw_text}")
        elif role == "assistant":
            tc = m.get("tool_calls")
            if raw_text:
                conv_blocks.append(f"[Assistant]\n{raw_text}")
            if tc:
                names = []
                for t in tc:
                    if isinstance(t, dict):
                        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
                        names.append(str(fn.get("name") or t.get("name") or "tool"))
                if names:
                    conv_blocks.append("[Assistant Tool Call] " + ", ".join(names))
        elif role == "tool":
            name = m.get("tool_name") or m.get("name") or "tool"
            if raw_text:
                if idx < recent_cutoff and len(raw_text) > _MAX_OLD_TOOL_RESULT_CHARS:
                    half = _MAX_OLD_TOOL_RESULT_CHARS // 2
                    trimmed = (
                        raw_text[:half]
                        + f"\n...[trimmed {len(raw_text) - _MAX_OLD_TOOL_RESULT_CHARS} chars]...\n"
                        + raw_text[-half:]
                    )
                    conv_blocks.append(f"[Tool Result: {name}]\n{trimmed}")
                else:
                    conv_blocks.append(f"[Tool Result: {name}]\n{raw_text}")
        elif role == "user":
            user_speech = _clean_user_text(raw_text)
            if user_speech:
                conv_blocks.append(f"[User]\n{user_speech}")
            elif msg_images:
                conv_blocks.append("[User]\n[Attached Image]")

    sys_text = "\n\n".join(sys_blocks).strip()
    conv_text = "\n\n".join(conv_blocks).strip()
    full_prompt = ("\n\n".join(x for x in (sys_text, conv_text) if x)).strip() or "Hello"

    # 만약 전체 프롬프트가 _MAX_PROMPT_CHARS를 초과하면 시스템 프롬프트 + 최신 대화 블록 위주로 유지
    if len(full_prompt) > _MAX_PROMPT_CHARS and conv_blocks:
        budget = max(30000, _MAX_PROMPT_CHARS - len(sys_text) - 200)
        kept_tail: list[str] = []
        used = 0
        for blk in reversed(conv_blocks):
            if used + len(blk) + 2 > budget and kept_tail:
                break
            if not kept_tail and len(blk) > budget:
                kept_tail.append(blk[-budget:])
                used += budget
                break
            kept_tail.append(blk)
            used += len(blk) + 2
        kept_tail.reverse()
        trimmed_conv = "[...Earlier conversation trimmed to fit 100k token limit...]\n\n" + "\n\n".join(kept_tail)
        full_prompt = ("\n\n".join(x for x in (sys_text, trimmed_conv) if x)).strip()

    if images:
        # 너무 많은 과거 이미지로 인한 페이로드 초과 방지를 위해 최근 5개 이미지만 전송
        recent_images = images[-5:]
        return [{"type": "text", "text": full_prompt}, *recent_images]
    return full_prompt


# Antigravity(interactions)가 받아들이는 최소 JSON Schema 키 화이트리스트.
# additionalProperties / $schema / default / format / oneOf / anyOf 등은 500을 유발하므로 전부 제거.
_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum"}


def _dbg(event: dict) -> None:
    """구조/에러 진단용 로그. 본문/키 값은 기록하지 않는다."""
    if not os.environ.get("AGY_ADAPTER_DEBUG"):
        return
    try:
        event["ts"] = time.time()
        with open("/tmp/agy_adapter_debug.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _clean_schema(obj, depth: int = 0):
    """JSON Schema를 Antigravity 허용 키만 남기고 재귀 정제."""
    if depth > 12:
        return {"type": "object", "properties": {}}
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k not in _SCHEMA_KEYS:
                continue
            if k == "type":
                if isinstance(v, list):
                    v = next((x for x in v if x != "null"), "string")
                if not isinstance(v, str) or not v:
                    v = "string"
                out["type"] = v
            elif k == "description":
                out[k] = str(v)[:1024]
            elif k == "required":
                if isinstance(v, list):
                    out[k] = [str(x) for x in v]
            elif k == "enum":
                if isinstance(v, list):
                    out[k] = v
            elif k == "properties":
                if isinstance(v, dict):
                    out[k] = {
                        str(pk): _clean_schema(pv, depth + 1)
                        for pk, pv in v.items()
                    }
            elif k == "items":
                out[k] = _clean_schema(v, depth + 1)
        if not out:
            return {"type": "object", "properties": {}}
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        # required는 실제 존재하는 property만 인정 (없는 필드 요구 시 500 위험)
        if isinstance(out.get("required"), list):
            props = out.get("properties") or {}
            out["required"] = [r for r in out["required"] if r in props]
            if not out["required"]:
                out.pop("required")
        return out
    if isinstance(obj, list):
        return [_clean_schema(x, depth + 1) for x in obj]
    return {"type": "string"}


def _format_tools_for_agy(tools: list) -> list:
    """OpenAI tools 스키마를 Antigravity tools 스키마로 변환."""
    agy_tools = []
    seen = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        params = _clean_schema(
            fn.get("parameters") or {"type": "object", "properties": {}}
        )
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        agy_tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(fn.get("description") or "Tool"),
                "parameters": params,
            }
        )
    return agy_tools


def _pending_function_calls(raw) -> list:
    """아직 결과가 제출되지 않은 function_call 스텝만 반환.

    Antigravity는 두 가지 응답 형태를 돌려준다:
      - status='requires_action': [function_call] 만 있고 우리 결과를 기다림 → 툴콜 반환해야 함
      - status='completed':      [function_call, function_result, ..., model_output]
                                  이미 도구를 서버측에서 실행하고 최종 답까지 냄
    후자에 function_result를 다시 제출하면 500('Internal error encountered.')이 난다.
    따라서 '결과가 아직 없는 function_call'만 pending으로 판정한다.
    """
    steps = (raw or {}).get("steps") or []
    answered = {
        s.get("call_id")
        for s in steps
        if isinstance(s, dict) and s.get("type") == "function_result"
    }
    return [
        s
        for s in steps
        if isinstance(s, dict)
        and s.get("type") == "function_call"
        and s.get("id") not in answered
    ]


def _get_target_model(model=None):
    """지정된 모델 또는 설정 파일의 모델을 반환."""
    if model:
        clean_model = model.split("/")[-1]
        if "gemini" in clean_model:
            return clean_model
    try:
        cfg_file = os.path.expanduser("~/.agy-router/config.json")
        if os.path.exists(cfg_file):
            with open(cfg_file, "r", encoding="utf-8") as f:
                c = json.load(f)
                if c.get("model"):
                    return c["model"]
    except Exception:
        pass
    return "gemini-4-argon"


def _call(messages, tools=None, model=None):
    key = _conv_key(messages)
    st = _SESSIONS.setdefault(key, {})

    target_model = _get_target_model(model)

    agy_tools = _format_tools_for_agy(tools) if tools else None

    # 루프 가드(방어적): 모델이 function_result를 받고도 계속 툴만 재요청하는 경우
    # (예: 어댑터/프로토콜 드리프트) 무한루프를 bounded로 만든다.
    # 임계치 초과 시 다음 호출은 툴 없이 강제 텍스트 응답.
    if st.get("force_no_tools"):
        _dbg({"event": "loop_guard_force_no_tools",
              "streak": st.get("fc_streak", 0)})
        agy_tools = None
        st.pop("force_no_tools", None)

    last_msg = messages[-1] if messages else {}
    # 툴 결과 턴 판정: 마지막 메시지가 아니라 '미제출 툴 결과'를 역방향 스캔한다.
    # (OpenClaw는 툴 결과 뒤에 런타임 컨텍스트 user 캐리어를 꼬리에 재배치하므로
    #  last_msg.role == 'tool' 로만 판정하면 무한루프에 빠진다.)
    pending_results = _pending_tool_results(messages)
    is_tool_result = bool(pending_results)
    last_msg = pending_results[-1] if pending_results else last_msg
    # 툴 체인 진행 중이면, 그 체인을 연 키로 고정(핀)한다.
    pinned = st.get("pin_key_ref")
    # 환경(대화 세션)이 살아있으면 그 소유 키. 테넌트가 다른 키로 가면 404가 난다.
    env_owner = st.get("env_key_ref") if st.get("environmentId") else None

    def _invoke(prompt, use_tools, tool_bound, pinned_key_ref=None):
        """풀 활성 시 롤오버 러너 경유, 비활성 시 기존 단일키 경로.

        pinned_key_ref가 주어지면(툴 체인 진행 중) 그 키로 고정 호출한다.
        """
        call_kwargs = dict(
            prompt=prompt,
            agent="antigravity-preview-09-2026",
            model=target_model,
            tools=use_tools,
            environment_id=st.get("environmentId"),
            previous_interaction_id=st.get("previousInteractionId"),
        )
        try:
            _pv = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
            _dbg({"event": "prompt", "kind": "str" if isinstance(prompt, str) else "list",
                  "len": len(_pv), "sha": hashlib.sha256(_pv.encode()).hexdigest()[:12],
                  "agent": call_kwargs.get("agent"), "env_in": (call_kwargs.get("environment_id") or "")[:12],
                  "prev_in": str(call_kwargs.get("previous_interaction_id"))[:20]})
        except Exception:
            pass
        if not _MULTIKEY:
            return call_antigravity(api_key=AGY_KEY, **call_kwargs)

        def _exec(raw_key):
            return call_antigravity(api_key=raw_key, **call_kwargs)

        return _ROLLOVER.run(
            _exec, tool_bound=tool_bound, pinned_key_ref=pinned_key_ref
        )
    _dbg(
        {
            "event": "call",
            "n_messages": len(messages),
            "roles": [m.get("role") for m in messages if isinstance(m, dict)][-6:],
            "n_tools": len(agy_tools or []),
            "tool_names": [t["name"] for t in (agy_tools or [])],
            "is_tool_result": is_tool_result,
            "model": target_model,
        }
    )

    if is_tool_result:
        pending_map = st.get("pending_calls") or {}
        payload_input = []
        for idx, tm in enumerate(pending_results):
            tc_id = tm.get("tool_call_id", "")
            fn_from_msg = tm.get("tool_name") or tm.get("name")
            if not fn_from_msg:
                tc = _find_assistant_tool_call(messages, tc_id, "")
                if tc is not None:
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    fn_from_msg = fn.get("name")
            mapped_name = pending_map.get(tc_id)
            func_name = fn_from_msg or mapped_name or st.get("last_func_name", "tool")
            if tc_id and tc_id in pending_map:
                real_call_id = tc_id
            elif len(pending_results) == 1 and st.get("last_call_id"):
                real_call_id = st.get("last_call_id")
            else:
                real_call_id = tc_id or st.get("last_call_id", "")

            content = tm.get("content", "")
            try:
                parsed_result = json.loads(content)
            except Exception:
                parsed_result = {"output": content}

            payload_input.append(
                {
                    "type": "function_result",
                    "name": func_name,
                    "call_id": real_call_id,
                    "result": parsed_result
                    if isinstance(parsed_result, dict)
                    else {"output": parsed_result},
                }
            )
        res = _invoke(payload_input, None, tool_bound=True, pinned_key_ref=pinned or env_owner)
        # 500 on a function_result means the interaction is already terminal
        # (Antigravity resolved the tool chain server-side). Do not surface a
        # hard error: drop the stale session and continue as a fresh text turn
        # carrying the full transcript (which already includes the tool result).
        if not res.get("success") and res.get("status_code") == 500:
            _dbg({"event": "function_result_terminal_recover"})
            st.pop("pin_key_ref", None)
            st.pop("environmentId", None)
            st.pop("previousInteractionId", None)
            st.pop("env_key_ref", None)
            res = _invoke(_extract_prompt(messages), agy_tools, tool_bound=False)
    else:
        # 새 텍스트 턴: 각 테넌트에 분산된 TPM 쿼터를 100% 활용하기 위해
        # 이전 세션 환경(environmentId) 및 키 고정(pin_key_ref)을 매 턴 초기화하고,
        # 전체 대화 히스토리 기반으로 키 풀(KeyPool)에서 LRU/라운드로빈 순환 호출한다.
        st.pop("pin_key_ref", None)
        st.pop("environmentId", None)
        st.pop("previousInteractionId", None)
        st.pop("env_key_ref", None)
        res = _invoke(_extract_prompt(messages), agy_tools, tool_bound=False, pinned_key_ref=None)

        # 404 발생 시 (타 테넌트 환경 ID 오염 또는 만료된 environment/interaction):
        # 세션 환경 캐시를 비우고 클린 상태로 즉시 1회 재시도
        if not res.get("success") and res.get("status_code") == 404:
            _dbg({"event": "404_clean_retry", "old_env": st.get("environmentId")})
            st.pop("environmentId", None)
            st.pop("previousInteractionId", None)
            st.pop("env_key_ref", None)
            res = _invoke(_extract_prompt(messages), agy_tools, tool_bound=False,
                          pinned_key_ref=None)

        # 도구가 있어 500 오류가 나면, 도구 없이라도 텍스트 응답을 보장(안전 폴백)
        if not res.get("success") and agy_tools and res.get("status_code") == 500:
            _dbg({"event": "fallback_no_tools", "status": res.get("status_code")})
            res2 = _invoke(_extract_prompt(messages), None, tool_bound=False)
            if res2.get("success"):
                res = res2

    # 핀 키 실패 처리.
    #  - 툴 체인 중: call_id/environment가 테넌트에 묶여 키 이동 불가 -> 체인/환경 드롭.
    #  - 일반 텍스트 턴인데 env 소유 키가 소진/무효(429/401/403): 환경을 드롭하고
    #    다른 키로 즉시 1회 재시도한다. 세션은 첫 user 메시지로 식별되고 전체
    #    트랜스크립트를 매 턴 재전송하므로 env를 새로 만들어도 맥락은 보존된다.
    if res.get("pin_failure"):
        _pf_status = res.get("status_code")
        _dbg({"event": "pin_failure_drop", "pin": pinned,
              "status": _pf_status, "tool_bound": is_tool_result})
        st.pop("pin_key_ref", None)
        st.pop("environmentId", None)
        st.pop("previousInteractionId", None)
        st.pop("env_key_ref", None)
        # 키 가용성 문제(쿼터 소진/권한)로 인한 실패면 사용자에게 429를 노출하지 않고
        # 새 키로 투명 재시도한다. (env를 이미 드롭했으므로 자유 순환 경로가 유효하며,
        # 툴 결과 턴이라도 _extract_prompt가 직전 툴 결과를 모두 포함하므로 맥락이 유지됨)
        if _pf_status in (401, 403, 429):
            _dbg({"event": "pin_quota_reroute", "status": _pf_status, "from_tool": is_tool_result})
            res = _invoke(_extract_prompt(messages), agy_tools, tool_bound=False,
                          pinned_key_ref=None)

    if not res.get("success"):
        _dbg(
            {
                "event": "error",
                "status": res.get("status_code"),
                "error": str(res.get("error"))[:300],
            }
        )

    if res.get("success"):
        _dbg({"event": "output", "status": (res.get("raw") or {}).get("status"),
              "steps": [s.get("type") for s in ((res.get("raw") or {}).get("steps") or [])],
              "out": str(res.get("output") or "")[:240]})
        if res.get("environment_id"):
            st["environmentId"] = res["environment_id"]
            # 환경 소유 키 고정: 이후 턴은 같은 테넌트 키로 보내야 404 방지
            if res.get("key_ref"):
                st["env_key_ref"] = res["key_ref"]
        if res.get("interaction_id"):
            st["previousInteractionId"] = res["interaction_id"]
            
        # Antigravity가 function_call을 '요구한 경우'에만 세션에 기록한다.
        # (status='completed' 응답의 이력 function_call은 이미 결과가 제출된 것)
        raw = res.get("raw") or {}
        pending_fcs = _pending_function_calls(raw)
        has_function_call = bool(pending_fcs)
        if has_function_call:
            st["pending_calls"] = {
                fc.get("id"): fc.get("name", "tool")
                for fc in pending_fcs
                if fc.get("id")
            }
            last_fc = pending_fcs[-1]
            if last_fc.get("id"):
                st["last_call_id"] = last_fc.get("id")
            if last_fc.get("name"):
                st["last_func_name"] = last_fc.get("name")
        else:
            st.pop("pending_calls", None)

        # 핀 수명주기:
        #  - function_call 발행 -> 이 체인을 연 키로 고정
        #  - 최종 텍스트 응답 -> 핀 해제(다음 턴은 자유 순환)
        if has_function_call:
            if not st.get("pin_key_ref") and res.get("key_ref"):
                st["pin_key_ref"] = res["key_ref"]
                _dbg({"event": "pin_set", "pin": res["key_ref"],
                      "env": (st.get("environmentId") or "")[:12]})
            # 연속 function_call 스트릭 추적 -> 루프 가드
            st["fc_streak"] = int(st.get("fc_streak", 0)) + 1
            if st["fc_streak"] >= _MAX_FC_STREAK:
                st["force_no_tools"] = True
                _dbg({"event": "loop_guard_armed",
                      "streak": st["fc_streak"]})
        else:
            st["fc_streak"] = 0
            if st.pop("pin_key_ref", None):
                _dbg({"event": "pin_released"})

    return res


def _error_text(res) -> str:
    return f"Error {res.get('status_code')}: {res.get('error')}"


def _openai_usage(res: dict, messages: list) -> dict:
    u = (res.get("usage") or (res.get("raw") or {}).get("usage") or {}) if isinstance(res, dict) else {}
    if not isinstance(u, dict):
        u = {}
    prompt_tokens = int(
        u.get("prompt_tokens")
        or u.get("total_input_tokens")
        or u.get("input_tokens")
        or u.get("prompt_token_count")
        or 0
    )
    completion_tokens = int(
        u.get("completion_tokens")
        or u.get("total_output_tokens")
        or u.get("output_tokens")
        or u.get("candidates_token_count")
        or 0
    )
    cached_tokens = int(
        u.get("total_cached_tokens")
        or u.get("cached_tokens")
        or 0
    )
    if prompt_tokens <= 0:
        try:
            prompt_tokens = max(1, len(_extract_prompt(messages)) // 4)
        except Exception:
            prompt_tokens = 0
    if completion_tokens <= 0:
        out_text = str((res or {}).get("output") or "")
        if out_text:
            completion_tokens = max(1, len(out_text) // 4)
    total_tokens = int(u.get("total_tokens") or u.get("total_token_count") or (prompt_tokens + completion_tokens))
    usage_obj = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if cached_tokens > 0:
        usage_obj["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return usage_obj


class Handler(BaseHTTPRequestHandler):
    """HTTP 요청 핸들러. 키 값을 로그에 남기지 않는다."""

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- OpenAI 호환: 모델 목록 ---
    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "owned_by": "antigravity-test",
                        }
                    ],
                },
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw) if raw else {}

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            data = self._read_json()
        except Exception:
            self._send_json(400, {"error": {"message": "invalid json body"}})
            return

        if path == "/v1/interactions":
            self._handle_interactions(data)
        elif path == "/v1/chat/completions":
            self._handle_chat(data)
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    # --- 레거시 직접 호출 ---
    def _handle_interactions(self, data: dict) -> None:
        _kw = dict(
            prompt=data.get("prompt", ""),
            environment_id=data.get("environment_id"),
            previous_interaction_id=data.get("previous_interaction_id"),
            model=data.get("model"),
        )
        if not _MULTIKEY:
            result = call_antigravity(api_key=AGY_KEY, **_kw)
        else:
            result = _ROLLOVER.run(lambda k: call_antigravity(api_key=k, **_kw))
        resp = {
            "success": result.get("success", False),
            "status_code": result.get("status_code"),
            "output": result.get("output", ""),
            "environment_id": result.get("environment_id"),
            "interaction_id": result.get("interaction_id"),
            "usage": result.get("usage", {}),
        }
        if not result.get("success"):
            resp["error"] = {"message": str(result.get("error"))}
        self._send_json(200, resp)

    # --- OpenAI 호환 채팅 ---
    def _handle_chat(self, data: dict) -> None:
        messages = data.get("messages") or []
        tools = data.get("tools")
        res = _call(messages, tools=tools, model=data.get("model"))
        if data.get("stream"):
            self._stream_openai(res, messages)
        else:
            self._json_openai(res, messages)

    def _json_openai(self, res, messages) -> None:
        key = _conv_key(messages)
        st = _SESSIONS.setdefault(key, {})

        # Antigravity requires_action (function_call) 확인
        # status='completed' 이면 이미 툴 체인이 서버측에서 끝난 것이므로
        # 툴콜을 반환하지 않고 최종 텍스트를 돌려준다(재제출 시 500 방지).
        raw = res.get("raw") or {}
        function_calls = _pending_function_calls(raw)

        if function_calls:
            tool_calls = []
            pending_map = {}
            for fc in function_calls:
                cid_fc = fc.get("id") or ("call_" + uuid.uuid4().hex[:12])
                fname = fc.get("name", "")
                fargs = fc.get("arguments") or {}
                pending_map[cid_fc] = fname
                st["last_func_name"] = fname
                st["last_call_id"] = cid_fc
                tool_calls.append(
                    {
                        "id": cid_fc,
                        "type": "function",
                        "function": {
                            "name": fname,
                            "arguments": json.dumps(fargs, ensure_ascii=False)
                            if isinstance(fargs, dict)
                            else str(fargs),
                        },
                    }
                )
            st["pending_calls"] = pending_map
            self._send_json(
                200,
                {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL_REF,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": tool_calls,
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": _openai_usage(res, messages),
                },
            )
            return

        # 일반 텍스트 완료
        content = (res.get("output") or "") if res.get("success") else _error_text(res)
        self._send_json(
            200,
            {
                "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_REF,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _openai_usage(res, messages),
            },
        )

    def _stream_openai(self, res, messages) -> None:
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        key = _conv_key(messages)
        st = _SESSIONS.setdefault(key, {})
        usage_obj = _openai_usage(res, messages)

        # Antigravity requires_action (function_call) 확인
        raw = res.get("raw") or {}
        function_calls = _pending_function_calls(raw)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish=None, usage=None) -> None:
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_REF,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
            if usage is not None:
                payload["usage"] = usage
            self.wfile.write(
                ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode(
                    "utf-8"
                )
            )
            self.wfile.flush()

        if function_calls:
            stream_tool_calls = []
            pending_map = {}
            for idx, fc in enumerate(function_calls):
                cid_fc = fc.get("id") or ("call_" + uuid.uuid4().hex[:12])
                fname = fc.get("name", "")
                fargs = fc.get("arguments") or {}
                pending_map[cid_fc] = fname
                st["last_func_name"] = fname
                st["last_call_id"] = cid_fc
                args_str = (
                    json.dumps(fargs, ensure_ascii=False)
                    if isinstance(fargs, dict)
                    else str(fargs)
                )
                stream_tool_calls.append(
                    {
                        "index": idx,
                        "id": cid_fc,
                        "type": "function",
                        "function": {
                            "name": fname,
                            "arguments": args_str,
                        },
                    }
                )
            st["pending_calls"] = pending_map

            # 스트림 tool_calls 전송
            chunk(
                {
                    "role": "assistant",
                    "tool_calls": stream_tool_calls,
                },
                finish="tool_calls",
                usage=usage_obj,
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        chunk({"role": "assistant", "content": ""})
        if res.get("success"):
            text = res.get("output") or ""
            for i in range(0, len(text), 8):
                chunk({"content": text[i : i + 8]})
        else:
            chunk({"content": _error_text(res)})
        chunk({}, "stop", usage=usage_obj)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format, *args):  # noqa: ARG002
        # 요청 로그에 키나 본문이 누출되지 않도록 기본 로그 억제
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Antigravity Test Adapter listening on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
