"""Service booking agent with Bedrock-assisted extraction and reply generation."""
from __future__ import annotations

import re
from datetime import date

from ..config import get_settings
from ..services import catalog
from ..services.conversation_memory import MEMORY
from . import llm, nlu, tools
from .page_help import (
    answer_page_question,
    build_page_tool_request,
    has_explicit_page_intent,
    looks_like_page_question,
)

DECIMAL_NUMBER_RE = re.compile(r"\d+\.\d+")

CONFIRM_WORDS = ("確認", "確定", "好", "可以", "沒問題", "送出", "ok", "OK", "yes", "Yes")
DENY_WORDS = ("不要", "不用", "取消", "改一下", "先不要", "no", "No")

SERVICE_DISPLAY_NAMES = {
    "plumbing_repair": "水電修繕",
    "washing_machine_cleaning": "洗衣機清洗",
    "air_conditioner_cleaning": "冷氣清洗",
    "home_cleaning": "居家清潔",
}

FIELD_DISPLAY_NAMES = {
    "issue_description": "問題描述",
    "preferred_date": "希望日期",
    "preferred_time_slot": "希望時段",
    "address": "服務地址",
    "phone": "聯絡電話",
    "quantity": "數量",
    "hours": "服務時數",
    "machine_type": "洗衣機類型",
}

SELECT_ALIASES = {
    "MORNING": ("MORNING", "上午", "早上"),
    "AFTERNOON": ("AFTERNOON", "下午"),
    "EVENING": ("EVENING", "晚上", "夜間"),
    "TOP_LOAD": ("TOP_LOAD", "直立式"),
    "FRONT_LOAD": ("FRONT_LOAD", "滾筒式"),
}

SELECT_DISPLAY_NAMES = {
    "MORNING": "上午",
    "AFTERNOON": "下午",
    "EVENING": "晚上",
    "TOP_LOAD": "直立式",
    "FRONT_LOAD": "滾筒式",
}

RULE_SERVICE_KEYWORDS = (
    ("plumbing_repair", ("水電", "修繕", "漏水", "插座", "燈具", "浴室", "維修")),
    ("washing_machine_cleaning", ("洗衣機", "清洗", "滾筒式", "直立式", "內槽")),
    ("air_conditioner_cleaning", ("冷氣", "清洗", "保養", "壁掛式", "室內機")),
    ("home_cleaning", ("清潔", "居家", "打掃", "整理", "到府")),
)


def _is_yes(text: str) -> bool:
    normalized = text.strip()
    return any(normalized == word or normalized.startswith(word) for word in CONFIRM_WORDS) and not _is_no(normalized)


def _is_no(text: str) -> bool:
    normalized = text.strip()
    return any(normalized == word or normalized.startswith(word) for word in DENY_WORDS)


def _judge_reply(question: str, text: str) -> str:
    verdict = llm.interpret_yes_no(question, text)
    if verdict is not None:
        return verdict
    if _is_yes(text):
        return "yes"
    if _is_no(text):
        return "no"
    return "unclear"


def _display_service_name(service_id: str | None, fallback: str | None = None) -> str:
    if service_id and service_id in SERVICE_DISPLAY_NAMES:
        return SERVICE_DISPLAY_NAMES[service_id]
    return fallback or "服務"


def _display_field_label(field_id: str, fields: list[dict]) -> str:
    field = next((item for item in fields if item["id"] == field_id), None)
    return FIELD_DISPLAY_NAMES.get(field_id) or (field or {}).get("label") or field_id


def _display_value(field_id: str, value, fields: list[dict]) -> str:
    if isinstance(value, str):
        value = SELECT_DISPLAY_NAMES.get(value, catalog.SELECT_LABELS.get(value, value))
    label = _display_field_label(field_id, fields)
    unit = " 台" if field_id == "quantity" else " 小時" if field_id == "hours" else ""
    return f"{label}：{value}{unit}"


def _build_summary_text(state: dict) -> str:
    fields = state["service_schema"]["fields"]
    lines = ["請確認以下申請內容：", f"服務：{_display_service_name(state['service_id'], state['service_name'])}"]
    for field in fields:
        if field["id"] in state["collected_fields"]:
            lines.append(_display_value(field["id"], state["collected_fields"][field["id"]], fields))
    lines.append("")
    lines.append("如果資料正確請直接回覆「確認送出」，如果要修改請直接告訴我要改哪一項。")
    return "\n".join(lines)


def _build_field_question(field: dict) -> str:
    field_id = field["id"]
    if field_id == "preferred_date":
        return "你希望安排哪一天服務呢？請提供日期。"
    if field_id == "preferred_time_slot":
        return "你希望安排哪個時段服務呢？"
    if field_id == "address":
        return "請提供服務地址。"
    if field_id == "phone":
        return "請提供聯絡電話。"
    if field["id"] == "issue_description":
        return "請描述你要處理的問題，例如漏水位置、設備故障或想清洗的項目。"
    if field["id"] == "preferred_date":
        return "你希望安排哪一天服務呢？"
    if field["id"] == "preferred_time_slot":
        return "你比較方便的時段是上午、下午，還是晚上呢？"
    if field["id"] == "address":
        return "請提供完整的服務地址，方便安排人員前往。"
    if field["id"] == "phone":
        return "請提供方便聯絡的電話號碼。"
    if field["id"] == "quantity":
        return "這次需要處理幾台呢？"
    if field["id"] == "hours":
        return "這次預計需要幾小時的服務呢？"
    if field["id"] == "machine_type":
        return "請問是直立式還是滾筒式洗衣機呢？"
    return field.get("question") or f"請提供{field.get('label') or field['id']}。"


def _recompute_missing(state: dict) -> None:
    fields = state["service_schema"]["fields"]
    state["missing_fields"] = [
        field["id"]
        for field in fields
        if field.get("required") and field["id"] not in state["collected_fields"]
    ]


def _compress_recent_events(events: list[dict] | None, latest_user_message: str | None = None) -> list[dict]:
    recent = list(events or [])[-6:]
    compact = [
        {"role": event.get("role", ""), "content": str(event.get("content", ""))[:200]}
        for event in recent
    ]
    if latest_user_message:
        compact.append({"role": "USER", "content": latest_user_message[:200]})
    return compact[-6:]


def _short_term_context(state: dict, events: list[dict] | None, latest_user_message: str | None = None) -> str:
    recent = _compress_recent_events(events, latest_user_message)
    state["short_term_memory"] = recent
    if not recent:
        return "None"
    return "\n".join(f"{item['role']}: {item['content']}" for item in recent)


def _short_term_context_from_state(state: dict) -> str:
    recent = state.get("short_term_memory") or []
    if not recent:
        return "None"
    return "\n".join(f"{item['role']}: {item['content']}" for item in recent)


def _long_term_memory_context(actor_id: str, query: str) -> str:
    try:
        return MEMORY.get_long_term_context(actor_id, query)
    except Exception:
        return "None"


def _safe_memory_snapshot(actor_id: str) -> dict:
    try:
        return MEMORY.get_memory_snapshot(actor_id)
    except Exception:
        return {"preferences": {}, "long_term_memory": {}}


def _safe_preferences(actor_id: str) -> dict:
    return _safe_memory_snapshot(actor_id).get("preferences") or {}


def _looks_like_memory_question(text: str) -> bool:
    hints = (
        "記得",
        "記住",
        "之前",
        "上次",
        "我的地址",
        "我的電話",
        "我的資料",
        "上次地址",
        "上次電話",
    )
    return any(hint in text for hint in hints)


def _reply_from_memory(actor_id: str) -> str | None:
    snapshot = _safe_memory_snapshot(actor_id)
    prefs = snapshot.get("preferences") or {}
    memory = snapshot.get("long_term_memory") or {}
    lines: list[str] = []

    if memory.get("last_service_name"):
        lines.append(f"你上次申請的服務是：{memory['last_service_name']}")
    if prefs.get("last_address"):
        lines.append(f"上次地址：{prefs['last_address']}")
    if prefs.get("last_phone"):
        lines.append(f"上次電話：{prefs['last_phone']}")
    if prefs.get("preferred_time_slot"):
        slot = SELECT_DISPLAY_NAMES.get(prefs["preferred_time_slot"], prefs["preferred_time_slot"])
        lines.append(f"常用時段：{slot}")
    if memory.get("last_request_summary"):
        lines.append(f"最近一次案件摘要：{memory['last_request_summary']}")

    if not lines:
        return None

    lines.append("如果你要沿用其中的資料，我也可以直接幫你帶入新的申請。")
    return "\n".join(lines)


def current_active_field(state: dict) -> str | None:
    if state.get("pending_pref_field"):
        return state["pending_pref_field"]
    missing_fields = state.get("missing_fields") or []
    return missing_fields[0] if missing_fields else None


def build_form_schema(state: dict) -> dict | None:
    service_schema = state.get("service_schema")
    if not service_schema:
        return None
    return {
        "service_id": state.get("service_id"),
        "service_name": state.get("service_name"),
        "fields": [
            {
                "id": field["id"],
                "label": _display_field_label(field["id"], service_schema["fields"]),
                "type": field.get("type", "text"),
                "required": bool(field.get("required")),
                "options": list(field.get("options", [])),
            }
            for field in service_schema["fields"]
        ],
    }


def build_form_draft(state: dict) -> dict | None:
    service_schema = state.get("service_schema")
    if not service_schema:
        return None

    fields = service_schema["fields"]
    values = {
        field["id"]: state.get("collected_fields", {}).get(field["id"])
        for field in fields
    }
    return {
        "service_id": state.get("service_id"),
        "service_name": state.get("service_name"),
        "status": state.get("status"),
        "request_id": state.get("request_id"),
        "fields": values,
        "missing_fields": list(state.get("missing_fields", [])),
        "active_field": current_active_field(state),
        "ready_for_confirmation": bool(not state.get("missing_fields")),
    }


def apply_form_patch(actor_id: str, state: dict, form_fields: dict) -> dict:
    service_schema = state.get("service_schema")
    if not service_schema:
        return _reply(state, "目前還沒有選定服務，請先告訴我你想申請哪一種服務。")

    fields = service_schema["fields"]
    field_map = {field["id"]: field for field in fields}
    invalid_labels: list[str] = []
    changed = False

    for field_id, raw_value in (form_fields or {}).items():
        field = field_map.get(field_id)
        if not field:
            continue

        if raw_value in (None, ""):
            if field_id in state["collected_fields"]:
                state["collected_fields"].pop(field_id, None)
                changed = True
            continue

        normalized = _normalize_field_value(field, raw_value, str(raw_value))
        if normalized is None:
            invalid_labels.append(_display_field_label(field_id, fields))
            continue

        if state["collected_fields"].get(field_id) != normalized:
            state["collected_fields"][field_id] = normalized
            changed = True

    state["awaiting_confirmation"] = False
    state["pending_pref_field"] = None
    state["pending_pref_value"] = None
    state["pending_pref_question"] = None
    _recompute_missing(state)

    result = _continue_collection(actor_id, state, latest_user_message="已更新表單資料")
    if invalid_labels:
        result["reply"] = f"有幾項資料格式不正確：{'、'.join(invalid_labels)}。\n{result['reply']}"
    elif changed:
        result["reply"] = f"表單資料已更新。\n{result['reply']}"
    else:
        result["reply"] = f"目前沒有需要變更的欄位。\n{result['reply']}"
    return result


def _normalize_select(raw_value: str, options: list[str]) -> str | None:
    if raw_value in options:
        return raw_value

    normalized = raw_value.strip().upper()
    for option, aliases in SELECT_ALIASES.items():
        if option in options and any(alias.upper() in normalized for alias in aliases):
            return option
    return None


def _normalize_field_value(field: dict, value, original_text: str):
    field_id = field["id"]
    if value in (None, ""):
        return None

    if field["type"] == "number":
        if DECIMAL_NUMBER_RE.search(str(value)) or DECIMAL_NUMBER_RE.search(original_text):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            return int(value) if value > 0 and float(value).is_integer() else None
        if field_id == "hours":
            return nlu.parse_hours(str(value)) or nlu.parse_hours(original_text)
        return (
            nlu.parse_quantity(str(value), unit_chars="台個")
            or nlu.parse_number(str(value))
            or nlu.parse_quantity(original_text, unit_chars="台個")
        )

    if field["type"] == "date":
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                normalized_date = date.fromisoformat(value)
                if normalized_date >= date.today():
                    return value
            except ValueError:
                pass
        return nlu.parse_date(str(value), today=date.today()) or nlu.parse_date(original_text, today=date.today())

    if field["type"] == "select":
        if field_id == "preferred_time_slot":
            return (
                _normalize_select(str(value), field.get("options", []))
                or nlu.parse_time_slot(str(value))
                or nlu.parse_time_slot(original_text)
            )
        if field_id == "machine_type":
            return (
                _normalize_select(str(value), field.get("options", []))
                or nlu.parse_machine_type(str(value))
                or nlu.parse_machine_type(original_text)
            )
        return _normalize_select(str(value), field.get("options", []))

    if field_id == "phone":
        return nlu.parse_phone(str(value)) or nlu.parse_phone(original_text)

    if field_id == "address":
        return nlu.parse_address(str(value)) or nlu.parse_address(original_text) or str(value).strip()

    text = str(value).strip()
    return text or None


def _message_matches_service(text: str, service_id: str, services: list[dict]) -> bool:
    service = next((item for item in services if item["id"] == service_id), None)
    if not service:
        return False

    keywords = list(service.get("keywords", []))
    if service.get("name"):
        keywords.append(service["name"])
    if service.get("description"):
        keywords.extend([part for part in re.split(r"[、，。\s]+", service["description"]) if len(part) >= 2])
    return any(keyword and keyword in text for keyword in keywords)


def _detect_service(text: str, services: list[dict], short_term_context: str, long_term_context: str) -> str | None:
    valid_ids = {service["id"] for service in services}
    llm_choice = llm.choose_service(
        text,
        services,
        short_term_memory=short_term_context,
        long_term_memory=long_term_context,
    )
    if llm_choice in valid_ids and _message_matches_service(text, llm_choice, services):
        return llm_choice

    return _rule_detect_service(text, services)


def _rule_detect_service(text: str, services: list[dict]) -> str | None:
    """不呼叫 LLM 的服務判斷（Bedrock 不可用時的主要路徑）。"""
    valid_ids = {service["id"] for service in services}

    # 依關鍵字命中權重挑最像的服務，不能用清單順序決定：
    # 「冷氣清洗」同時命中 washing_machine_cleaning 的「清洗」，
    # 若照順序取第一個命中就會被判成洗衣機清洗。
    scored = [
        (
            service_id,
            sum(len(keyword) for keyword in keywords if keyword in text),
        )
        for service_id, keywords in RULE_SERVICE_KEYWORDS
        if service_id in valid_ids
    ]
    if scored:
        # 分數相同時 max 會保留清單中較前面的項目。
        best_service_id, best_score = max(scored, key=lambda item: item[1])
        if best_score > 0:
            return best_service_id

    rule_choice, _ = nlu.detect_service(text)
    return rule_choice if rule_choice in valid_ids else None


def _extract_fields(actor_id: str, state: dict, text: str, events: list[dict] | None) -> dict:
    fields = state["service_schema"]["fields"]
    short_term_context = _short_term_context(state, events, text)
    long_term_context = _long_term_memory_context(actor_id, text)
    form_schema = build_form_schema(state)
    form_draft = build_form_draft(state)

    found: dict = {}
    llm_fields = llm.extract_fields(
        message=text,
        service_name=_display_service_name(state["service_id"], state["service_name"]),
        fields=fields,
        collected_fields=state["collected_fields"],
        form_schema=form_schema,
        form_draft=form_draft,
        short_term_memory=short_term_context,
        long_term_memory=long_term_context,
    )

    # Bedrock 不可用（或這一輪沒抓到欄位）時退回規則式 NLU，
    # 否則整段對話會永遠問同一個欄位。LLM 的判斷優先，規則只補沒抓到的欄位。
    rule_fields = nlu.extract_fields(
        state["service_id"] or "",
        fields,
        text,
        state["collected_fields"],
    )

    active_field_id = current_active_field(state)
    for field in fields:
        field_id = field["id"]
        if field_id in state["collected_fields"]:
            continue
        raw_value = llm_fields.get(field_id)
        if raw_value is None:
            raw_value = rule_fields.get(field_id)
        if raw_value is None and field_id == active_field_id and _takes_whole_message(field, text):
            # 自由填寫欄位（問題描述、地址…）被問到時，整句就是答案（nlu.extract_fields 的預留行為）。
            raw_value = text
        if raw_value is None:
            continue
        normalized = _normalize_field_value(field, raw_value, text)
        if normalized is not None:
            found[field_id] = normalized

    return found


def _takes_whole_message(field: dict, text: str) -> bool:
    """被問到的自由填寫欄位可以直接把整句話當答案。"""
    if field.get("type") != "text" or field["id"] == "phone":
        return False
    stripped = text.strip()
    return bool(stripped) and not _is_yes(stripped) and not _is_no(stripped)


def _fallback_reply(state: dict, phase: str, **kwargs) -> str:
    if phase == "completed":
        service_name = _display_service_name(state.get("service_id"), state.get("service_name"))
        request_id = state.get("request_id") or "目前案件"
        return (
            f"你目前已經有一筆 {service_name} 案件，案件編號是 {request_id}。"
            "如果你想查看進度，我可以協助說明；如果你要新增別的服務，也可以直接告訴我你要預約哪一種。"
        )
    if phase == "service_catalog_error":
        return "我現在暫時無法讀取服務清單，請稍後再試。"
    if phase == "service_not_understood":
        services = kwargs.get("service_options") or []
        service_text = "、".join(services) if services else "水電修繕、洗衣機清洗、冷氣清洗、居家清潔"
        return f"我目前只支援這幾種服務：{service_text}。你可以直接告訴我想預約哪一種。"
    if phase == "service_schema_error":
        return "我現在暫時無法讀取這項服務的表單，請稍後再試。"
    if phase == "reuse_preference":
        value = kwargs.get("preferred_value", "")
        label = kwargs.get("missing_field_label", "資料")
        return f"我這邊有你上次使用的{label}：{value}。這次要沿用嗎？"
    if phase == "collect_field":
        question = kwargs.get("missing_field_question")
        return question or "請再補充下一項必填資料。"
    if phase == "confirm":
        return kwargs.get("summary") or _build_summary_text(state)
    if phase == "confirmation_retry":
        return "如果資料正確請回覆「確認送出」，如果要修改請直接告訴我哪一項要改。"
    if phase == "submit_error":
        error_message = kwargs.get("error_message") or "送出失敗"
        return f"抱歉，這次送出沒有成功，原因是：{error_message}。你可以稍後再試，或直接把要修改的資料告訴我。"
    if phase == "submit_success":
        request_id = kwargs.get("request_id", "")
        return f"已幫你建立案件 {request_id}。案件已送出，接下來可以到我的服務查看最新進度。"
    return "我先幫你整理需求，你也可以直接告訴我下一個要補的資訊。"


def _model_reply(actor_id: str, state: dict, phase: str, latest_user_message: str = "", **kwargs) -> str:
    if not llm.is_available():
        return _fallback_reply(state, phase, **kwargs)

    fields = (state.get("service_schema") or {}).get("fields", [])
    missing_fields = state.get("missing_fields") or []
    missing_field_id = missing_fields[0] if missing_fields else None
    missing_field_label = _display_field_label(missing_field_id, fields) if missing_field_id else ""
    missing_field_question = ""
    if missing_field_id:
        field = next((item for item in fields if item["id"] == missing_field_id), None)
        if field:
            missing_field_question = _build_field_question(field)

    short_term_memory = _short_term_context_from_state(state)
    long_term_query = latest_user_message or state.get("service_name") or state.get("service_id") or "服務預約"
    long_term_memory = _long_term_memory_context(actor_id, long_term_query)

    reply = llm.compose_reply(
        phase=phase,
        latest_user_message=latest_user_message,
        service_name=_display_service_name(state.get("service_id"), state.get("service_name")),
        collected_fields=state.get("collected_fields", {}),
        missing_field_label=kwargs.get("missing_field_label", missing_field_label),
        missing_field_question=kwargs.get("missing_field_question", missing_field_question),
        summary=kwargs.get("summary", ""),
        preferred_value=kwargs.get("preferred_value", ""),
        request_id=kwargs.get("request_id", ""),
        request_status=kwargs.get("request_status", ""),
        error_message=kwargs.get("error_message", ""),
        service_options=kwargs.get("service_options", []),
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
    )
    return reply or _fallback_reply(state, phase, **kwargs)


def new_state() -> dict:
    return {
        "service_id": None,
        "service_name": None,
        "service_schema": None,
        "collected_fields": {},
        "missing_fields": [],
        "awaiting_confirmation": False,
        "pending_pref_field": None,
        "pending_pref_value": None,
        "pending_pref_question": None,
        "asked_pref_fields": [],
        "request_id": None,
        "status": "COLLECTING_INFORMATION",
        "short_term_memory": [],
    }


def _available_services(auth_token: str | None = None) -> list[dict] | None:
    result = tools.call("list_services", {}, auth_token=auth_token)
    if result.get("success", True) and isinstance(result.get("services"), list):
        return result["services"]
    if get_settings().use_mock:
        return catalog.list_services()
    return None


def _service_schema(service_id: str, auth_token: str | None = None) -> dict | None:
    result = tools.call("get_service_schema", {"service_id": service_id}, auth_token=auth_token)
    if result.get("success", True) and isinstance(result.get("fields"), list):
        return {
            "service_id": result.get("service_id", service_id),
            "title": result.get("title") or _display_service_name(service_id),
            "fields": result.get("fields", []),
        }
    if get_settings().use_mock:
        return catalog.get_service_schema(service_id)
    return None


def _looks_like_new_service_request(text: str) -> bool:
    hints = ("我要", "我想", "想要", "預約", "安排", "需要", "服務", "清洗", "清潔", "維修", "修繕", "打掃")
    return any(hint in text for hint in hints)


def _page_help_reply(text: str, current_page_id: str | None, auth_token: str | None) -> str | None:
    tool_request = build_page_tool_request(text, current_page_id=current_page_id)
    tool_payload: dict | None = None

    if tool_request:
        tool_name, params = tool_request
        result = tools.call(tool_name, params, auth_token=auth_token)
        if result.get("success"):
            tool_payload = result

    if tool_payload and llm.is_available():
        llm_reply = llm.compose_page_help_reply(
            latest_user_message=text,
            current_page_id=current_page_id or "",
            tool_payload=tool_payload,
        )
        if llm_reply:
            return llm_reply

    return answer_page_question(text, current_page_id=current_page_id, tool_payload=tool_payload)


def _invalid_number_field_message(state: dict, latest_user_message: str) -> str | None:
    if not DECIMAL_NUMBER_RE.search(latest_user_message):
        return None
    missing_fields = state.get("missing_fields") or []
    if not missing_fields:
        return None
    field_id = missing_fields[0]
    if field_id == "quantity":
        return "數量需要填整數，例如 1 台或 2 台，不能填 0.1 台。"
    if field_id == "hours":
        return "時數需要填整數，例如 2 小時或 3 小時，不能填 0.5 小時。"
    return None


def _should_answer_page_question(
    state: dict,
    text: str,
    current_page_id: str | None,
    services: list[dict] | None,
) -> bool:
    """只有真的在問頁面／導覽時才切去頁面說明。

    頁面關鍵字很容易誤命中：「滾筒式」是欄位答案、「浴室漏水想找人修」是預約需求，
    兩者都會命中頁面關鍵字。以前只要命中就回導覽說明，
    使用者因此既開不了單、也填不完表單。
    """
    if not looks_like_page_question(text, current_page_id=current_page_id):
        return False
    if has_explicit_page_intent(text, current_page_id=current_page_id):
        return True

    # 只命中關鍵字：填表中一律當成欄位答案。
    if state.get("service_id") and not state.get("request_id"):
        return False
    # 還沒選服務：這句話本身就能判斷出服務時，當成預約需求處理。
    return not _rule_detect_service(text, services or [])


def handle_message(
    actor_id: str,
    session_id: str,
    state: dict,
    message: str,
    events: list[dict] | None = None,
    current_page_id: str | None = None,
    auth_token: str | None = None,
) -> dict:
    text = message.strip()

    if looks_like_page_question(text, current_page_id=current_page_id):
        # 只有還沒選服務時才需要服務清單來判斷這句是預約需求還是導覽問題。
        services = None if state.get("service_id") else _available_services(auth_token)
        if _should_answer_page_question(state, text, current_page_id, services):
            page_reply = _page_help_reply(text, current_page_id=current_page_id, auth_token=auth_token)
            if page_reply:
                return _reply(state, page_reply)

    if state.get("request_id"):
        services = _available_services(auth_token)
        if services is None:
            return _reply(state, _model_reply(actor_id, state, "service_catalog_error", latest_user_message=text))
        short_term_context = _short_term_context(state, events, text)
        long_term_context = _long_term_memory_context(actor_id, text)
        if _looks_like_new_service_request(text):
            service_id = _detect_service(text, services, short_term_context, long_term_context)
            if service_id:
                state = new_state()
            else:
                return _reply(
                    state,
                    _model_reply(
                        actor_id,
                        state,
                        "service_not_understood",
                        latest_user_message=text,
                        service_options=[service["name"] for service in services],
                    ),
                )
        else:
            return _reply(state, _model_reply(actor_id, state, "completed", latest_user_message=text))

    if state.get("pending_pref_field"):
        field_id = state["pending_pref_field"]
        question = state.get("pending_pref_question") or ""
        verdict = _judge_reply(question, text)
        if verdict == "yes":
            state["collected_fields"][field_id] = state["pending_pref_value"]
        else:
            state["collected_fields"].update(_extract_fields(actor_id, state, text, events))
        state["pending_pref_field"] = None
        state["pending_pref_value"] = None
        state["pending_pref_question"] = None
        _recompute_missing(state)
        return _continue_collection(actor_id, state, latest_user_message=text, events=events)

    if state["awaiting_confirmation"]:
        _recompute_missing(state)
        if state["missing_fields"]:
            state["awaiting_confirmation"] = False
            state["status"] = "COLLECTING_INFORMATION"
            return _continue_collection(actor_id, state, latest_user_message=text, events=events)

        verdict = _judge_reply("請確認以上內容是否正確，若正確請回覆確認送出。", text)
        if verdict == "yes":
            return _submit(actor_id, session_id, state, latest_user_message=text, auth_token=auth_token)

        override = _extract_fields(actor_id, state, text, events)
        if override:
            state["collected_fields"].update(override)
            state["awaiting_confirmation"] = False
            _recompute_missing(state)
            if not state["missing_fields"]:
                state["status"] = "AWAITING_USER_CONFIRMATION"
                summary = _build_summary_text(state)
                return _reply(state, _model_reply(actor_id, state, "confirm", latest_user_message=text, summary=summary))
            return _continue_collection(actor_id, state, latest_user_message=text, events=events)

        state["awaiting_confirmation"] = False
        state["status"] = "COLLECTING_INFORMATION"
        return _reply(state, _model_reply(actor_id, state, "confirmation_retry", latest_user_message=text))

    if not state["service_id"]:
        if _looks_like_memory_question(text):
            memory_reply = _reply_from_memory(actor_id)
            if memory_reply:
                return _reply(state, memory_reply)

        services = _available_services(auth_token)
        if services is None:
            return _reply(state, _model_reply(actor_id, state, "service_catalog_error", latest_user_message=text))
        short_term_context = _short_term_context(state, events, text)
        long_term_context = _long_term_memory_context(actor_id, text)
        service_id = _detect_service(text, services, short_term_context, long_term_context)
        if not service_id:
            return _reply(
                state,
                _model_reply(
                    actor_id,
                    state,
                    "service_not_understood",
                    latest_user_message=text,
                    service_options=[service["name"] for service in services],
                ),
            )

        schema_result = _service_schema(service_id, auth_token)
        if not schema_result:
            return _reply(state, _model_reply(actor_id, state, "service_schema_error", latest_user_message=text))

        service = next((item for item in services if item["id"] == service_id), None)
        state["service_id"] = service_id
        state["service_name"] = _display_service_name(service_id, (service or {}).get("name") or schema_result.get("title"))
        state["service_schema"] = {"fields": schema_result["fields"]}
        _recompute_missing(state)

    found = _extract_fields(actor_id, state, text, events)
    state["collected_fields"].update(found)
    _recompute_missing(state)
    return _continue_collection(actor_id, state, latest_user_message=text, events=events)


def _continue_collection(actor_id: str, state: dict, latest_user_message: str = "", events: list[dict] | None = None) -> dict:
    prefs = _safe_preferences(actor_id)
    fields = state["service_schema"]["fields"]
    asked = state.setdefault("asked_pref_fields", [])

    for field_id, pref_key in (
        ("preferred_time_slot", "preferred_time_slot"),
        ("address", "last_address"),
        ("phone", "last_phone"),
    ):
        if (
            field_id in state["missing_fields"]
            and prefs.get(pref_key)
            and state["missing_fields"][0] == field_id
            and field_id not in asked
        ):
            value = SELECT_DISPLAY_NAMES.get(prefs[pref_key], prefs[pref_key])
            asked.append(field_id)
            state["pending_pref_field"] = field_id
            state["pending_pref_value"] = prefs[pref_key]
            question = _model_reply(
                actor_id,
                state,
                "reuse_preference",
                latest_user_message=latest_user_message,
                preferred_value=value,
                missing_field_label=_display_field_label(field_id, fields),
            )
            state["pending_pref_question"] = question
            return _reply(state, question)

    if state["missing_fields"]:
        state["status"] = "COLLECTING_INFORMATION"
        field = next(item for item in fields if item["id"] == state["missing_fields"][0])
        question = _build_field_question(field)
        invalid_message = _invalid_number_field_message(state, latest_user_message)
        if invalid_message:
            return _reply(state, f"{invalid_message}\n{question}")
        return _reply(
            state,
            _model_reply(
                actor_id,
                state,
                "collect_field",
                latest_user_message=latest_user_message,
                missing_field_label=_display_field_label(field["id"], fields),
                missing_field_question=question,
            ),
        )

    state["awaiting_confirmation"] = True
    state["status"] = "AWAITING_USER_CONFIRMATION"
    summary = _build_summary_text(state)
    return _reply(state, _model_reply(actor_id, state, "confirm", latest_user_message=latest_user_message, summary=summary))


def _update_long_term_memory(actor_id: str, state: dict) -> None:
    fields = state["service_schema"]["fields"]
    summary_lines = [f"服務：{_display_service_name(state['service_id'], state['service_name'])}"]
    for field in fields:
        if field["id"] in state["collected_fields"]:
            summary_lines.append(_display_value(field["id"], state["collected_fields"][field["id"]], fields))
    try:
        MEMORY.save_long_term_summary(
            actor_id,
            {
                "last_service_id": state["service_id"],
                "last_service_name": _display_service_name(state["service_id"], state["service_name"]),
                "last_request_summary": "；".join(summary_lines),
            },
        )
    except Exception:
        return None


def _submit(
    actor_id: str,
    session_id: str,
    state: dict,
    latest_user_message: str = "",
    auth_token: str | None = None,
) -> dict:
    _recompute_missing(state)
    if state.get("missing_fields"):
        state["awaiting_confirmation"] = False
        state["status"] = "COLLECTING_INFORMATION"
        return _continue_collection(actor_id, state, latest_user_message=latest_user_message)

    result = tools.call(
        "submit_service_request",
        {
            "service_id": state["service_id"],
            "session_id": session_id,
            "actor_id": actor_id,
            "payload": dict(state["collected_fields"]),
        },
        auth_token=auth_token,
    )
    if not result.get("success"):
        message = result.get("error", {}).get("message", "送出失敗")
        return _reply(
            state,
            _model_reply(
                actor_id,
                state,
                "submit_error",
                latest_user_message=latest_user_message,
                error_message=message,
            ),
        )

    state["request_id"] = result["request_id"]
    state["status"] = result["status"]
    state["awaiting_confirmation"] = False

    collected = state["collected_fields"]
    prefs = {}
    if collected.get("address"):
        prefs["last_address"] = collected["address"]
    if collected.get("phone"):
        prefs["last_phone"] = collected["phone"]
    if collected.get("preferred_time_slot"):
        prefs["preferred_time_slot"] = collected["preferred_time_slot"]
    if prefs:
        try:
            MEMORY.save_preferences(actor_id, prefs)
        except Exception:
            pass

    _update_long_term_memory(actor_id, state)
    return _reply(
        state,
        _model_reply(
            actor_id,
            state,
            "submit_success",
            latest_user_message=latest_user_message,
            request_id=result["request_id"],
        ),
    )


def _reply(state: dict, reply: str) -> dict:
    return {"reply": reply, "state": state}
