"""
選情監控系統 - 主程式
功能：搜尋 Google + 爬取 Threads，用 Claude AI 分析情緒，寫入 Google Sheets，發送 Email 通知
"""

import os
import json
import time
import datetime
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── 設定區（從環境變數讀取，不要直接寫金鑰在這裡）───
GOOGLE_API_KEY   = os.environ["GOOGLE_API_KEY"]
GOOGLE_CX_ID     = os.environ["GOOGLE_CX_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER       = os.environ["GMAIL_USER"]
GMAIL_PASS       = os.environ["GMAIL_PASS"]
NOTIFY_EMAIL     = os.environ["NOTIFY_EMAIL"]
SHEET_ID         = os.environ["GOOGLE_SHEET_ID"]

# ─── 監控目標設定（在這裡修改你要監控的候選人/議題）───
CANDIDATES = [
    "陳素月",      # 候選人/議題名稱
    # "黃柏瑜",    # 新增更多只要取消 # 號
    # "婦幼政策",
]

DIMENSIONS = [
    "政見",
    "爭議",
    "支持",
    "批評",
]

# ─── Google Custom Search ───────────────────────────────

def search_google(keyword: str, num: int = 10) -> list[dict]:
    """搜尋 Google，回傳結果清單"""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx":  GOOGLE_CX_ID,
        "q":   keyword,
        "num": num,
        "hl":  "zh-TW",
        "dateRestrict": "d1",   # 只搜今天
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        items = r.json().get("items", [])
        return [
            {
                "platform": "Google",
                "title":    i.get("title", ""),
                "summary":  i.get("snippet", ""),
                "url":      i.get("link", ""),
            }
            for i in items
        ]
    except Exception as e:
        print(f"[Google Search 錯誤] {keyword}: {e}")
        return []


# ─── Threads 爬蟲（不需要 API）──────────────────────────

def search_threads(keyword: str) -> list[dict]:
    """爬取 Threads 搜尋結果（用公開網頁）"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    results = []
    try:
        # Threads 公開搜尋頁
        url = f"https://www.threads.net/search?q={requests.utils.quote(keyword)}&serp_type=default"
        r = requests.get(url, headers=headers, timeout=15)

        # 簡單解析：抓取 og:title / og:description meta 標籤
        from html.parser import HTMLParser

        class MetaParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.posts = []
                self._current = {}

            def handle_starttag(self, tag, attrs):
                if tag == "meta":
                    d = dict(attrs)
                    prop = d.get("property", "") or d.get("name", "")
                    content = d.get("content", "")
                    if prop == "og:title" and content:
                        self._current["title"] = content
                    elif prop == "og:description" and content:
                        self._current["summary"] = content
                        if "title" in self._current:
                            self.posts.append(dict(self._current))
                            self._current = {}

        parser = MetaParser()
        parser.feed(r.text)
        for p in parser.posts[:5]:
            results.append({
                "platform": "Threads",
                "title":    p.get("title", ""),
                "summary":  p.get("summary", ""),
                "url":      url,
            })

    except Exception as e:
        print(f"[Threads 爬蟲錯誤] {keyword}: {e}")

    # 若爬蟲取不到資料，改用 Google 搜尋 Threads 網域
    if not results:
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": GOOGLE_API_KEY,
                "cx":  GOOGLE_CX_ID,
                "q":   f"site:threads.net {keyword}",
                "num": 5,
            }
            r = requests.get(url, params=params, timeout=10)
            items = r.json().get("items", [])
            for i in items:
                results.append({
                    "platform": "Threads",
                    "title":    i.get("title", ""),
                    "summary":  i.get("snippet", ""),
                    "url":      i.get("link", ""),
                })
        except Exception as e:
            print(f"[Threads via Google 錯誤] {keyword}: {e}")

    return results


# ─── Claude AI 情緒分析 ──────────────────────────────────

def analyze_sentiment(items: list[dict], candidate: str) -> list[dict]:
    """呼叫 Claude API，批次分析情緒"""
    if not items:
        return []

    # 把所有標題+摘要整理成一個 prompt
    content_list = "\n".join(
        f"{i+1}. 標題：{it['title']}\n   摘要：{it['summary']}"
        for i, it in enumerate(items)
    )

    prompt = f"""你是台灣選情分析專家。以下是關於「{candidate}」的網路聲量資料，請分析每一筆的情緒傾向。

{content_list}

請以 JSON 陣列回覆，每個元素包含：
- index: 編號（從1開始）
- sentiment: "正面" / "中立" / "負面"
- reason: 判斷理由（10字內）

只回傳 JSON 陣列，不要其他文字。"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",  # 用最便宜的模型做情緒判定
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        sentiments = json.loads(text)

        # 把情緒結果合併回原始資料
        sent_map = {s["index"]: s for s in sentiments}
        for i, item in enumerate(items):
            s = sent_map.get(i + 1, {})
            item["sentiment"] = s.get("sentiment", "中立")
            item["reason"]    = s.get("reason", "")

    except Exception as e:
        print(f"[Claude 分析錯誤]: {e}")
        for item in items:
            item["sentiment"] = "中立"
            item["reason"]    = "分析失敗"

    return items


# ─── 寫入 Google Sheets ──────────────────────────────────

def append_to_sheet(rows: list[list]) -> bool:
    """把資料附加到 Google Sheets（使用公開可編輯的 Sheet）"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A1:append"
    # 注意：這裡需要 OAuth，改用 gspread 套件更簡單
    # 下方用 gspread + service account 的方式
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        # 從環境變數讀取 Service Account JSON
        sa_info = json.loads(os.environ["GOOGLE_SA_JSON"])
        creds = Credentials.from_service_account_info(
            sa_info,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"],
        )
        gc     = gspread.authorize(creds)
        sheet  = gc.open_by_key(SHEET_ID).sheet1

        # 如果是第一行，先寫標頭
        if sheet.row_count == 0 or sheet.cell(1, 1).value is None:
            sheet.append_row(["時間", "候選人", "維度", "平台", "標題", "摘要", "情緒", "原因", "來源連結"])

        sheet.append_rows(rows)
        print(f"[Sheets] 成功寫入 {len(rows)} 筆")
        return True

    except Exception as e:
        print(f"[Sheets 錯誤]: {e}")
        return False


# ─── 發送 Email 通知 ─────────────────────────────────────

def send_email(subject: str, html_body: str):
    """用 Gmail SMTP 發送通知"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"[Email] 通知發送成功 → {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"[Email 錯誤]: {e}")


def build_email_html(results: list[dict], run_time: str) -> str:
    """產生 Email HTML 報告"""
    pos = sum(1 for r in results if r["sentiment"] == "正面")
    neu = sum(1 for r in results if r["sentiment"] == "中立")
    neg = sum(1 for r in results if r["sentiment"] == "負面")
    total = len(results)
    score = round(((pos - neg) / total * 100)) if total else 0

    rows_html = ""
    for r in results:
        color = "#22c55e" if r["sentiment"] == "正面" else "#ef4444" if r["sentiment"] == "負面" else "#94a3b8"
        rows_html += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#64748b">{r.get('platform','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px">{r.get('candidate','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px">{r.get('dimension','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:13px;max-width:300px">{r.get('title','')[:60]}...</td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0"><span style="color:{color};font-weight:700">{r.get('sentiment','')}</span></td>
          <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px"><a href="{r.get('url','')}" style="color:#3b82f6">查看</a></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><body style="font-family:'Microsoft JhengHei',sans-serif;background:#f8fafc;padding:20px">
<div style="max-width:800px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.1)">
  <div style="background:linear-gradient(135deg,#1e293b,#334155);padding:24px;color:white">
    <h1 style="margin:0;font-size:20px">📡 選情監控日報</h1>
    <p style="margin:8px 0 0;opacity:0.7;font-size:13px">{run_time} 自動產生</p>
  </div>
  <div style="padding:20px;display:grid;grid-template-columns:repeat(4,1fr);gap:12px;background:#f1f5f9">
    <div style="background:white;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:900;color:#3b82f6">{total}</div>
      <div style="font-size:12px;color:#64748b">總聲量</div>
    </div>
    <div style="background:white;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:900;color:#22c55e">{pos}</div>
      <div style="font-size:12px;color:#64748b">正面</div>
    </div>
    <div style="background:white;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:900;color:#ef4444">{neg}</div>
      <div style="font-size:12px;color:#64748b">負面</div>
    </div>
    <div style="background:white;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:24px;font-weight:900;color:{'#22c55e' if score>0 else '#ef4444' if score<0 else '#94a3b8'}">{'+' if score>0 else ''}{score}</div>
      <div style="font-size:12px;color:#64748b">情緒指數</div>
    </div>
  </div>
  <div style="padding:20px">
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f8fafc">
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">平台</th>
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">候選人</th>
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">維度</th>
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">標題</th>
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">情緒</th>
          <th style="padding:10px 8px;text-align:left;font-size:11px;color:#64748b;border-bottom:2px solid #e2e8f0">連結</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="padding:16px 20px;background:#f8fafc;font-size:12px;color:#94a3b8;text-align:center">
    由選情雷達自動產生 · 資料來源：Google、Threads
  </div>
</div>
</body></html>"""


# ─── 主流程 ──────────────────────────────────────────────

def main():
    run_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*50}")
    print(f"選情監控啟動 {run_time}")
    print(f"{'='*50}")

    all_results = []

    for candidate in CANDIDATES:
        print(f"\n▶ 處理候選人：{candidate}")

        for dimension in DIMENSIONS:
            keyword = f"{candidate} {dimension}"
            print(f"  🔍 搜尋：{keyword}")

            # 1. Google 搜尋
            google_items = search_google(keyword)
            time.sleep(1)  # 避免速率限制

            # 2. Threads 搜尋
            threads_items = search_threads(keyword)
            time.sleep(1)

            items = google_items + threads_items
            print(f"     取得 {len(items)} 筆（Google:{len(google_items)} Threads:{len(threads_items)}）")

            # 3. Claude 情緒分析
            if items:
                items = analyze_sentiment(items, candidate)

            # 加上候選人和維度標記
            for item in items:
                item["candidate"] = candidate
                item["dimension"] = dimension
                item["time"]      = run_time

            all_results.extend(items)

    print(f"\n共蒐集 {len(all_results)} 筆資料")

    # 4. 寫入 Google Sheets
    if all_results:
        rows = [
            [
                r["time"], r["candidate"], r["dimension"],
                r["platform"], r["title"], r["summary"],
                r["sentiment"], r.get("reason", ""), r["url"],
            ]
            for r in all_results
        ]
        append_to_sheet(rows)

    # 5. 發送 Email 通知
    html = build_email_html(all_results, run_time)
    send_email(f"📡 選情日報 {run_time}｜共 {len(all_results)} 筆", html)

    print("\n✅ 完成！")


if __name__ == "__main__":
    main()
