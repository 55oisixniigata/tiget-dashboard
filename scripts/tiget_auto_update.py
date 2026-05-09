#!/usr/bin/env python3
"""
TIGET自動更新スクリプト
GitHub Actionsから呼び出してtiget_data.jsonを更新する
"""
import json
import re
import sys
import os
import urllib.request
import urllib.parse
import urllib.error

# ===== 設定 =====
TIGET_EMAIL    = os.environ["TIGET_EMAIL"]
TIGET_PASSWORD = os.environ["TIGET_PASSWORD"]
JSON_PATH      = os.environ.get("JSON_PATH", "tiget_data.json")

BASE_URL  = "https://api.more.tiget.net"
GQL_URL   = f"{BASE_URL}/graphql"

# 2026年 イベントID
JIYU_EVENT_ID   = 10234  # 自由席 → jiyu_sold, jiyu_allocated
SHITEI_EVENT_ID = 10233  # 指定席 → shitei_sold
SHOTAI_EVENT_IDS = [10250, 10276, 10273]  # 招待3イベント → shotai_sold合算

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


def signin():
    """TIGETにサインインしてアクセストークンを取得"""
    url = f"{BASE_URL}/auth/organization/signin"
    body = json.dumps({"email": TIGET_EMAIL, "password": TIGET_PASSWORD}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://organization.more.tiget.net",
            "Referer": "https://organization.more.tiget.net/",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as res:
            access_token = res.headers.get("access-token")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"サインイン失敗: HTTP {e.code} - {err_body}")
    if not access_token:
        raise RuntimeError("サインイン失敗: access-tokenが取得できません")
    print(f"✅ サインイン成功")
    return access_token


def fetch_event_data(event_id, access_token):
    """指定イベントの試合別販売データを取得"""
    payload = json.dumps({
        "query": GQL_QUERY,
        "variables": {"eventId": event_id}
    }).encode()
    req = urllib.request.Request(
        GQL_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://organization.more.tiget.net",
            "Referer": "https://organization.more.tiget.net/",
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as res:
        data = json.load(res)
    stages = data.get("data", {}).get("eventPurchaseListByStage", [])
    print(f"  イベント {event_id}: {len(stages)}試合分取得")
    return stages


def parse_date(stage_start_at):
    """stageStartAt (ISO形式) → 日付文字列 (YYYY-MM-DD)"""
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    dt = datetime.fromisoformat(stage_start_at.replace("Z", "+00:00"))
    return dt.astimezone(JST).strftime("%Y-%m-%d")


def main():
    print("=== TIGET自動更新 開始 ===")

    # 1. サインイン
    access_token = signin()

    # 2. 自由席データ取得
    print("自由席データ取得中...")
    jiyu_stages = fetch_event_data(JIYU_EVENT_ID, access_token)
    jiyu_data = {}
    for s in jiyu_stages:
        d = parse_date(s["stageStartAt"])
        jiyu_data[d] = {
            "confirmed": s["paidEventPurchasesCount"],
            "allocated": s["ticketSalesConfigurationStock"]
        }

    # 3. 指定席データ取得
    print("指定席データ取得中...")
    shitei_stages = fetch_event_data(SHITEI_EVENT_ID, access_token)
    shitei_data = {}
    for s in shitei_stages:
        d = parse_date(s["stageStartAt"])
        shitei_data[d] = s["paidEventPurchasesCount"]

    # 4. 招待データ取得（3イベント合算）
    print("招待データ取得中...")
    shotai_data = {}
    for event_id in SHOTAI_EVENT_IDS:
        stages = fetch_event_data(event_id, access_token)
        for s in stages:
            d = parse_date(s["stageStartAt"])
            shotai_data[d] = shotai_data.get(d, 0) + s["paidEventPurchasesCount"]

    # 5. tiget_data.json 読み込み
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # 6. 更新
    updated = 0
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

        if changed:
            game["total_presale"] = (
                game.get("jiyu_sold", 0) +
                game.get("shitei_sold", 0) +
                game.get("shotai_sold", 0)
            )
            updated += 1

    # 7. バージョンインクリメント
    ver_m = re.search(r"v(\d+)$", data.get("version", "2026-v0"))
    old_v = int(ver_m.group(1)) if ver_m else 0
    data["version"] = f"2026-v{old_v + 1}"

    # 8. 書き込み
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n✅ 更新完了: {updated}試合 / バージョン: {data['version']}")

    # GitHub Actions用サマリー出力
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"## ✅ TIGET更新完了\n")
            f.write(f"- 更新試合数: **{updated}試合**\n")
            f.write(f"- バージョン: `{data['version']}`\n")
            f.write(f"- 自由席: {len(jiyu_data)}試合\n")
            f.write(f"- 指定席: {len(shitei_data)}試合\n")
            f.write(f"- 招待合計: {len(shotai_data)}試合\n")


if __name__ == "__main__":
    main()
