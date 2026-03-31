"""
選情監控系統 v2 - 4層資料架構
Layer 1: RawResults   - 原始抓取資料
Layer 2: Processed    - 清理後資料
Layer 3: Report       - 分析報告
Layer 4: ErrorLog     - 錯誤記錄

搜尋來源：Serper.dev (Google/FB/IG/Threads/PTT) + Google News RSS
"""

import os, json, time, datetime, smtplib, requests, hashlib, re, uuid
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── 設定區 ───
SERPER_API_KEY = os.environ["SERPER_API_KEY"].strip()
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_USER     = os.environ["GMAIL_USER"].strip()
GMAIL_PASS     = os.environ["GMAIL_PASS"].strip()
NOTIFY_EMAIL   = os.environ["NOTIFY_EMAIL"].strip()
SHEET_ID       = os.environ["GOOGLE_SHEET_ID"].strip()

TODAY  = datetime.date.today().isoformat()
RUN_ID = str(uuid.uuid4())[:8]

# ─── 監控目標（只抓這兩位）───
CANDIDATES = ["黃柏瑜", "陳素月"]

DIMENSIONS = ["政見", "爭議", "支持", "批評"]

PLATFORMS = {
    "Google":  "",
    "FB":      "site:facebook.com",
    "IG":      "site:instagram.com",
    "Threads": "site:threads.net",
    "PTT":     "site:ptt.cc",
}

# ─── HTML 清理工具 ────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)
    def get_text(self):
        return " ".join(self.text).strip()

def strip_html(text: str) -> str:
    if not text: return ""
    try:
        s = HTMLStripper()
        s.feed(text)
        clean = s.get_text()
    except:
        clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:500]

def normalize_url(url: str) -> str:
    url = url.strip()
    url = re.sub(r'[?&](utm_[^&]+|oc=\d+)', '', url)
    return url

def text_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]

# ─── Layer 1: RawResults ─────────────────────────────────

def fetch_serper(keyword: str, site_prefix: str, platform: str) -> list[dict]:
    """抓取 Serper.dev 原始資料"""
    query = f"{site_prefix} {keyword}".strip() if site_prefix else keyword
    raw_results = []
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5, "gl": "tw", "hl": "zh-tw", "tbs": "qdr:d"},
            timeout=10,
        )
        r.raise_for_status()
        for i, item in enumerate(r.json().get("organic", [])):
            raw_results.append({
                "run_id":       RUN_ID,
                "fetched_at":   datetime.datetime.now().isoformat(),
                "keyword":      keyword,
                "source":       platform,
                "title_raw":    item.get("title", ""),
                "snippet_raw":  item.get("snippet", ""),
                "url_raw":      item.get("link", ""),
                "raw_text":     json.dumps(item, ensure_ascii=False),
                "fetch_status": "ok",
            })
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": platform,
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "raw_text": "", "fetch_status": f"error:{str(e)[:100]}",
        })
    return raw_results

def fetch_google_news_rss(keyword: str) -> list[dict]:
    """抓取 Google News RSS 原始資料"""
    raw_results = []
    url = f"https://news.google.com/rss/search?q={quote(keyword)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:10]:
            pub_date = item.findtext("pubDate", "")
            # 只取今天的新聞
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    if parsedate_to_datetime(pub_date).date().isoformat() != TODAY:
                        continue
                except: pass
            raw_results.append({
                "run_id":       RUN_ID,
                "fetched_at":   datetime.datetime.now().isoformat(),
                "keyword":      keyword,
                "source":       "Google News",
                "title_raw":    item.findtext("title", ""),
                "snippet_raw":  item.findtext("description", ""),
                "url_raw":      item.findtext("link", ""),
                "raw_text":     "",
                "fetch_status": "ok",
            })
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": "Google News",
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "raw_text": "", "fetch_status": f"error:{str(e)[:100]}",
        })
    return raw_results

# ─── Layer 2: Processed ──────────────────────────────────

def process_raw(raw: dict, raw_id: int) -> dict | None:
    """清理原始資料"""
    if raw.get("fetch_status", "").startswith("error"):
        return None
    title_clean   = strip_html(raw.get("title_raw", ""))
    snippet_clean = strip_html(raw.get("snippet_raw", ""))
    text_clean    = f"{title_clean} {snippet_clean}".strip()
    if not title_clean:
        return None
    return {
        "raw_id":        raw_id,
        "source":        raw.get("source", ""),
        "keyword":       raw.get("keyword", ""),
        "fetched_at":    raw.get("fetched_at", ""),
        "normalized_url": normalize_url(raw.get("url_raw", "")),
        "title_clean":   title_clean,
        "text_clean":    text_clean,
        "text_len":      len(text_clean),
        "hash":          text_hash(text_clean),
        "parser_status": "ok",
    }

# ─── Layer 3: Report（Claude 分析）──────────────────────

def analyze_to_report(processed_items: list[dict], candidate: str) -> list[dict]:
    """用 Claude 分析，產生 Report 層資料"""
    if not processed_items: return []

    content_list = "\n".join(
        f"{i+1}. 標題：{it['title_clean']}\n   摘要：{it['text_clean'][:200]}"
        for i, it in enumerate(processed_items)
    )

    prompt = f"""你是台灣選情分析專家，請分析以下關於候選人「{candidate}」的新聞和社群資料。

{content_list}

請針對每一筆資料分析：
1. **topic**：主要議題（10字內，例如：政見發表、選舉爭議、民調支持）
2. **stance**：媒體/發文者對{candidate}的立場（pro=支持、against=反對、neutral=中立）
3. **sentiment**：整體情緒（正面/中立/負面）
4. **priority**：重要程度（high/medium/low）
5. **summary**：一句話摘要（20字內）

注意：情緒判斷以「對{candidate}的影響」為準，而非文章本身的語氣。

以 JSON 陣列回覆，每個元素包含：index, topic, stance, sentiment, priority, summary
只回傳 JSON，不要其他文字。"""

    reports = []
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json","").replace("```","").strip()
        analyses = json.loads(text)
        sent_map = {a["index"]: a for a in analyses}

        for i, item in enumerate(processed_items):
            a = sent_map.get(i+1, {})
            reports.append({
                "keyword":      item["keyword"],
                "topic":        a.get("topic", ""),
                "stance":       a.get("stance", "neutral"),
                "sentiment":    a.get("sentiment", "中立"),
                "summary":      a.get("summary", ""),
                "priority":     a.get("priority", "low"),
                "source":       item["source"],
                "url":          item["normalized_url"],
                "title":        item["title_clean"],
                "published_at": item["fetched_at"][:10],
                "text_clean":   item["text_clean"],
            })
    except Exception as e:
        print(f"  [Claude 分析錯誤] {e}")
        for item in processed_items:
            reports.append({
                "keyword": item["keyword"], "topic": "", "stance": "neutral",
                "sentiment": "中立", "summary": "", "priority": "low",
                "source": item["source"], "url": item["normalized_url"],
                "title": item["title_clean"], "published_at": item["fetched_at"][:10],
                "text_clean": item["text_clean"],
            })
    return reports

# ─── Layer 4: ErrorLog ───────────────────────────────────

def log_error(stage: str, source: str, keyword: str, error_code: str, message: str, payload: str = "") -> dict:
    return {
        "run_id":       RUN_ID,
        "stage":        stage,
        "source":       source,
        "keyword":      keyword,
        "error_code":   error_code,
        "error_message": message[:200],
        "raw_payload":  payload[:500],
    }

# ─── 寫入 Google Sheets ──────────────────────────────────

def write_to_sheets(reports: list[dict], error_logs: list[dict]) -> bool:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        sa_info = json.loads(os.environ["GOOGLE_SA_JSON"].strip())
        creds   = Credentials.from_service_account_info(sa_info, scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ])
        gc      = gspread.authorize(creds)
        wb      = gc.open_by_key(SHEET_ID)

        # Report 工作表
        try:
            ws_report = wb.worksheet("Report")
        except:
            ws_report = wb.add_worksheet("Report", 1000, 15)
            ws_report.append_row(["時間","候選人","議題","立場","情緒","重要度","摘要","平台","標題","連結","發布日期"])

        report_rows = [[
            TODAY, r["keyword"], r["topic"], r["stance"], r["sentiment"],
            r["priority"], r["summary"], r["source"], r["title"],
            r["url"], r["published_at"]
        ] for r in reports]
        if report_rows:
            ws_report.append_rows(report_rows)

        # ErrorLog 工作表
        if error_logs:
            try:
                ws_err = wb.worksheet("ErrorLog")
            except:
                ws_err = wb.add_worksheet("ErrorLog", 500, 8)
                ws_err.append_row(["run_id","stage","source","keyword","error_code","error_message","raw_payload","時間"])
            err_rows = [[e["run_id"],e["stage"],e["source"],e["keyword"],e["error_code"],e["error_message"],e["raw_payload"],TODAY] for e in error_logs]
            ws_err.append_rows(err_rows)

        print(f"[Sheets] ✅ Report: {len(report_rows)} 筆, ErrorLog: {len(error_logs)} 筆")
        return True
    except Exception as e:
        print(f"[Sheets 錯誤] {e}")
        return False

# ─── Email 通知 ──────────────────────────────────────────

def send_email_report(reports: list[dict], run_time: str):
    try:
        pos    = sum(1 for r in reports if r["sentiment"] == "正面")
        neg    = sum(1 for r in reports if r["sentiment"] == "負面")
        total  = len(reports)
        score  = round(((pos-neg)/total*100)) if total else 0
        pc     = {}
        for r in reports: pc[r["source"]] = pc.get(r["source"], 0) + 1
        ps = " ｜ ".join(f"{k} {v}筆" for k,v in sorted(pc.items()))

        rows_html = ""
        high_priority = [r for r in reports if r.get("priority") == "high"]
        display = high_priority[:20] if high_priority else reports[:20]
        for r in display:
            sc = "#22c55e" if r["sentiment"]=="正面" else "#ef4444" if r["sentiment"]=="負面" else "#94a3b8"
            em = "🟢" if r["sentiment"]=="正面" else "🔴" if r["sentiment"]=="負面" else "⚪"
            st = "🔥" if r.get("priority")=="high" else ""
            rows_html += f"""<tr>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px">{st}{r['source']}</td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px;font-weight:600">{r['keyword']}</td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px">{r['topic']}</td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px">{r['title'][:50]}...</td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0"><span style="color:{sc};font-weight:700">{em} {r['sentiment']}</span></td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px">{r.get('summary','')}</td>
              <td style="padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px"><a href="{r['url']}" style="color:#3b82f6">查看</a></td>
            </tr>"""

        sc_color = "#22c55e" if score>0 else "#ef4444" if score<0 else "#94a3b8"
        sc_str   = f"+{score}" if score>0 else str(score)
        html = f"""<!DOCTYPE html><html><body style="font-family:'Microsoft JhengHei',sans-serif;background:#f8fafc;padding:20px">
<div style="max-width:900px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.1)">
  <div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px;color:white">
    <h1 style="margin:0;font-size:22px">📡 選情監控日報</h1>
    <p style="margin:6px 0 0;opacity:0.6;font-size:12px">{run_time}｜監控：黃柏瑜、陳素月</p>
    <p style="margin:4px 0 0;opacity:0.5;font-size:11px">{ps}</p>
  </div>
  <div style="padding:16px 20px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;background:#f1f5f9">
    <div style="background:white;border-radius:8px;padding:14px;text-align:center"><div style="font-size:28px;font-weight:900;color:#3b82f6">{total}</div><div style="font-size:11px;color:#64748b">今日聲量</div></div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center"><div style="font-size:28px;font-weight:900;color:#22c55e">{pos}</div><div style="font-size:11px;color:#64748b">🟢 正面</div></div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center"><div style="font-size:28px;font-weight:900;color:#ef4444">{neg}</div><div style="font-size:11px;color:#64748b">🔴 負面</div></div>
    <div style="background:white;border-radius:8px;padding:14px;text-align:center"><div style="font-size:28px;font-weight:900;color:{sc_color}">{sc_str}</div><div style="font-size:11px;color:#64748b">情緒指數</div></div>
  </div>
  <div style="padding:16px 20px;overflow-x:auto">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="background:#f8fafc">
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">平台</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">候選人</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">議題</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">標題</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">情緒</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">摘要</th>
        <th style="padding:10px 8px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0">連結</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="padding:14px 20px;background:#f8fafc;font-size:11px;color:#94a3b8;text-align:center">選情雷達 v2 · 來源：Serper.dev (Google/FB/IG/Threads/PTT) + Google News</div>
</div></body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📡 選情日報 {run_time}｜{total} 筆｜黃柏瑜 + 陳素月"
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"[Email] ✅ → {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"[Email 錯誤] {e}")

# ─── 主流程 ──────────────────────────────────────────────

def main():
    run_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  選情監控 v2 啟動 {run_time}")
    print(f"  Run ID: {RUN_ID}")
    print(f"  候選人: {CANDIDATES}")
    print(f"  搜尋引擎: Serper.dev + Google News RSS")
    print(f"{'='*60}")

    all_raw_results   = []  # Layer 1
    all_processed     = []  # Layer 2
    all_reports       = []  # Layer 3
    all_error_logs    = []  # Layer 4
    seen_hashes       = set()

    # ── Layer 1: 抓取原始資料 ────────────────────────────
    print("\n[Layer 1] 抓取原始資料...")
    for candidate in CANDIDATES:
        print(f"\n  ▶ {candidate}")
        for dimension in DIMENSIONS:
            keyword = f"{candidate} {dimension}"
            # Serper 各平台
            for platform, prefix in PLATFORMS.items():
                raw_list = fetch_serper(keyword, prefix, platform)
                all_raw_results.extend(raw_list)
                ok = sum(1 for r in raw_list if r["fetch_status"] == "ok")
                if ok: print(f"    {platform}: {ok} 筆")
                time.sleep(0.2)
            # Google News RSS
            rss_list = fetch_google_news_rss(keyword)
            all_raw_results.extend(rss_list)
            ok = sum(1 for r in rss_list if r["fetch_status"] == "ok")
            if ok: print(f"    Google News: {ok} 筆（今日）")

    print(f"\n  原始資料總計: {len(all_raw_results)} 筆")

    # ── Layer 2: 清理資料 ────────────────────────────────
    print("\n[Layer 2] 清理資料...")
    for i, raw in enumerate(all_raw_results):
        if raw["fetch_status"].startswith("error"):
            all_error_logs.append(log_error(
                "fetch", raw["source"], raw["keyword"],
                raw["fetch_status"], raw["fetch_status"]
            ))
            continue
        processed = process_raw(raw, i)
        if processed:
            if processed["hash"] not in seen_hashes:
                seen_hashes.add(processed["hash"])
                all_processed.append(processed)

    print(f"  清理後: {len(all_processed)} 筆（去重後）")

    # ── Layer 3: 分析報告 ────────────────────────────────
    print("\n[Layer 3] Claude 情緒分析...")
    for candidate in CANDIDATES:
        group = [p for p in all_processed if candidate in p["keyword"]]
        if group:
            reports = analyze_to_report(group, candidate)
            all_reports.extend(reports)
            print(f"  {candidate}: {len(reports)} 筆分析完成")

    print(f"  報告總計: {len(all_reports)} 筆")

    # ── Layer 4 + 寫入 Sheets ────────────────────────────
    print("\n[Layer 4] 寫入 Sheets...")
    write_to_sheets(all_reports, all_error_logs)

    # ── 發送 Email ───────────────────────────────────────
    send_email_report(all_reports, run_time)

    print(f"\n✅ 完成！")
    print(f"  原始: {len(all_raw_results)} → 清理: {len(all_processed)} → 報告: {len(all_reports)}")
    print(f"  錯誤: {len(all_error_logs)} 筆")

if __name__ == "__main__":
    main()
