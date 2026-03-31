"""
選情監控系統 - 主程式
功能：搜尋 Google / FB / IG / Threads / PTT / Google News，用 Claude AI 分析情緒，寫入 Google Sheets，發送 Email 通知
- 只收錄今天的資料（日期過濾）
- API 額度控制在 100 次內
"""

import os
import json
import time
import datetime
import smtplib
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── 設定區 ───
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"].strip()
GOOGLE_CX_ID   = os.environ["GOOGLE_CX_ID"].strip()
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_USER     = os.environ["GMAIL_USER"].strip()
GMAIL_PASS     = os.environ["GMAIL_PASS"].strip()
NOTIFY_EMAIL   = os.environ["NOTIFY_EMAIL"].strip()
SHEET_ID       = os.environ["GOOGLE_SHEET_ID"].strip()

TODAY = datetime.date.today().isoformat()  # e.g. "2026-03-31"

# ─── 監控目標 ───
CANDIDATES = [
    "賴清德",
    "陳素月",
    "蔡英文",
    "黃柏瑜",
    "彰化縣",
    "彰化市",
    "民進黨",
]

DIMENSIONS = [
    "政見",
    "爭議",
    "支持",
    "批評",
]

MAIN_PLATFORMS = {
    "Google":  "",
    "FB":      "site:facebook.com",
    "Threads": "site:threads.net",
}

EXTRA_PLATFORMS = {
    "IG":  "site:instagram.com",
    "PTT": "site:ptt.cc",
}


# ─── Google Custom Search ────────────────────────────────

def search_google_custom(keyword: str, site_prefix: str, platform: str, num: int = 3) -> list[dict]:
    url = "https://www.googleapis.com/customsearch/v1"
    query = f"{site_prefix} {keyword}".strip() if site_prefix else keyword
    params = {
        "key": GOOGLE_API_KEY,
        "cx":  GOOGLE_CX_ID,
        "q":   query,
        "num": num,
        "hl":  "zh-TW",
        "dateRestrict": "d1",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("items", [])
        return [{
            "platform": platform,
            "title":    i.get("title", ""),
            "summary":  i.get("snippet", ""),
            "url":      i.get("link", ""),
        } for i in items]
    except Exception as e:
        print(f"  [{platform} 錯誤] {e}")
        return []


# ─── Google News RSS（免費，只取今天）──────────────────────

def search_google_news_rss(keyword: str, num: int = 5) -> list[dict]:
    """只收錄今天發布的新聞"""
    results = []
    url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:num * 3]:  # 多取幾筆再過濾
            title   = item.findtext("title", "")
            link    = item.findtext("link", "")
            desc    = (item.findtext("description", "") or "")[:200]
            pub_date = item.findtext("pubDate", "")

            # 日期過濾：只收今天的
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_date)
                    pub_day = pub_dt.date().isoformat()
                    if pub_day != TODAY:
                        continue
                except:
                    pass  # 解析失敗就保留

            results.append({
                "platform": "Google News",
                "title":    title,
                "summary":  desc,
                "url":      link,
            })
            if len(results) >= num:
                break
    except Exception as e:
        print(f"  [Google News RSS 錯誤] {e}")
    return results


# ─── Claude AI 情緒分析 ──────────────────────────────────

def analyze_sentiment(items: list[dict], candidate: str) -> list[dict]:
    if not items:
        return []
    content_list = "\n".join(
        f"{i+1}. 標題：{it['title']}\n   摘要：{it['summary']}"
        for i, it in enumerate(items)
    )
    prompt = f"""你是台灣選情分析專家。分析以下關於「{candidate}」的資料情緒傾向。

{content_list}

以 JSON 陣列回覆，每個元素：
- index: 編號（從1開始）
- sentiment: "正面" / "中立" / "負面"
- reason: 理由（10字內）

只回傳 JSON 陣列。"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        sentiments = json.loads(text)
        sent_map = {s["index"]: s for s in sentiments}
        for i, item in enumerate(items):
            s = sent_map.get(i + 1, {})
            item["sentiment"] = s.get("sentiment", "中立")
            item["reason"]    = s.get("reason", "")
    except Exception as e:
        print(f"  [Claude 分析錯誤] {e}")
        for item in items:
            item["sentiment"] = "中立"
            item["reason"]    = "分析失敗"
    return items


# ─── 寫入 Google Sheets ──────────────────────────────────

def append_to_sheet(rows: list[list]) -> bool:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        sa_json = os.environ["GOOGLE_SA_JSON"].strip()
        sa_info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(
            sa_info,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc    = gspread.authorize(creds)
        sheet = gc.open_by_key(SHEET_ID).sheet1
        if sheet.row_count == 0 or sheet.cell(1, 1).value is None:
            sheet.append_row(["時間", "候選人", "維度", "平台", "標題", "摘要", "情緒", "原因", "來源連結"])
        sheet.append_rows(rows)
        print(f"[Sheets] ✅ 寫入 {len(rows)} 筆")
        return True
    except Exception as e:
        print(f"[Sheets 錯誤] {e}")
        return False


# ─── Email 通知 ──────────────────────────────────────────

def send_email(subject: str, html_body: str):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"[Email] ✅ 發送成功 → {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"[Email 錯誤] {e}")


def build_email_html(results: list[dict], run_time: str, api_count: int) -> str:
    pos   = sum(1 for r in results if r.get("sentiment") == "正面")
    neg   = sum(1 for r in results if r.get("sentiment") == "負面")
    total = len(results)
    score = round(((pos - neg) / total * 100)) if total else 0

    platform_counts = {}
    for r in results:
        p = r.get("platform", "")
        platform_counts[p] = platform_counts.get(p, 0) + 1
    platform_summary = " ｜ ".join(f"{k} {v}筆" for k, v in sorted(platform_counts.items()))

    rows_html = ""
    for r in results:
        color = "#22c55e" if r.get("sentiment") == "正面" else "#ef4444" if r.get("sentiment") == "負面" else "#94a3b8"
        emoji = "🟢" if r.get("sentiment") == "正面" else "🔴" if r.get("sentiment") == "負面" else "⚪"
        rows_html += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px;color:#64748b">{r.get('platform','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px;font-weight:600">{r.get('candidate','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px">{r.get('dimension','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;max-width:280px">{r.get('title','')[:55]}...</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;white-space:nowrap"><span style="color:{color};font-weight:700">{emoji} {r.get('sentiment','')}</span></td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px"><a href="{r.get('url','')}" style="color:#3b82f6">查看</a></td>
        </tr>"""

    score_color = "#22c55e" if score > 0 else "#ef4444" if score < 0 else "#94a3b8"
    score_str   = f"+{score}" if score > 0 else str(score)

    return f"""<!DOCTYPE html>
<html><body style="font-family:'Microsoft JhengHei',sans-serif;background:#f8fafc;padding:20px;margin:0">
<div style="max-width:820px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.1)">
  <div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px;color:white">
    <h1 style="margin:0;font-size:22px;letter-spacing:1px">📡 選情監控日報</h1>
    <p style="margin:6px 0 0;opacity:0.6;font-size:12px">{run_time} 自動產生｜僅限今日資料｜API 用量 {api_count}/100 次</p>
    <p style="margin:4px 0 0;opacity:0.5;font-size:11px">{platform_summary}</p>
  </div>
  <div style="padding:16px 20px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;background:#f1f5f9">
    <div style="background:white;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#3b82f6">{total}</div>
      <div style="font-size:11px;color:#64748b;margin-top:2px">今日聲量</div>
    </div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#22c55e">{pos}</div>
      <div style="font-size:11px;color:#64748b;margin-top:2px">🟢 正面</div>
    </div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#ef4444">{neg}</div>
      <div style="font-size:11px;color:#64748b;margin-top:2px">🔴 負面</div>
    </div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:{score_color}">{score_str}</div>
      <div style="font-size:11px;color:#64748b;margin-top:2px">情緒指數</div>
    </div>
  </div>
  <div style="padding:16px 20px;overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;min-width:600px">
      <thead>
        <tr style="background:#f8fafc">
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">平台</th>
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">候選人</th>
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">維度</th>
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">標題</th>
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">情緒</th>
          <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">連結</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="padding:14px 20px;background:#f8fafc;font-size:11px;color:#94a3b8;text-align:center;border-top:1px solid #e2e8f0">
    選情雷達自動產生 · 來源：Google / FB / IG / Threads / PTT / Google News（僅限今日）
  </div>
</div>
</body></html>"""


# ─── 主流程 ──────────────────────────────────────────────

def main():
    run_time  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    api_count = 0
    print(f"\n{'='*55}")
    print(f"  選情監控啟動 {run_time}（只收今日：{TODAY}）")
    print(f"{'='*55}")

    all_results = []

    # ── 主力平台 ──────────────────────────────────────────
    for candidate in CANDIDATES:
        print(f"\n▶ {candidate}")
        for dimension in DIMENSIONS:
            keyword = f"{candidate} {dimension}"
            for platform, prefix in MAIN_PLATFORMS.items():
                items = search_google_custom(keyword, prefix, platform, num=3)
                api_count += 1
                if items:
                    print(f"  {platform} [{keyword}]: {len(items)} 筆")
                for item in items:
                    item.update({"candidate": candidate, "dimension": dimension, "time": run_time})
                all_results.extend(items)
                time.sleep(0.3)

            # Google News RSS（免費，只取今天）
            news = search_google_news_rss(keyword, num=3)
            if news:
                print(f"  Google News [{keyword}]: {len(news)} 筆（今日）")
            for item in news:
                item.update({"candidate": candidate, "dimension": dimension, "time": run_time})
            all_results.extend(news)

    # ── 補充平台 ──────────────────────────────────────────
    print("\n── 補充平台（IG / PTT）──")
    for candidate in CANDIDATES:
        for platform, prefix in EXTRA_PLATFORMS.items():
            items = search_google_custom(candidate, prefix, platform, num=3)
            api_count += 1
            if items:
                print(f"  {platform} [{candidate}]: {len(items)} 筆")
            for item in items:
                item.update({"candidate": candidate, "dimension": "綜合", "time": run_time})
            all_results.extend(items)
            time.sleep(0.3)

    print(f"\n📊 共蒐集 {len(all_results)} 筆 | API 用量 {api_count}/100 次")

    # ── 情緒分析 ──────────────────────────────────────────
    if all_results:
        print("\n🤖 Claude 情緒分析中...")
        for candidate in CANDIDATES:
            group = [r for r in all_results if r.get("candidate") == candidate]
            if group:
                analyze_sentiment(group, candidate)

    # ── 寫入 Sheets ───────────────────────────────────────
    if all_results:
        rows = [[
            r["time"], r["candidate"], r.get("dimension", ""),
            r["platform"], r["title"], r["summary"],
            r.get("sentiment", "中立"), r.get("reason", ""), r["url"],
        ] for r in all_results]
        append_to_sheet(rows)
    else:
        print("⚠️  今日無資料")

    # ── Email ─────────────────────────────────────────────
    html = build_email_html(all_results, run_time, api_count)
    send_email(f"📡 選情日報 {run_time}｜今日 {len(all_results)} 筆", html)

    print(f"\n✅ 完成！API 總用量：{api_count}/100 次")


if __name__ == "__main__":
    main()
