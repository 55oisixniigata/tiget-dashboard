#!/usr/bin/env python3
"""
TIGET自動更新スクリプト
GitHub Actionsから呼び出してtiget_data.jsonを更新する

認証方式: リフレッシュトークン（30日有効）
  - TIGET_REFRESH_TOKEN シークレットに保存
  - 期限切れ時は organization.more.tiget.net にログイン後、
    ブラウザの Cookie "lear-organization-session" から refreshToken を再取得して更新
"""
import json
import re
import os
from datetime import date
import sys

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ===== 設定 =====
TIGET_REFRESH_TOKEN = os.environ["TIGET_REFRESH_TOKEN"]
JSON_PATH           = os.environ.get("JSON_PATH", "tiget_data.json")

BASE_URL = "https://api.more.tiget.net"
GQL_URL  = f"{BASE_URL}/graphql"

# 2026年 イベントID
JIYU_EVENT_ID    = 10234
SHITEI_EVENT_ID  = 10233
SHOTAI_EVENT_IDS = [10250, 10276, 10273]
FARM_DATA_URL = "https://raw.githubusercontent.com/55oisixniigata/npb-farm-tracker/main/data.json"


GQL_QUERY = """
query ($eventId: Int!) {
  eventPurchaseListByStage(eventId: $eventId) {
    stageStartAt
    stageName
    ticketSalesConfigurationStock
    paidEventPurchasesCount
  }
}
"""

COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://organization.more.tiget.net",
    "Referer": "https://organization.more.tiget.net/",
}


def get_access_token():
    """リフレッシュトークンを使って新しいアクセストークンを取得"""
    resp = requests.put(
        f"{BASE_URL}/auth/organization/refresh",
        headers={
            **COMMON_HEADERS,
            "Authorization": f"Bearer {TIGET_REFRESH_TOKEN}",
            "refresh-token": TIGET_REFRESH_TOKEN,
        },
    )
    if not resp.ok:
        raise RuntimeError(f"トークン更新失敗: HTTP {resp.status_code} - {resp.text[:200]}")
    access_token = resp.headers.get("access-token")
    if not access_token:
        raise RuntimeError("トークン更新失敗: access-tokenが取得できません")
    print("✅ アクセストークン取得成功")
    return access_token


def fetch_event_data(event_id, access_token):
    """指定イベントの試合別販売データを取得"""
    resp = requests.post(
        GQL_URL,
        json={"query": GQL_QUERY, "variables": {"eventId": event_id}},
        headers={**COMMON_HEADERS, "Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    stages = resp.json().get("data", {}).get("eventPurchaseListByStage", [])
    print(f"  イベント {event_id}: {len(stages)}試合分取得")
    return stages


def parse_date(stage_start_at):
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    dt = datetime.fromisoformat(stage_start_at.replace("Z", "+00:00"))
    return dt.astimezone(JST).strftime("%Y-%m-%d")


def fetch_farm_attendance():
    """NPBファームトラッカーからオイシックスのホーム試合実動員数を取得"""
    try:
        resp = requests.get(FARM_DATA_URL, timeout=10)
        resp.raise_for_status()
        farm = resp.json()
        result = {
            g["date"]: g["audience"]
            for g in farm.get("games", [])
            if g.get("home") == "オイシックス" and g.get("audience")
        }
        print(f"  ファームトラッカー: {len(result)}試合分取得")
        return result
    except Exception as e:
        print(f"  ⚠️ ファームトラッカー取得失敗（スキップ）: {e}")
        return {}


def main():
    print("=== TIGET自動更新 開始 ===")

    access_token = get_access_token()

    print("自由席データ取得中...")
    jiyu_stages = fetch_event_data(JIYU_EVENT_ID, access_token)
    jiyu_data = {}
    for s in jiyu_stages:
        d = parse_date(s["stageStartAt"])
        jiyu_data[d] = {
            "confirmed": s["paidEventPurchasesCount"],
            "allocated": s["ticketSalesConfigurationStock"]
        }

    print("指定席データ取得中...")
    shitei_stages = fetch_event_data(SHITEI_EVENT_ID, access_token)
    shitei_data = {}
    for s in shitei_stages:
        d = parse_date(s["stageStartAt"])
        shitei_data[d] = s["paidEventPurchasesCount"]

    print("招待データ取得中...")
    shotai_data = {}
    for event_id in SHOTAI_EVENT_IDS:
        stages = fetch_event_data(event_id, access_token)
        for s in stages:
            d = parse_date(s["stageStartAt"])
            shotai_data[d] = shotai_data.get(d, 0) + s["paidEventPurchasesCount"]

    # ファームトラッカーから実動員数取得
    print("実動員数（ファームトラッカー）取得中...")
    farm_attendance = fetch_farm_attendance()

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    updated = 0
    farm_updated = 0
    for game in data["games"]:
        d = game["date"]
        changed = False
        if d in jiyu_data:
            game["jiyu_sold"]      = jiyu_data[d]["confirmed"]
            game["jiyu_allocated"] = jiyu_data[d]["allocated"]
            changed = True
        if d in shitei_data:
            game["shitei_sold"] = shitei_data[d]
            changed = True
        if d in shotai_data:
            game["shotai_sold"] = shotai_data[d]
            changed = True
        if d in farm_attendance:
            new_val = farm_attendance[d]
            if game.get("actual_audience") != new_val:
                game["actual_audience"] = new_val
                farm_updated += 1
                changed = True
        if changed:
            game["total_presale"] = (
                game.get("jiyu_sold", 0) +
                game.get("shitei_sold", 0) +
                game.get("shotai_sold", 0)
            )
            updated += 1

    ver_m = re.search(r"v(\d+)$", data.get("version", "2026-v0"))
    old_v = int(ver_m.group(1)) if ver_m else 0
    data["version"] = f"2026-v{old_v + 1}"
    data["last_updated"] = date.today().isoformat()  # 更新日を今日の日付に

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✅ 更新完了: {updated}試合 / バージョン: {data['version']}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"## ✅ TIGET更新完了\n")
            f.write(f"- 更新試合数: **{updated}試合**\n")
            f.write(f"- バージョン: `{data['version']}`\n")
            f.write(f"- 実動員数更新: **{farm_updated}試合**\n")


if __name__ == "__main__":
    main()
