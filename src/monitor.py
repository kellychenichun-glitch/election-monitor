"""
選情監控系統 v3
改善：
1. 搜尋關鍵字加地名（彰化/員林），減少無關結果
2. URL 去重（同一篇文章只保留一筆）
3. Claude 分析前加候選人背景，並過濾無關資料
4. Sheets：每天一個分頁，按時間排序（最新在最上面）
"""

import os, json, time, datetime, smtplib, requests, hashlib, re, uuid
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SERPER_API_KEY = os.environ["SERPER_API_KEY"].strip()
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"].strip()
GMAIL_USER     = os.environ["GMAIL_USER"].strip()
GMAIL_PASS     = os.environ["GMAIL_PASS"].strip()
NOTIFY_EMAIL   = os.environ["NOTIFY_EMAIL"].strip()
SHEET_ID       = os.environ["GOOGLE_SHEET_ID"].strip()

TODAY  = datetime.date.today().isoformat()
RUN_ID = str(uuid.uuid4())[:8]

CANDIDATES = {
    "黃柏瑜": {
        "description": "黃柏瑜是台灣民進黨彰化市長候選人，現任彰化市議員",
        "location": "彰化",
        "keywords": ["黃柏瑜 彰化"],
    },
    "陳素月": {
        "description": "陳素月是台灣民進黨員林市長候選人，現任員林市長",
        "location": "員林",
        "keywords": ["陳素月 員林"],
    },
}

DIMENSIONS = ["政見", "爭議", "支持", "批評"]

PLATFORMS = {
    "Google":  "",
    "FB":      "site:facebook.com",
    "IG":      "site:instagram.com",
    "Threads": "site:threads.net",
    "PTT":     "site:ptt.cc",
}

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)
    def get_text(self):
        return " ".join(self.text).strip()

def strip_html(text):
    if not text:
        return ""
    try:
        s = HTMLStripper()
        s.feed(text)
        clean = s.get_text()
    except Exception:
        clean = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', clean).strip()[:500]

def normalize_url(url):
    url = url.strip()
    url = re.sub(r'[?&](utm_[^&]+|oc=\d+)', '', url)
    url = re.sub(r'[?&]$', '', url)
    return url

def url_hash(url):
    return hashlib.md5(normalize_url(url).encode()).hexdigest()[:12]

def fetch_serper(keyword, site_prefix, platform):
    query = (site_prefix + " " + keyword).strip() if site_prefix else keyword
    raw_results = []
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5, "gl": "tw", "hl": "zh-tw", "tbs": "qdr:d"},
            timeout=10,
        )
        r.raise_for_status()
        for item in r.json().get("organic", []):
            raw_results.append({
                "run_id": RUN_ID,
                "fetched_at": datetime.datetime.now().isoformat(),
                "keyword": keyword, "source": platform,
                "title_raw": item.get("title", ""),
                "snippet_raw": item.get("snippet", ""),
                "url_raw": item.get("link", ""),
                "fetch_status": "ok",
            })
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID,
            "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": platform,
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "fetch_status": "error:" + str(e)[:100],
        })
    return raw_results

def fetch_google_news_rss(keyword):
    raw_results = []
    url = "https://news.google.com/rss/search?q=" + quote(keyword) + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:10]:
            pub_date = item.findtext("pubDate", "")
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    if parsedate_to_datetime(pub_date).date().isoformat() != TODAY:
                        continue
                except Exception:
                    pass
            raw_results.append({
                "run_id": RUN_ID,
                "fetched_at": datetime.datetime.now().isoformat(),
                "keyword": keyword, "source": "Google News",
                "title_raw": item.findtext("title", ""),
                "snippet_raw": item.findtext("description", ""),
                "url_raw": item.findtext("link", ""),
                "fetch_status": "ok",
            })
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID,
            "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": "Google News",
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "fetch_status": "error:" + str(e)[:100],
        })
    return raw_results

def process_raw(raw, raw_id):
    if raw.get("fetch_status", "").startswith("error"):
        return None
    title_clean = strip_html(raw.get("title_raw", ""))
    snippet_clean = strip_html(raw.get("snippet_raw", ""))
    text_clean = (title_clean + " " + snippet_clean).strip()
    if not title_clean:
        return None
    return {
        "raw_id": raw_id,
        "source": raw.get("source", ""),
        "keyword": raw.get("keyword", ""),
        "candidate": raw.get("candidate", ""),
        "fetched_at": raw.get("fetched_at", ""),
        "normalized_url": normalize_url(raw.get("url_raw", "")),
        "url_hash": url_hash(raw.get("url_raw", "")),
        "title_clean": title_clean,
        "text_clean": text_clean,
        "text_len": len(text_clean),
        "parser_status": "ok",
    }

def analyze_to_report(processed_items, candidate, candidate_info):
    if not processed_items:
        return []
    description = candidate_info.get("description", "")
    content_list = "\n".join(
        str(i+1) + ". 標題：" + it["title_clean"] + "\n   來源：" + it["source"] + "\n   摘要：" + it["text_clean"][:200]
        for i, it in enumerate(processed_items)
    )
    prompt = (
        "你是台灣選情分析專家。\n"
        "候選人背景：" + description + "\n\n"
        "以下是搜尋到的資料，請先判斷每筆是否與此候選人直接相關：\n\n"
        + content_list
        + "\n\n針對每筆資料回答：\n"
        "1. relevant：是否與「" + candidate + "」直接相關（true/false）。若是同名不同人、商品廣告、無關政治內容，標為 false\n"
        "2. topic：主要議題（10字內，若 relevant=false 填 '無關'）\n"
        "3. stance：對" + candidate + "的立場（pro=支持、against=反對、neutral=中立）\n"
        "4. sentiment：對" + candidate + "的影響（正面/中立/負面）\n"
        "5. priority：重要度（high/medium/low）\n"
        "6. summary：一句話摘要（20字內）\n\n"
        "JSON陣列回覆，每元素含：index, relevant, topic, stance, sentiment, priority, summary\n只回JSON。"
    )
    reports = []
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        sent_map = {a["index"]: a for a in json.loads(text)}
        skipped = 0
        for i, item in enumerate(processed_items):
            a = sent_map.get(i + 1, {})
            if not a.get("relevant", True):
                skipped += 1
                continue
            reports.append({
                "keyword": item["keyword"], "candidate": candidate,
                "topic": a.get("topic",""), "stance": a.get("stance","neutral"),
                "sentiment": a.get("sentiment","中立"), "summary": a.get("summary",""),
                "priority": a.get("priority","low"), "source": item["source"],
                "url": item["normalized_url"], "title": item["title_clean"],
                "published_at": item["fetched_at"][:10],
            })
        if skipped:
            print("  過濾無關: " + str(skipped) + "筆")
    except Exception as e:
        print("  [Claude錯誤] " + str(e))
        for item in processed_items:
            reports.append({
                "keyword": item["keyword"], "candidate": candidate,
                "topic": "", "stance": "neutral", "sentiment": "中立",
                "summary": "", "priority": "low", "source": item["source"],
                "url": item["normalized_url"], "title": item["title_clean"],
                "published_at": item["fetched_at"][:10],
            })
    return reports

def write_to_sheets(reports, error_logs):
    for attempt in range(3):
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            sa_info = json.loads(os.environ["GOOGLE_SA_JSON"].strip())
            creds = Credentials.from_service_account_info(sa_info, scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ])
            gc = gspread.authorize(creds)
            wb = gc.open_by_key(SHEET_ID)
            sheet_name = TODAY
            try:
                ws = wb.worksheet(sheet_name)
            except Exception:
                ws = wb.add_worksheet(sheet_name, 1000, 12)
                ws.append_row(["時間","候選人","議題","立場","情緒","重要度","摘要","平台","標題","連結","發布日期"])
                try:
                    all_sheets = wb.worksheets()
                    wb.reorder_worksheets([ws] + [s for s in all_sheets if s.title != sheet_name])
                except Exception:
                    pass
            sorted_reports = sorted(reports, key=lambda x: x.get("published_at",""), reverse=True)
            now_time = datetime.datetime.now().strftime("%H:%M:%S")
            report_rows = [[
                now_time, r["candidate"], r["topic"], r["stance"], r["sentiment"],
                r["priority"], r["summary"], r["source"], r["title"],
                r["url"], r["published_at"],
            ] for r in sorted_reports]
            if report_rows:
                ws.insert_rows(report_rows, row=2)
            if error_logs:
                try:
                    ws_err = wb.worksheet("ErrorLog")
                except Exception:
                    ws_err = wb.add_worksheet("ErrorLog", 500, 8)
                    ws_err.append_row(["run_id","stage","source","keyword","error_code","error_message","raw_payload","時間"])
                ws_err.append_rows([[e["run_id"],e["stage"],e["source"],e["keyword"],e["error_code"],e["error_message"],e["raw_payload"],TODAY] for e in error_logs])
            print("[Sheets] 成功！[" + sheet_name + "] 寫入 " + str(len(report_rows)) + "筆")
            return True
        except Exception as e:
            print("[Sheets錯誤] 第" + str(attempt+1) + "次: " + str(e))
            if attempt < 2:
                time.sleep(5)
    print("[Sheets] 3次失敗")
    return False

def send_email_report(reports, run_time):
    try:
        pos = sum(1 for r in reports if r["sentiment"] == "正面")
        neg = sum(1 for r in reports if r["sentiment"] == "負面")
        total = len(reports)
        score = round(((pos-neg)/total*100)) if total else 0
        pc = {}
        for r in reports: pc[r["source"]] = pc.get(r["source"],0)+1
        ps = " | ".join(k+" "+str(v)+"筆" for k,v in sorted(pc.items()))
        rows_html = ""
        for r in sorted(reports, key=lambda x: (x.get("priority","")=="high", x.get("sentiment","")=="正面"), reverse=True)[:25]:
            sc = "#22c55e" if r["sentiment"]=="正面" else "#ef4444" if r["sentiment"]=="負面" else "#94a3b8"
            em = "🟢" if r["sentiment"]=="正面" else "🔴" if r["sentiment"]=="負面" else "⚪"
            flag = "🔥" if r.get("priority")=="high" else ""
            rows_html += "<tr><td style='padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px'>"+flag+r["source"]+"</td><td style='padding:8px;border-bottom:1px solid #e2e8f0;font-weight:600'>"+r["candidate"]+"</td><td style='padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px'>"+r["topic"]+"</td><td style='padding:8px;border-bottom:1px solid #e2e8f0;font-size:12px'>"+r["title"][:50]+"...</td><td style='padding:8px;border-bottom:1px solid #e2e8f0'><span style='color:"+sc+";font-weight:700'>"+em+" "+r["sentiment"]+"</span></td><td style='padding:8px;border-bottom:1px solid #e2e8f0;font-size:11px'>"+r.get("summary","")+"</td><td style='padding:8px;border-bottom:1px solid #e2e8f0'><a href='"+r["url"]+"' style='color:#3b82f6'>查看</a></td></tr>"
        sc_color = "#22c55e" if score>0 else "#ef4444" if score<0 else "#94a3b8"
        sc_str = ("+") if score>0 else ""
        sc_str = sc_str + str(score)
        html = "<!DOCTYPE html><html><body style='font-family:Microsoft JhengHei,sans-serif;background:#f8fafc;padding:20px'><div style='max-width:900px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.1)'><div style='background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px;color:white'><h1 style='margin:0;font-size:22px'>📡 選情監控日報</h1><p style='margin:6px 0 0;opacity:0.6;font-size:12px'>"+run_time+" | 黃柏瑜（彰化）、陳素月（員林）</p><p style='margin:4px 0 0;opacity:0.5;font-size:11px'>"+ps+"</p></div><div style='padding:16px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;background:#f1f5f9'><div style='background:white;border-radius:8px;padding:14px;text-align:center'><div style='font-size:28px;font-weight:900;color:#3b82f6'>"+str(total)+"</div><div style='font-size:11px;color:#64748b'>今日聲量</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center'><div style='font-size:28px;font-weight:900;color:#22c55e'>"+str(pos)+"</div><div style='font-size:11px'>🟢 正面</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center'><div style='font-size:28px;font-weight:900;color:#ef4444'>"+str(neg)+"</div><div style='font-size:11px'>🔴 負面</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center'><div style='font-size:28px;font-weight:900;color:"+sc_color+"'>"+sc_str+"</div><div style='font-size:11px'>情緒指數</div></div></div><div style='padding:16px;overflow-x:auto'><table style='width:100%;border-collapse:collapse'><thead><tr style='background:#f8fafc'><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>平台</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>候選人</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>議題</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>標題</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>情緒</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>摘要</th><th style='padding:10px;text-align:left;font-size:10px;color:#94a3b8;border-bottom:2px solid #e2e8f0'>連結</th></tr></thead><tbody>"+rows_html+"</tbody></table></div><div style='padding:14px;background:#f8fafc;font-size:11px;color:#94a3b8;text-align:center'>選情雷達 v3 · Serper.dev + Google News · 已過濾無關資料</div></div></body></html>"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "📡 選情日報 "+run_time+"｜"+str(total)+"筆｜黃柏瑜+陳素月"
        msg["From"] = GMAIL_USER
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print("[Email] 成功 → " + NOTIFY_EMAIL)
    except Exception as e:
        print("[Email錯誤] " + str(e))

def main():
    run_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("="*60)
    print("  選情監控 v3 " + run_time)
    print("="*60)
    all_raw, all_processed, all_reports, all_errors = [], [], [], []
    seen_url_hashes = set()

    print("\n[Layer 1] 抓取...")
    for candidate, info in CANDIDATES.items():
        print("\n  ▶ " + candidate + "（" + info["location"] + "）")
        for base_kw in info["keywords"]:
            for dimension in DIMENSIONS:
                keyword = base_kw + " " + dimension
                for platform, prefix in PLATFORMS.items():
                    raw_list = fetch_serper(keyword, prefix, platform)
                    for rr in raw_list: rr["candidate"] = candidate
                    all_raw.extend(raw_list)
                    ok = sum(1 for r in raw_list if r["fetch_status"]=="ok")
                    if ok: print("    "+platform+" ["+keyword+"]: "+str(ok)+"筆")
                    time.sleep(0.2)
                rss_list = fetch_google_news_rss(keyword)
                for rr in rss_list: rr["candidate"] = candidate
                all_raw.extend(rss_list)
                ok = sum(1 for r in rss_list if r["fetch_status"]=="ok")
                if ok: print("    Google News ["+keyword+"]: "+str(ok)+"筆")

    print("\n  原始: "+str(len(all_raw))+"筆")

    print("\n[Layer 2] 清理（URL去重）...")
    for i, raw in enumerate(all_raw):
        if raw["fetch_status"].startswith("error"):
            all_errors.append({"run_id":RUN_ID,"stage":"fetch","source":raw["source"],"keyword":raw["keyword"],"error_code":raw["fetch_status"],"error_message":raw["fetch_status"],"raw_payload":""})
            continue
        p = process_raw(raw, i)
        if p and p["url_hash"] not in seen_url_hashes:
            seen_url_hashes.add(p["url_hash"])
            all_processed.append(p)
    print("  清理後: "+str(len(all_processed))+"筆")

    print("\n[Layer 3] Claude分析...")
    for candidate, info in CANDIDATES.items():
        group = [p for p in all_processed if p.get("candidate")==candidate]
        if group:
            rpts = analyze_to_report(group, candidate, info)
            all_reports.extend(rpts)
            print("  "+candidate+": "+str(len(rpts))+"筆（過濾後）")
    print("  報告: "+str(len(all_reports))+"筆")

    print("\n[Layer 4] 寫入Sheets...")
    write_to_sheets(all_reports, all_errors)
    send_email_report(all_reports, run_time)
    print("\n完成！原始:"+str(len(all_raw))+" 清理:"+str(len(all_processed))+" 報告:"+str(len(all_reports)))

if __name__ == "__main__":
    main()
