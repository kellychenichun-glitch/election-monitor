"""
選情監控系統 v4
優化：
1. FB/IG/Threads 改用候選人粉專名稱搜尋，提升精準度
2. Claude 情緒 prompt 加入具體判斷標準和例子
3. 重要度明確定義（high/medium/low）
4. 日期過濾更嚴格（Serper + RSS 都只取今日）
5. 無關資料過濾更強
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
        "description": "黃柏瑜是台灣彰化市長候選人、現任彰化市議員，代表民進黨參選2026年地方選舉",
        "location": "彰化市",
        "keywords": ["黃柏瑜 彰化市"],
        "social_keywords": {"FB": "黃柏瑜 彰化市長", "IG": "黃柏瑜 彰化", "Threads": "黃柏瑜 彰化市", "PTT": "黃柏瑜 彰化"},
        "context": ["high: 媒體主動報導選舉爭議、負面新聞、重大政策宣布、民調結果", "high: 對手攻擊或回應、司法案件、重要造勢活動", "medium: 一般政見說明、地方建設、受邀出席活動", "medium: 支持者貼文、一般新聞報導", "low: 日常社群貼文、無直接選舉相關內容"],
        "sentiment_guide": "正面：民調上升、獲得重要人士背書、政績被肯定、活動成功、正面報導\n負面：民調下滑、爭議事件、被批評、負面新聞、對手攻擊\n中立：一般政見說明、中性報導、活動宣傳（無明顯褒貶）",
    },
    "陳素月": {
        "description": "陳素月是台灣員林市長候選人、現任員林市長，代表民進黨爭取連任2026年地方選舉",
        "location": "員林市",
        "keywords": ["陳素月 員林市"],
        "social_keywords": {"FB": "陳素月 員林市長", "IG": "陳素月 員林", "Threads": "陳素月 員林市", "PTT": "陳素月 員林"},
        "context": ["high: 媒體主動報導選舉爭議、負面新聞、重大政策宣布、民調結果", "high: 對手攻擊或回應、市政爭議、重要建設竣工", "medium: 一般市政業務、地方建設說明、受邀出席活動", "medium: 支持者貼文、一般新聞報導", "low: 日常社群貼文、無直接選舉相關內容"],
        "sentiment_guide": "正面：市政獲肯定、重要建設完工、獲得支持、正面報導、爭取到中央資源\n負面：市政被批評、爭議事件、對手攻擊、負面新聞\n中立：一般市政說明、中性報導、活動宣傳（無明顯褒貶）",
    },
}

SOCIAL_PLATFORMS = {"FB": "site:facebook.com", "IG": "site:instagram.com", "Threads": "site:threads.net", "PTT": "site:ptt.cc"}

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)
    def get_text(self):
        return " ".join(self.text).strip()

def strip_html(text):
    if not text: return ""
    try:
        s = HTMLStripper(); s.feed(text); clean = s.get_text()
    except Exception: clean = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', clean).strip()[:500]

def normalize_url(url):
    url = url.strip()
    url = re.sub(r'[?&](utm_[^&]+|oc=\d+)', '', url)
    return re.sub(r'[?&]$', '', url)

def url_hash(url):
    return hashlib.md5(normalize_url(url).encode()).hexdigest()[:12]

def is_today(date_str):
    if not date_str: return True
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).date().isoformat() == TODAY
    except Exception:
        return TODAY in str(date_str)

def fetch_serper(keyword, site_prefix, platform):
    query = (site_prefix + " " + keyword).strip() if site_prefix else keyword
    raw_results = []
    try:
        r = requests.post("https://google.serper.dev/search", headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}, json={"q": query, "num": 5, "gl": "tw", "hl": "zh-tw", "tbs": "qdr:d"}, timeout=10)
        r.raise_for_status()
        for item in r.json().get("organic", []):
            item_date = item.get("date", "")
            if item_date and TODAY not in item_date and "小時前" not in item_date and "分鐘前" not in item_date: continue
            raw_results.append({"run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(), "keyword": keyword, "source": platform, "title_raw": item.get("title", ""), "snippet_raw": item.get("snippet", ""), "url_raw": item.get("link", ""), "fetch_status": "ok"})
    except Exception as e:
        raw_results.append({"run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(), "keyword": keyword, "source": platform, "title_raw": "", "snippet_raw": "", "url_raw": "", "fetch_status": "error:" + str(e)[:100]})
    return raw_results

def fetch_google_news_rss(keyword):
    raw_results = []
    url = "https://news.google.com/rss/search?q=" + quote(keyword) + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:15]:
            if not is_today(item.findtext("pubDate", "")): continue
            raw_results.append({"run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(), "keyword": keyword, "source": "Google News", "title_raw": item.findtext("title", ""), "snippet_raw": item.findtext("description", ""), "url_raw": item.findtext("link", ""), "fetch_status": "ok"})
    except Exception as e:
        raw_results.append({"run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(), "keyword": keyword, "source": "Google News", "title_raw": "", "snippet_raw": "", "url_raw": "", "fetch_status": "error:" + str(e)[:100]})
    return raw_results

def process_raw(raw, raw_id):
    if raw.get("fetch_status", "").startswith("error"): return None
    title_clean = strip_html(raw.get("title_raw", ""))
    snippet_clean = strip_html(raw.get("snippet_raw", ""))
    text_clean = (title_clean + " " + snippet_clean).strip()
    if not title_clean: return None
    return {"raw_id": raw_id, "source": raw.get("source", ""), "keyword": raw.get("keyword", ""), "candidate": raw.get("candidate", ""), "fetched_at": raw.get("fetched_at", ""), "normalized_url": normalize_url(raw.get("url_raw", "")), "url_hash": url_hash(raw.get("url_raw", "")), "title_clean": title_clean, "text_clean": text_clean, "text_len": len(text_clean), "parser_status": "ok"}

def analyze_to_report(processed_items, candidate, candidate_info):
    if not processed_items: return []
    description = candidate_info.get("description", "")
    sentiment_guide = candidate_info.get("sentiment_guide", "")
    context_rules = "\n".join(candidate_info.get("context", []))
    content_list = "\n".join(str(i+1) + ". 標題：" + it["title_clean"] + "\n   來源：" + it["source"] + "\n   摘要：" + it["text_clean"][:200] for i, it in enumerate(processed_items))
    prompt = ("你是台灣地方選舉專業分析師。\n\n【候選人資料】\n姓名：" + candidate + "\n背景：" + description + "\n\n【情緒判斷標準】（對" + candidate + "的影響）\n" + sentiment_guide + "\n\n【重要度判斷標準】\n" + context_rules + "\n\n【待分析資料】\n" + content_list + "\n\n請針對每筆資料回答：\n1. relevant（布林值）：是否與「" + candidate + "」本人直接相關？false 的情況：同名不同人、商品廣告、無關政治\n2. topic：主要議題（10字內）\n3. stance：立場（pro/against/neutral）\n4. sentiment：對" + candidate + "的影響（正面/中立/負面）\n5. priority：重要度（high/medium/low）\n6. summary：一句話說明對選情影響（20字內）\n\nJSON陣列回覆，每元素含：index,relevant,topic,stance,sentiment,priority,summary\n只回JSON。")
    reports = []
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        sent_map = {a["index"]: a for a in json.loads(text)}
        skipped = 0
        for i, item in enumerate(processed_items):
            a = sent_map.get(i + 1, {})
            if not a.get("relevant", True): skipped += 1; continue
            reports.append({"keyword": item["keyword"], "candidate": candidate, "topic": a.get("topic",""), "stance": a.get("stance","neutral"), "sentiment": a.get("sentiment","中立"), "summary": a.get("summary",""), "priority": a.get("priority","low"), "source": item["source"], "url": item["normalized_url"], "title": item["title_clean"], "published_at": item["fetched_at"][:10]})
        if skipped: print("  過濾無關: " + str(skipped) + "筆")
    except Exception as e:
        print("  [Claude錯誤] " + str(e))
        for item in processed_items:
            reports.append({"keyword": item["keyword"], "candidate": candidate, "topic": "", "stance": "neutral", "sentiment": "中立", "summary": "", "priority": "low", "source": item["source"], "url": item["normalized_url"], "title": item["title_clean"], "published_at": item["fetched_at"][:10]})
    return reports

def write_to_sheets(reports, error_logs):
    for attempt in range(3):
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            sa_info = json.loads(os.environ["GOOGLE_SA_JSON"].strip())
            creds = Credentials.from_service_account_info(sa_info, scopes=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"])
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
                except Exception: pass
            sorted_reports = sorted(reports, key=lambda x: ({"high":0,"medium":1,"low":2}.get(x.get("priority","low"),2), {"負面":0,"正面":1,"中立":2}.get(x.get("sentiment","中立"),2)))
            now_time = datetime.datetime.now().strftime("%H:%M:%S")
            report_rows = [[now_time, r["candidate"], r["topic"], r["stance"], r["sentiment"], r["priority"], r["summary"], r["source"], r["title"], r["url"], r["published_at"]] for r in sorted_reports]
            if report_rows: ws.insert_rows(report_rows, row=2)
            if error_logs:
                try: ws_err = wb.worksheet("ErrorLog")
                except Exception:
                    ws_err = wb.add_worksheet("ErrorLog", 500, 8)
                    ws_err.append_row(["run_id","stage","source","keyword","error_code","error_message","raw_payload","時間"])
                ws_err.append_rows([[e["run_id"],e["stage"],e["source"],e["keyword"],e["error_code"],e["error_message"],e["raw_payload"],TODAY] for e in error_logs])
            print("[Sheets] 成功！[" + sheet_name + "] 寫入 " + str(len(report_rows)) + "筆")
            return True
        except Exception as e:
            print("[Sheets錯誤] 第" + str(attempt+1) + "次: " + str(e))
            if attempt < 2: time.sleep(5)
    return False

def send_email_report(reports, run_time):
    try:
        pos = sum(1 for r in reports if r["sentiment"]=="正面")
        neg = sum(1 for r in reports if r["sentiment"]=="負面")
        total = len(reports)
        score = round(((pos-neg)/total*100)) if total else 0
        high_count = sum(1 for r in reports if r["priority"]=="high")
        pc = {}
        for r in reports: pc[r["source"]] = pc.get(r["source"],0)+1
        ps = " | ".join(k+" "+str(v)+"筆" for k,v in sorted(pc.items()))
        sorted_r = sorted(reports, key=lambda x: ({"high":0,"medium":1,"low":2}.get(x.get("priority","low"),2), {"負面":0,"正面":1,"中立":2}.get(x.get("sentiment","中立"),2)))
        rows_html = ""
        for r in sorted_r[:25]:
            sc = "#16a34a" if r["sentiment"]=="正面" else "#dc2626" if r["sentiment"]=="負面" else "#64748b"
            em = "🟢" if r["sentiment"]=="正面" else "🔴" if r["sentiment"]=="負面" else "⚪"
            flag = "🔥" if r.get("priority")=="high" else "📌" if r.get("priority")=="medium" else ""
            rows_html += "<tr style='border-bottom:1px solid #e2e8f0'><td style='padding:10px 12px;font-size:12px;color:#6b7280'>"+flag+r["source"]+"</td><td style='padding:10px 12px;font-weight:700;font-size:13px'>"+r["candidate"]+"</td><td style='padding:10px 12px;font-size:12px'>"+r["topic"]+"</td><td style='padding:10px 12px;font-size:13px;max-width:250px'>"+r["title"][:55]+"...</td><td style='padding:10px 12px'><span style='color:"+sc+";font-weight:700;font-size:12px'>"+em+" "+r["sentiment"]+"</span></td><td style='padding:10px 12px;font-size:12px;color:#374151'>"+r.get("summary","")+"</td><td style='padding:10px 12px'><a href='"+r["url"]+"' style='color:#2563eb;font-size:12px'>查看</a></td></tr>"
        sc_color = "#16a34a" if score>0 else "#dc2626" if score<0 else "#64748b"
        sc_str = ("+") if score>0 else ""
        sc_str = sc_str+str(score)
        html = "<!DOCTYPE html><html><body style='font-family:Microsoft JhengHei,sans-serif;background:#f8f9fa;padding:20px'><div style='max-width:920px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)'><div style='background:linear-gradient(135deg,#15803d,#166534);padding:28px;color:white'><h1 style='margin:0;font-size:22px;font-weight:900'>📡 選情雷達日報</h1><p style='margin:6px 0 0;opacity:0.75;font-size:13px'>"+run_time+"｜彰化市黃柏瑜 × 員林市陳素月</p><p style='margin:4px 0 0;opacity:0.6;font-size:12px'>"+ps+"</p></div><div style='padding:18px 20px;display:grid;grid-template-columns:repeat(5,1fr);gap:10px;background:#f1f5f9'><div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#2563eb'>"+str(total)+"</div><div style='font-size:11px;color:#6b7280'>今日聲量</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#16a34a'>"+str(pos)+"</div><div style='font-size:11px;color:#6b7280'>🟢 正面</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#dc2626'>"+str(neg)+"</div><div style='font-size:11px;color:#6b7280'>🔴 負面</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#d97706'>"+str(high_count)+"</div><div style='font-size:11px;color:#6b7280'>🔥 重要</div></div><div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:"+sc_color+"'>"+sc_str+"</div><div style='font-size:11px;color:#6b7280'>情緒指數</div></div></div><div style='padding:16px 20px;overflow-x:auto'><table style='width:100%;border-collapse:collapse'><thead><tr style='background:#f8f9fa'><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>平台</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>候選人</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>議題</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>標題</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>情緒</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>影響摘要</th><th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;font-weight:700;border-bottom:2px solid #e2e8f0'>連結</th></tr></thead><tbody>"+rows_html+"</tbody></table></div><div style='padding:14px 20px;background:#f8f9fa;font-size:11px;color:#9ca3af;text-align:center;border-top:1px solid #e2e8f0'>選情雷達 v4 · Serper.dev + Google News · 已過濾無關 · 嚴格日期</div></div></body></html>"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "📡 選情日報 "+run_time+"｜"+str(total)+"筆｜🔥"+str(high_count)+"則重要"
        msg["From"] = GMAIL_USER
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(html,"html","utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(GMAIL_USER,GMAIL_PASS)
            s.sendmail(GMAIL_USER,NOTIFY_EMAIL,msg.as_string())
        print("[Email] 成功 → "+NOTIFY_EMAIL)
    except Exception as e:
        print("[Email錯誤] "+str(e))

def main():
    run_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("="*60)
    print("  選情監控 v4 "+run_time+" Run:"+RUN_ID)
    print("="*60)
    all_raw,all_processed,all_reports,all_errors=[],[],[],[]
    seen_url_hashes=set()
    print("\n[Layer 1] 抓取...")
    for candidate, info in CANDIDATES.items():
        print("\n  ▶ "+candidate+"（"+info["location"]+"）")
        for base_kw in info["keywords"]:
            raw_list = fetch_serper(base_kw,"","Google")
            for rr in raw_list: rr["candidate"]=candidate
            all_raw.extend(raw_list)
            ok=sum(1 for r in raw_list if r["fetch_status"]=="ok")
            if ok: print("    Google ["+base_kw+"]: "+str(ok)+"筆")
            time.sleep(0.3)
            rss_list = fetch_google_news_rss(base_kw)
            for rr in rss_list: rr["candidate"]=candidate
            all_raw.extend(rss_list)
            ok=sum(1 for r in rss_list if r["fetch_status"]=="ok")
            if ok: print("    Google News ["+base_kw+"]: "+str(ok)+"筆（今日）")
        for platform,prefix in SOCIAL_PLATFORMS.items():
            social_kw=info["social_keywords"].get(platform,info["keywords"][0])
            raw_list=fetch_serper(social_kw,prefix,platform)
            for rr in raw_list: rr["candidate"]=candidate
            all_raw.extend(raw_list)
            ok=sum(1 for r in raw_list if r["fetch_status"]=="ok")
            if ok: print("    "+platform+" ["+social_kw+"]: "+str(ok)+"筆")
            time.sleep(0.3)
    print("\n  原始: "+str(len(all_raw))+"筆")
    print("\n[Layer 2] 清理...")
    for i,raw in enumerate(all_raw):
        if raw["fetch_status"].startswith("error"):
            all_errors.append({"run_id":RUN_ID,"stage":"fetch","source":raw["source"],"keyword":raw["keyword"],"error_code":raw["fetch_status"],"error_message":raw["fetch_status"],"raw_payload":""})
            continue
        p=process_raw(raw,i)
        if p and p["url_hash"] not in seen_url_hashes:
            seen_url_hashes.add(p["url_hash"])
            all_processed.append(p)
    print("  清理後: "+str(len(all_processed))+"筆")
    print("\n[Layer 3] Claude分析（v4優化版）...")
    for candidate,info in CANDIDATES.items():
        group=[p for p in all_processed if p.get("candidate")==candidate]
        if group:
            rpts=analyze_to_report(group,candidate,info)
            all_reports.extend(rpts)
            pos=sum(1 for r in rpts if r["sentiment"]=="正面")
            neg=sum(1 for r in rpts if r["sentiment"]=="負面")
            high=sum(1 for r in rpts if r["priority"]=="high")
            print("  "+candidate+": "+str(len(rpts))+"筆 | 正面:"+str(pos)+" 負面:"+str(neg)+" 重要:"+str(high))
    print("\n[Layer 4] 寫入Sheets...")
    write_to_sheets(all_reports,all_errors)
    send_email_report(all_reports,run_time)
    print("\n完成！原始:"+str(len(all_raw))+" 清理:"+str(len(all_processed))+" 報告:"+str(len(all_reports)))

if __name__ == "__main__":
    main()
