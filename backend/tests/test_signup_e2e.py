"""註冊帳號端到端測試。

覆蓋「真的註冊一組帳號，然後把 App 裡的功能全跑一遍」：
註冊 → 登入 → 服務清單／schema → 手動表單建案 → 案件狀態流轉 →
AI 管家對話建案 → 資料隔離 → 重開後端後帳號與案件仍在。

這些測試不需要 AWS：Bedrock 不可用時 agent 會退回規則式 NLU，
對話流程必須照樣走完（這正是先前註冊帳號「進去什麼都不能用」的原因）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.auth.users import USERS, UserStore
from backend.app.main import app
from backend.app.services import store as STORE_MODULE
from backend.app.services.store import STORE, MemoryStore

PASSWORD = "Passw0rd123"


def _unique_email() -> str:
    return f"e2e-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture(scope="module")
def storage_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把使用者表與案件表導向暫存檔，測試不要污染開發機的 .local-*.json。"""
    directory = tmp_path_factory.mktemp("e2e-store")
    USERS._storage_path = directory / "users.json"
    STORE._storage_path = directory / "store.json"
    return directory


@pytest.fixture
def client(storage_dir: Path) -> TestClient:
    return TestClient(app)


@pytest.fixture
def account(client: TestClient) -> dict:
    """真的註冊一組新帳號，回傳 token 與 sub。"""
    email = _unique_email()
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "name": "E2E 測試用戶"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "email": email,
        "token": body["token"],
        "sub": body["sub"],
        "name": body["name"],
        "headers": {"Authorization": f"Bearer {body['token']}"},
    }


def _chat(client: TestClient, headers: dict, session_id: str, message: str) -> dict:
    response = client.post(
        "/api/chat",
        headers=headers,
        json={"session_id": session_id, "message": message},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- 註冊／登入 ----


def test_register_returns_backend_generated_sub_and_token(account: dict):
    assert account["sub"].startswith("user-")
    assert account["token"]
    assert account["name"] == "E2E 測試用戶"


def test_register_rejects_duplicate_email(client: TestClient, account: dict):
    response = client.post(
        "/api/auth/register",
        json={"email": account["email"], "password": PASSWORD, "name": "另一個人"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "EMAIL_EXISTS"


def test_register_rejects_weak_password_and_bad_email(client: TestClient):
    weak = client.post(
        "/api/auth/register",
        json={"email": _unique_email(), "password": "123", "name": ""},
    )
    assert weak.status_code == 400
    assert weak.json()["detail"]["error"]["code"] == "WEAK_PASSWORD"

    bad_email = client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": PASSWORD, "name": ""},
    )
    assert bad_email.status_code == 400
    assert bad_email.json()["detail"]["error"]["code"] == "INVALID_EMAIL"


def test_registered_account_can_log_in_again(client: TestClient, account: dict):
    response = client.post(
        "/api/auth/login",
        json={"email": account["email"], "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sub"] == account["sub"]


def test_login_rejects_wrong_password(client: TestClient, account: dict):
    response = client.post(
        "/api/auth/login",
        json={"email": account["email"], "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "INVALID_CREDENTIALS"


def test_me_requires_a_token(client: TestClient):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


# ---- 登入後的功能都要能用 ----


def test_registered_account_can_read_service_catalog(client: TestClient, account: dict):
    services = client.get("/api/services", headers=account["headers"])
    assert services.status_code == 200, services.text
    service_ids = {service["id"] for service in services.json()["services"]}
    assert {
        "plumbing_repair",
        "washing_machine_cleaning",
        "air_conditioner_cleaning",
        "home_cleaning",
    } <= service_ids

    schema = client.get(
        "/api/services/air_conditioner_cleaning/schema", headers=account["headers"]
    )
    assert schema.status_code == 200, schema.text
    assert schema.json()["service_id"] == "air_conditioner_cleaning"


def test_registered_account_request_list_starts_empty(client: TestClient, account: dict):
    response = client.get("/api/requests", headers=account["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_registered_account_can_submit_manual_form_and_progress_the_request(
    client: TestClient, account: dict
):
    created = client.post(
        "/api/services/air_conditioner_cleaning/requests",
        headers=account["headers"],
        json={
            "payload": {
                "quantity": 2,
                "preferred_date": "2099-08-05",
                "preferred_time_slot": "MORNING",
                "address": "台北市大安區忠孝東路四段 100 號 5 樓",
                "phone": "0912345678",
            }
        },
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["request_id"]
    assert request_id

    detail = client.get(f"/api/requests/{request_id}", headers=account["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["form_data"]["quantity"] == 2

    listing = client.get("/api/requests", headers=account["headers"])
    assert [item["request_id"] for item in listing.json()["items"]] == [request_id]

    for next_status in ("CONFIRMED", "IN_PROGRESS", "COMPLETED"):
        moved = client.post(
            f"/api/requests/{request_id}/simulate/{next_status}", headers=account["headers"]
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["status"] == next_status


def test_manual_form_rejects_missing_required_fields(client: TestClient, account: dict):
    response = client.post(
        "/api/services/air_conditioner_cleaning/requests",
        headers=account["headers"],
        json={"payload": {"quantity": 1}},
    )
    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "INVALID_FORM_DATA"
    assert error["missing_fields"]


def test_registered_account_can_cancel_a_request(client: TestClient, account: dict):
    created = client.post(
        "/api/services/home_cleaning/requests",
        headers=account["headers"],
        json={
            "payload": {
                "hours": 3,
                "preferred_date": "2099-08-10",
                "preferred_time_slot": "AFTERNOON",
                "address": "台北市大安區忠孝東路四段 100 號",
                "phone": "0912345678",
            }
        },
    )
    assert created.status_code == 200, created.text
    request_id = created.json()["request_id"]

    cancelled = client.post(f"/api/requests/{request_id}/cancel", headers=account["headers"])
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"


# ---- AI 管家對話（沒有 Bedrock 也必須能走完） ----


def test_registered_account_can_complete_a_booking_through_chat(
    client: TestClient, account: dict
):
    session = client.post("/api/sessions", headers=account["headers"])
    assert session.status_code == 200, session.text
    session_id = session.json()["session_id"]

    first = _chat(client, account["headers"], session_id, "我要預約洗衣機清洗")
    assert first["service_id"] == "washing_machine_cleaning"

    # 一輪一輪回答，每一輪都必須真的把欄位收進去（Bedrock 不可用時靠規則式 NLU）。
    answers = {
        "quantity": ("兩台", 2),
        "machine_type": ("滾筒式", "FRONT_LOAD"),
        "preferred_date": ("2099-08-05", "2099-08-05"),
        "preferred_time_slot": ("下午", "AFTERNOON"),
        "address": ("台北市大安區忠孝東路四段 100 號 5 樓", None),
        "phone": ("0912345678", "0912345678"),
    }

    latest = first
    for _ in range(len(answers) + 2):
        if not latest["missing_fields"]:
            break
        field_id = latest["missing_fields"][0]
        assert field_id in answers, f"agent 問了預期外的欄位：{field_id}"
        message, expected = answers[field_id]
        latest = _chat(client, account["headers"], session_id, message)
        assert field_id in latest["collected_fields"], (
            f"回答「{message}」之後 {field_id} 仍未被收集，"
            f"reply={latest['reply']!r}"
        )
        if expected is not None:
            assert latest["collected_fields"][field_id] == expected

    assert latest["missing_fields"] == []
    assert latest["status"] == "AWAITING_USER_CONFIRMATION"

    submitted = _chat(client, account["headers"], session_id, "確認送出")
    assert submitted["request_id"], f"確認後沒有建立案件：{submitted['reply']!r}"

    listing = client.get("/api/requests", headers=account["headers"])
    assert submitted["request_id"] in {
        item["request_id"] for item in listing.json()["items"]
    }


def test_chat_collects_free_text_field_for_plumbing_repair(client: TestClient, account: dict):
    """issue_description 是自由填寫欄位，被問到時整句話就是答案。"""
    session_id = client.post("/api/sessions", headers=account["headers"]).json()["session_id"]

    first = _chat(client, account["headers"], session_id, "家裡浴室漏水想找人修")
    assert first["service_id"] == "plumbing_repair"
    # 開場那句本身就是問題描述，不該再倒回來問一次。
    assert first["collected_fields"].get("issue_description") == "家裡浴室漏水想找人修"
    assert "issue_description" not in first["missing_fields"]

    # 剩下的欄位一輪一輪都要收得到，最後能真的送出。
    for message, field_id in (
        ("2099-08-20", "preferred_date"),
        ("上午", "preferred_time_slot"),
        ("台北市大安區忠孝東路四段 100 號 5 樓", "address"),
        ("0912345678", "phone"),
    ):
        latest = _chat(client, account["headers"], session_id, message)
        assert field_id in latest["collected_fields"], (
            f"回答「{message}」之後 {field_id} 仍未被收集，reply={latest['reply']!r}"
        )

    assert latest["missing_fields"] == []
    submitted = _chat(client, account["headers"], session_id, "確認送出")
    assert submitted["request_id"], f"確認後沒有建立案件：{submitted['reply']!r}"


def test_chat_keeps_the_full_address_including_house_number(client: TestClient, account: dict):
    session_id = client.post("/api/sessions", headers=account["headers"]).json()["session_id"]

    _chat(client, account["headers"], session_id, "我想預約冷氣清洗")
    _chat(client, account["headers"], session_id, "一台")
    _chat(client, account["headers"], session_id, "2099-08-21")
    _chat(client, account["headers"], session_id, "上午")
    latest = _chat(client, account["headers"], session_id, "台北市大安區忠孝東路四段 100 號 5 樓")

    # 含空白的地址不能在第一個空格處被截斷。
    assert latest["collected_fields"]["address"] == "台北市大安區忠孝東路四段 100 號 5 樓"


def test_chat_field_answer_is_not_hijacked_by_page_guidance(client: TestClient, account: dict):
    """「滾筒式」同時是頁面關鍵字，填表中不能被當成導覽問題。"""
    session_id = client.post("/api/sessions", headers=account["headers"]).json()["session_id"]

    _chat(client, account["headers"], session_id, "我要預約洗衣機清洗")
    _chat(client, account["headers"], session_id, "兩台")
    reply = _chat(client, account["headers"], session_id, "滾筒式")
    assert reply["collected_fields"].get("machine_type") == "FRONT_LOAD"


def test_chat_still_answers_real_navigation_questions(client: TestClient, account: dict):
    session_id = client.post("/api/sessions", headers=account["headers"]).json()["session_id"]

    reply = _chat(client, account["headers"], session_id, "我可以去哪裡申請冷氣清潔?")
    assert reply["reply"]
    assert reply["request_id"] is None


def test_chat_recognises_air_conditioner_cleaning_not_washing_machine(
    client: TestClient, account: dict
):
    session_id = client.post("/api/sessions", headers=account["headers"]).json()["session_id"]

    reply = _chat(client, account["headers"], session_id, "我想預約冷氣清洗")
    assert reply["service_id"] == "air_conditioner_cleaning"


def test_chat_rejects_an_unknown_session(client: TestClient, account: dict):
    response = client.post(
        "/api/chat",
        headers=account["headers"],
        json={"session_id": "does-not-exist", "message": "你好"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "SESSION_NOT_FOUND"


# ---- 多使用者資料隔離 ----


def test_requests_are_isolated_between_accounts(client: TestClient, account: dict):
    other = client.post(
        "/api/auth/register",
        json={"email": _unique_email(), "password": PASSWORD, "name": "別人"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    created = client.post(
        "/api/services/home_cleaning/requests",
        headers=account["headers"],
        json={
            "payload": {
                "hours": 2,
                "preferred_date": "2099-09-01",
                "preferred_time_slot": "MORNING",
                "address": "台北市大安區忠孝東路四段 100 號",
                "phone": "0912345678",
            }
        },
    )
    request_id = created.json()["request_id"]

    assert request_id not in {
        item["request_id"]
        for item in client.get("/api/requests", headers=other_headers).json()["items"]
    }
    assert client.get(f"/api/requests/{request_id}", headers=other_headers).status_code == 404


# ---- 重開後端後帳號不能消失 ----


def test_user_and_request_stores_persist_by_default():
    """實際跑起來的 singleton 必須真的有落地路徑，不然重開就全沒了。"""
    assert UserStore()._storage_path is not None
    assert STORE_MODULE.build_store()._storage_path is not None


def test_registered_account_survives_a_backend_restart(account: dict, storage_dir: Path):
    """重新建立 UserStore 模擬 `uvicorn --reload`／重開後端。

    先前使用者表只放在記憶體，重開後 token 直接 401、
    連原本的 Email／密碼都登不回來，示範帳號卻還能用——
    這就是「註冊帳號無法使用裡面的功能」的主因。
    """
    restarted_store = UserStore(storage_dir / "users.json")

    # 前端保存的 token 仍然有效。
    resolved = restarted_store.resolve(account["token"])
    assert resolved is not None, "重開後端後既有 token 失效"
    assert resolved.sub == account["sub"]
    assert resolved.name == account["name"]

    # 原本的 Email／密碼也還能登入，而且拿到同一個 sub（案件不會走失）。
    user, new_token = restarted_store.login(account["email"], PASSWORD)
    assert user.sub == account["sub"]
    assert new_token

    # 密碼雜湊沒有被明文化。
    saved = (storage_dir / "users.json").read_text(encoding="utf-8")
    assert PASSWORD not in saved


def test_requests_survive_a_backend_restart(client: TestClient, account: dict, storage_dir: Path):
    created = client.post(
        "/api/services/home_cleaning/requests",
        headers=account["headers"],
        json={
            "payload": {
                "hours": 2,
                "preferred_date": "2099-09-02",
                "preferred_time_slot": "MORNING",
                "address": "台北市大安區忠孝東路四段 100 號",
                "phone": "0912345678",
            }
        },
    )
    request_id = created.json()["request_id"]

    restarted_store = MemoryStore(storage_dir / "store.json")
    assert restarted_store.get_request(account["sub"], request_id) is not None
