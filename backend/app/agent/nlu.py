"""規則式中文 NLU（Milestone 1 Mock LLM）。

Milestone 2 換成 Bedrock 後，這個模組仍保留作為：
1. 離線備援  2. 單元測試基準  3. 欄位後驗證（相對日期轉換等）
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from ..services.catalog import SERVICES

# ---- 縣市行政區資料（來自命題縣市區域檔） ----
_REGIONS = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "tw_regions.json").read_text(encoding="utf-8")
)
COUNTY_NAMES = [c["name"] for c in _REGIONS["counties"]]
# 台/臺 互換
_COUNTY_ALT = {n.replace("台", "臺"): n for n in COUNTY_NAMES if "台" in n}

_CN_NUM = {"一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_quantity(text: str, unit_chars: str = "台臺部間") -> int | None:
    """擷取「兩台」「3 台」等數量。"""
    m = re.search(rf"([0-9]+|[{''.join(_CN_NUM)}])\s*[{unit_chars}]", text)
    if m:
        tok = m.group(1)
        return int(tok) if tok.isdigit() else _CN_NUM.get(tok)
    return None


def parse_number(text: str) -> int | None:
    m = re.search(rf"([0-9]+|[{''.join(_CN_NUM)}])", text)
    if m:
        tok = m.group(1)
        return int(tok) if tok.isdigit() else _CN_NUM.get(tok)
    return None


def parse_hours(text: str) -> int | None:
    m = re.search(rf"([0-9]+|[{''.join(_CN_NUM)}])\s*(小時|個小時|hr|小時)", text)
    if m:
        tok = m.group(1)
        return int(tok) if tok.isdigit() else _CN_NUM.get(tok)
    return None


_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_date(text: str, today: date | None = None) -> str | None:
    """相對日期 → ISO 日期字串（設計書 14.3：相對日期需轉成明確日期）。"""
    today = today or date.today()
    if "大後天" in text:
        return (today + timedelta(days=3)).isoformat()
    if "後天" in text:
        return (today + timedelta(days=2)).isoformat()
    if "明天" in text or "明日" in text:
        return (today + timedelta(days=1)).isoformat()
    if "今天" in text or "今日" in text:
        return today.isoformat()

    m = re.search(r"(下)?(?:週|周|星期|禮拜)([一二三四五六日天])", text)
    if m:
        target = _WEEKDAYS[m.group(2)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        if m.group(1):  # 「下週X」再加一週
            delta += 7 if delta <= 7 else 0
        return (today + timedelta(days=delta)).isoformat()

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year + (1 if (month, day) < (today.month, today.day) else 0)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def parse_time_slot(text: str) -> str | None:
    if re.search(r"早上|上午|一早|早班", text):
        return "MORNING"
    if re.search(r"下午|中午過後|午後", text):
        return "AFTERNOON"
    if re.search(r"晚上|傍晚|晚間|夜間", text):
        return "EVENING"
    return None


def parse_machine_type(text: str) -> str | None:
    if "滾筒" in text:
        return "FRONT_LOAD"
    if "直立" in text:
        return "TOP_LOAD"
    return None


def parse_phone(text: str) -> str | None:
    m = re.search(r"09\d{2}[-\s]?\d{3}[-\s]?\d{3}", text)
    if m:
        return re.sub(r"[-\s]", "", m.group(0))
    m = re.search(r"0\d{1,2}[-\s]?\d{6,8}", text)
    return re.sub(r"[-\s]", "", m.group(0)) if m else None


def parse_address(text: str) -> str | None:
    """偵測含縣市名的地址片段（利用命題縣市區域檔）。"""
    norm = text
    for alt, std in _COUNTY_ALT.items():
        norm = norm.replace(alt, std)
    for county in COUNTY_NAMES:
        idx = norm.find(county)
        if idx >= 0:
            tail = norm[idx:]
            # 允許空白：實際輸入常寫「台北市大安區忠孝東路四段 100 號 5 樓」，
            # 不允許空白會在第一個空格處截斷，門牌整段被丟掉。
            m = re.match(r"^[\u4e00-\u9fffA-Za-z0-9０-９\-之號樓巷弄路街段區鄉鎮市村里鄰 ]+", tail)
            addr = m.group(0) if m else county
            # 去掉結尾標點與贅字；若含「號/樓」則截到最後一個門牌單位
            addr = re.sub(r"[，。,\.、\s]+$", "", addr)
            m2 = re.search(r"^.*?(?:\d+\s*號(?:之\d+)?(?:\s*\d+\s*樓)?)", addr)
            if m2:
                addr = m2.group(0)
            if len(addr) >= len(county):
                return addr
    return None


def detect_service(text: str) -> tuple[str | None, list[dict]]:
    """回傳 (最佳 service_id, 候選列表)。以關鍵字出現次數與長度加權。"""
    scores: list[tuple[int, dict]] = []
    for s in SERVICES:
        if not s["enabled"]:
            continue
        score = 0
        for kw in s["keywords"]:
            if kw in text:
                score += len(kw)
        if score:
            scores.append((score, s))
    scores.sort(key=lambda x: -x[0])
    best = scores[0][1]["id"] if scores else None
    return best, [s for _, s in scores]


# ---- 欄位擷取 dispatcher ----
def extract_fields(service_id: str, fields: list[dict], text: str,
                   collected: dict) -> dict:
    """從一則訊息擷取尚未收集的欄位值，回傳新擷取到的 {field_id: value}。"""
    found: dict = {}
    for f in fields:
        fid = f["id"]
        if fid in collected:
            continue
        value = None
        if fid == "quantity":
            value = parse_quantity(text)
        elif fid == "hours":
            value = parse_hours(text)
        elif fid == "preferred_date":
            value = parse_date(text)
        elif fid == "preferred_time_slot":
            value = parse_time_slot(text)
        elif fid == "machine_type":
            value = parse_machine_type(text)
        elif fid == "phone":
            value = parse_phone(text)
        elif fid == "address":
            value = parse_address(text)
        elif fid == "issue_description":
            # 問題描述：在被詢問時取整句；首句若含關鍵詞也直接取
            value = None
        if value is not None:
            found[fid] = value
    return found
