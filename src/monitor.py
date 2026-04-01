"""
選情監控系統 v5.3
修正：
1. is_recent() 解決 RSS 日期過濾問題（Google News 現在可以抓到）
2. 陳素月 relevant_check 加強：排除八卦山無關活動、員林市政
3. 黃柏瑜 relevant_check 加強：搜尋結果需與本人直接相關
4. Serper 移除日期過濾，讓 Claude 在分析時判斷相關性
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

TODAY     = datetime.date.today().isoformat()
YESTERDAY = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
RUN_ID    = str(uuid.uuid4())[:8]

CANDIDATES = {
    "黃柏瑜": {
        "description": (
            "黃柏瑜（1995年2月11日生），民進黨籍，現任彰化縣議員（第20屆，第一選區），"
            "民進黨提名2026年彰化市長候選人。"
            "學歷：美國麻州大學波士頓分校學士、英國劍橋大學碩士。"
            "曾任立法委員洪宗熠國會助理、立委候選人吳怡農競選辦公室助理、"
            "彰化市公所市長室專員。主要政見：彰化市地方建設、青年政策、城市治理。"
        ),
        "location": "彰化市",
        "role": "彰化市長候選人／現任彰化縣議員（第一選區）",
        "keywords": ["黃柏瑜 彰化市"],
        "social_keywords": {
            "FB":      "黃柏瑜 彰化市長",
            "IG":      "黃柏瑜 彰化",
            "Threads": "黃柏瑜 彰化",
            "PTT":     "黃柏瑜 彰化",
        },
        "relevant_check": (
            "黃柏瑜是彰化縣議員兼市長候選人，1995年生，民進黨。\n"
            "relevant=TRUE 條件（必須明確提到本人）：\n"
            "- 新聞或貼文的主角是黃柏瑜本人\n"
            "- 標題或內文明確提到黃柏瑜的姓名且與選舉/議員職務相關\n"
            "relevant=FALSE 條件：\n"
            "- 同名但不同人（其他縣市的黃柏瑜）\n"
            "- 黃柏瑜只是被順帶一提，主角是其他人\n"
            "- 商業廣告、無關政治的內容\n"
            "- 只提到彰化市但完全未提及黃柏瑜"
        ),
        "context": [
            "high: 選舉爭議、負面新聞、民調結果、對手攻擊、重要媒體報導、造勢活動",
            "medium: 地方建設宣傳、政見說明、受邀出席活動、重要人士背書、議員問政",
            "low: 日常社群貼文、例行行程",
        ],
        "sentiment_guide": (
            "正面：民調上升、獲重要人士背書、政績被肯定、活動成功、正面媒體報導\n"
            "負面：民調下滑、爭議事件、被批評或攻擊、負面新聞曝光\n"
            "中立：一般政見說明、例行活動宣傳、中性報導（無明顯褒貶）"
        ),
    },
    "陳素月": {
        "description": (
            "陳素月（Chen Su-Yueh，1966年1月18日生），民進黨籍立法委員，"
            "現任彰化縣第4選區立法委員（曾任第8、9、10屆），"
            "民進黨提名2026年彰化縣長候選人。"
            "學歷：文化大學史學研究所碩士。"
            "主要政策：交通建設與公共運輸（長期任立法院交通委員會委員，"
            "第11屆第2會期任召集委員）、彰化地方建設、"
            "教育資源爭取（偏鄉數位學習、融合教育）、弱勢照顧、青年參與。\n"
            "【重要注意】台灣另有員林市長陳素月（民進黨，彰化縣員林市），"
            "兩人不同人，本系統監控的是立法委員陳素月。"
        ),
        "location": "彰化縣第4選區",
        "role": "立法委員／2026彰化縣長候選人",
        "keywords": ["陳素月 立委 彰化", "陳素月 彰化縣長"],
        "social_keywords": {
            "FB":      "陳素月 立委員 彰化",
            "IG":      "陳素月 立委 彰化縣長候選",
            "Threads": "陳素月 立委 彰化",
            "PTT":     "陳素月 彰化縣長候選",
        },
        "relevant_check": (
            "\u672C\u7CFB\u7D71\u76E3\u63A7\u7684\u662F\u3010\u7ACB\u6CD5\u59D4\u9673\u7D20\u6708\u3011\uFF0C\u5F70\u5316\u7E23\u7B2C4\u9078\u5340\uFF0C\u6C11\u9032\u9EE8\uFF0C2026\u5F70\u5316\u7E23\u9577\u5019\u9078\u4EBA\u3002"
            "\u4E3B\u8981\u8077\u52D9\uFF1A\u7ACB\u6CD5\u9662\u4EA4\u901A\u59D4\u54E1\u6703\u3001\u5F70\u5316\u5730\u65B9\u5EFA\u8A2D\u7B49\u4E89\u53D6\u3001\u6559\u80B2\u8CC7\u6E90\u3002"
            "\u3010\u91CD\u8981\u3011\u53F0\u7063\u6709\u5169\u4F4D\u9673\u7D20\u6708\uFF1A"
            "(1)\u7ACB\u6CD5\u59D4\u54E1\u9673\u7D20\u6708(\u5F70\u5316\u7E23\u7B2C4\u9078\u5340) "
            "(2)\u54E1\u6797\u5E02\u9577\u9673\u7D20\u6708(\u5F70\u5316\u7E23\u54E1\u6797\u5E02)\u3002"
            "relevant=TRUE\uFF1A"
            "\u660E\u78BA\u7528\u300C\u7ACB\u59D4\u300D\u300C\u5F70\u5316\u7E23\u9577\u5019\u9078\u300D\u3001\u7ACB\u6CD5\u9662\u8CEA\u8A62\u3001\u4EA4\u901A\u59D4\u54E1\u6703\u3001\u5F70\u5316\u7E234\u9078\u5340\u3001\u5F70\u5316\u7E23\u9577\u9078\u8209\u3002"
            "relevant=FALSE(\u4EE5\u4E0B\u4EFB\u4E00\u5373\u70BAfalse)\uFF1A"
            "\u767D\u7C73/\u5305\u6750/\u8FB2\u7522\u5305\u6750/\u8FB2\u6703/\u7A3B\u7C73/\u81EA\u5099\u888B\u3001"
            "\u54E1\u6797\u5E02/\u54E1\u6797\u5E02\u9577/\u54E1\u6797\u8FB2\u6703/\u5927\u5CE6\u5DEB\u6B65\u9053/\u5317\u6E9D\u6392\u6C34\u3001"
            "\u516B\u5366\u5C71/\u6B65\u9053/\u74B0\u4FDD\u8003\u5BDF/\u6A02\u9F4A\u8001\u4EBA/\u9577\u7167\u3001"
            "IG/Threads\u8CA3\u6587\u4E14\u5167\u5BB9\u5C6C\u8FB2\u696D/\u74B0\u4FDD/\u8FB2\u6703/\u5730\u65B9\u5E02\u653F\u3001"
            "\u4E3B\u89D2\u662F\u5176\u4ED6\u4EBA\u4E14\u9673\u7D20\u6708\u672A\u76F4\u63A5\u51FA\u73FE\u3001"
            "\u7121\u6CD5\u5224\u65B7\u662F\u54EA\u4F4D\u9673\u7D20\u6708\u6642\u9810\u8A2Dfalse\u3002"
            "\u5224\u65B7\u539F\u5247\uFF1A\u5BCD\u53EF\u591A\u904E\u6FFE\uFF0C\u4E5F\u4E0D\u8981\u8B93\u54E1\u6797\u5E02\u9577\u696D\u52D9\u6DF7\u5165\u3002"
        ),
        "context": [
            "high: 縣長選舉爭議、民調結果、對手攻擊、負面新聞、重大政策宣布、立院重要質詢",
            "medium: 一般立委問政、交通建設考察、教育爭取、地方服務、造勢活動、支持者背書",
            "low: 例行行程、日常社群貼文",
        ],
        "sentiment_guide": (
            "正面：民調上升、重要建設成功、獲關鍵人士背書、正面媒體報導、問政被稱讚\n"
            "負面：民調下滑、爭議事件、被攻擊或批評、負面新聞、問政失誤\n"
            "中立：例行立委問政、一般考察、中性報導（無明顯褒貶）"
        ),
    },
}

SOCIAL_PLATFORMS = {
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

def is_recent(date_str):
    """接受今日、昨日或前天的資料，解析失敗預設保留"""
    if not date_str:
        return True
    # 計算允許的日期範圍
    allowed = set()
    for i in range(3):  # 今天、昨天、前天
        allowed.add((datetime.date.today() - datetime.timedelta(days=i)).isoformat())
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(date_str).date().isoformat()
        return parsed in allowed
    except Exception:
        pass
    try:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', str(date_str))
        if m:
            return m.group(1) in allowed
    except Exception:
        pass
    # 嘗試解析月日格式如 "Apr 1" 或 "01 Apr 2026"
    try:
        import email.utils
        ts = email.utils.parsedate(str(date_str))
        if ts:
            import time
            d = datetime.date(*ts[:3])
            return d.isoformat() in allowed
    except Exception:
        pass
    return True  # 無法解析一律保留，讓 Claude 判斷相關性

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
                "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
                "keyword": keyword, "source": platform,
                "title_raw": item.get("title", ""), "snippet_raw": item.get("snippet", ""),
                "url_raw": item.get("link", ""), "fetch_status": "ok",
            })
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": platform,
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "fetch_status": "error:" + str(e)[:100],
        })
    return raw_results

def fetch_google_news_rss(keyword):
    """抓取 Google News RSS，保留今日和昨日的資料"""
    raw_results = []
    url = "https://news.google.com/rss/search?q=" + quote(keyword) + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; ElectionMonitor/1.0)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        kept = 0
        for item in items[:20]:
            pub_date = item.findtext("pubDate", "")
            if not is_recent(pub_date):
                continue
            raw_results.append({
                "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
                "keyword": keyword, "source": "Google News",
                "title_raw": item.findtext("title", ""),
                "snippet_raw": item.findtext("description", ""),
                "url_raw": item.findtext("link", ""),
                "fetch_status": "ok",
            })
            kept += 1
        print("    RSS [" + keyword + "]: 掃描" + str(len(items)) + "筆，保留" + str(kept) + "筆（今+昨+前天）")
        if kept == 0 and len(items) > 0:
            # debug: 印出前3筆的日期
            for dbg_item in root.findall(".//item")[:3]:
                dbg_date = dbg_item.findtext("pubDate", "NO_DATE")
                print("      [RSS debug] pubDate=" + dbg_date[:50])
    except Exception as e:
        raw_results.append({
            "run_id": RUN_ID, "fetched_at": datetime.datetime.now().isoformat(),
            "keyword": keyword, "source": "Google News",
            "title_raw": "", "snippet_raw": "", "url_raw": "",
            "fetch_status": "error:" + str(e)[:100],
        })
        print("    RSS 錯誤 [" + keyword + "]: " + str(e)[:80])
    return raw_results

def process_raw(raw, raw_id):
    if raw.get("fetch_status", "").startswith("error"): return None
    title_clean = strip_html(raw.get("title_raw", ""))
    snippet_clean = strip_html(raw.get("snippet_raw", ""))
    text_clean = (title_clean + " " + snippet_clean).strip()
    if not title_clean: return None
    return {
        "raw_id": raw_id, "source": raw.get("source", ""),
        "keyword": raw.get("keyword", ""), "candidate": raw.get("candidate", ""),
        "fetched_at": raw.get("fetched_at", ""),
        "normalized_url": normalize_url(raw.get("url_raw", "")),
        "url_hash": url_hash(raw.get("url_raw", "")),
        "title_clean": title_clean, "text_clean": text_clean,
        "text_len": len(text_clean), "parser_status": "ok",
    }

def analyze_to_report(processed_items, candidate, candidate_info):
    if not processed_items: return []
    content_list = "\n".join(
        str(i+1) + ". 標題：" + it["title_clean"] +
        "\n   來源：" + it["source"] +
        "\n   內容：" + it["text_clean"][:250]
        for i, it in enumerate(processed_items)
    )
    prompt = (
        "你是台灣選舉專業分析師，判斷標準要嚴格。\n\n"
        "【監控對象】\n姓名：" + candidate + "\n"
        "身份：" + candidate_info.get("role", "") + "\n"
        "背景：" + candidate_info.get("description", "") + "\n\n"
        "【相關性判斷規則（嚴格執行）】\n"
        + candidate_info.get("relevant_check", "") + "\n\n"
        "【情緒判斷標準】\n" + candidate_info.get("sentiment_guide", "") + "\n\n"
        "【重要度標準】\n" + "\n".join(candidate_info.get("context", [])) + "\n\n"
        "【待分析資料】\n" + content_list + "\n\n"
        "請針對每筆資料回答（判斷要嚴格，寧可多過濾也不要放入無關資料）：\n"
        "1. relevant（true/false）：是否與【" + candidate + "】本人直接相關？\n"
        "2. topic：主要議題（10字內，若relevant=false填'無關'）\n"
        "3. stance：立場（pro/against/neutral）\n"
        "4. sentiment：對選情影響（正面/中立/負面）\n"
        "5. priority：重要度（high/medium/low）\n"
        "6. summary：一句話說明對選情影響（20字內）\n\n"
        "以JSON陣列回覆，每元素含：index,relevant,topic,stance,sentiment,priority,summary\n只回JSON。"
    )
    reports = []
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
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
                print("    [過濾] " + item["title_clean"][:60])
                continue
            reports.append({
                "keyword": item["keyword"], "candidate": candidate,
                "topic": a.get("topic", ""), "stance": a.get("stance", "neutral"),
                "sentiment": a.get("sentiment", "中立"), "summary": a.get("summary", ""),
                "priority": a.get("priority", "low"),
                "source": item["source"], "url": item["normalized_url"],
                "title": item["title_clean"], "published_at": item["fetched_at"][:10],
            })
        if skipped: print("  過濾無關: " + str(skipped) + "筆")
    except Exception as e:
        print("  [Claude錯誤] " + str(e))
        for item in processed_items:
            reports.append({
                "keyword": item["keyword"], "candidate": candidate,
                "topic": "", "stance": "neutral", "sentiment": "中立",
                "summary": "", "priority": "low",
                "source": item["source"], "url": item["normalized_url"],
                "title": item["title_clean"], "published_at": item["fetched_at"][:10],
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
                except Exception: pass
            sorted_reports = sorted(reports, key=lambda x: (
                {"high":0,"medium":1,"low":2}.get(x.get("priority","low"),2),
                {"負面":0,"正面":1,"中立":2}.get(x.get("sentiment","中立"),2)
            ))
            now_time = datetime.datetime.now().strftime("%H:%M:%S")
            rows = [[now_time,r["candidate"],r["topic"],r["stance"],r["sentiment"],
                     r["priority"],r["summary"],r["source"],r["title"],r["url"],r["published_at"]]
                    for r in sorted_reports]
            if rows: ws.insert_rows(rows, row=2)
            if error_logs:
                try: ws_err = wb.worksheet("ErrorLog")
                except Exception:
                    ws_err = wb.add_worksheet("ErrorLog", 500, 8)
                    ws_err.append_row(["run_id","stage","source","keyword","error_code","error_message","raw_payload","時間"])
                ws_err.append_rows([[e["run_id"],e["stage"],e["source"],e["keyword"],
                                     e["error_code"],e["error_message"],e["raw_payload"],TODAY]
                                    for e in error_logs])
            print("[Sheets] 成功！[" + sheet_name + "] 寫入 " + str(len(rows)) + "筆")
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
        sorted_r = sorted(reports, key=lambda x: (
            {"high":0,"medium":1,"low":2}.get(x.get("priority","low"),2),
            {"負面":0,"正面":1,"中立":2}.get(x.get("sentiment","中立"),2)
        ))
        rows_html = ""
        for r in sorted_r[:30]:
            sc = "#16a34a" if r["sentiment"]=="正面" else "#dc2626" if r["sentiment"]=="負面" else "#64748b"
            em = "🟢" if r["sentiment"]=="正面" else "🔴" if r["sentiment"]=="負面" else "⚪"
            flag = "🔥" if r.get("priority")=="high" else "📌" if r.get("priority")=="medium" else ""
            rows_html += (
                "<tr style='border-bottom:1px solid #e2e8f0'>"
                "<td style='padding:10px 12px;font-size:12px;color:#6b7280'>"+flag+r["source"]+"</td>"
                "<td style='padding:10px 12px;font-weight:700;font-size:13px'>"+r["candidate"]+"</td>"
                "<td style='padding:10px 12px;font-size:12px'>"+r["topic"]+"</td>"
                "<td style='padding:10px 12px;font-size:13px;max-width:250px'>"+r["title"][:55]+"...</td>"
                "<td style='padding:10px 12px'><span style='color:"+sc+";font-weight:700;font-size:12px'>"+em+" "+r["sentiment"]+"</span></td>"
                "<td style='padding:10px 12px;font-size:12px;color:#374151'>"+r.get("summary","")+"</td>"
                "<td style='padding:10px 12px'><a href='"+r["url"]+"' style='color:#2563eb;font-size:12px'>查看</a></td>"
                "</tr>"
            )
        sc_color = "#16a34a" if score>0 else "#dc2626" if score<0 else "#64748b"
        sc_str = ("+" if score>0 else "")+str(score)
        html = (
            "<!DOCTYPE html><html><body style='font-family:Microsoft JhengHei,sans-serif;background:#f8f9fa;padding:20px'>"
            "<div style='max-width:920px;margin:0 auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>"
            "<div style='background:linear-gradient(135deg,#15803d,#166534);padding:28px;color:white'>"
            "<h1 style='margin:0;font-size:22px;font-weight:900'>📡 選情雷達日報 v5.3</h1>"
            "<p style='margin:6px 0 0;opacity:0.75;font-size:13px'>"+run_time+"｜黃柏瑜（彰化市長候選人）× 陳素月（彰化縣長候選人）</p>"
            "<p style='margin:4px 0 0;opacity:0.6;font-size:12px'>"+ps+"</p></div>"
            "<div style='padding:16px 20px;display:grid;grid-template-columns:repeat(5,1fr);gap:10px;background:#f1f5f9'>"
            "<div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#2563eb'>"+str(total)+"</div><div style='font-size:11px;color:#6b7280'>今日聲量</div></div>"
            "<div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#16a34a'>"+str(pos)+"</div><div style='font-size:11px;color:#6b7280'>🟢 正面</div></div>"
            "<div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#dc2626'>"+str(neg)+"</div><div style='font-size:11px;color:#6b7280'>🔴 負面</div></div>"
            "<div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:#d97706'>"+str(high_count)+"</div><div style='font-size:11px;color:#6b7280'>🔥 重要</div></div>"
            "<div style='background:white;border-radius:8px;padding:14px;text-align:center;border:1px solid #e2e8f0'><div style='font-size:28px;font-weight:900;color:"+sc_color+"'>"+sc_str+"</div><div style='font-size:11px;color:#6b7280'>情緒指數</div></div></div>"
            "<div style='padding:16px 20px;overflow-x:auto'><table style='width:100%;border-collapse:collapse'>"
            "<thead><tr style='background:#f8f9fa'>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>平台</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>候選人</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>議題</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>標題</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>情緒</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>影響摘要</th>"
            "<th style='padding:10px 12px;text-align:left;font-size:11px;color:#9ca3af;border-bottom:2px solid #e2e8f0'>連結</th>"
            "</tr></thead><tbody>"+rows_html+"</tbody></table></div>"
            "<div style='padding:14px 20px;background:#f8f9fa;font-size:11px;color:#9ca3af;text-align:center;border-top:1px solid #e2e8f0'>"
            "選情雷達 v5.3 · 加強相關性過濾 + Google News RSS 修正</div>"
            "</div></body></html>"
        )
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
    print("  選情監控 v5.3 "+run_time+" Run:"+RUN_ID)
    print("  修正：RSS日期過濾 + 加強相關性判斷（排除八卦山/員林）")
    print("="*60)
    all_raw,all_processed,all_reports,all_errors=[],[],[],[]
    seen_url_hashes=set()
    print("\n[Layer 1] 抓取...")
    for candidate, info in CANDIDATES.items():
        print("\n  ▶ "+candidate+"（"+info["role"]+"）")
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
        for platform,prefix in SOCIAL_PLATFORMS.items():
            social_kw=info["social_keywords"].get(platform,info["keywords"][0])
            raw_list=fetch_serper(social_kw,prefix,platform)
            for rr in raw_list: rr["candidate"]=candidate
            all_raw.extend(raw_list)
            ok=sum(1 for r in raw_list if r["fetch_status"]=="ok")
            if ok: print("    "+platform+" ["+social_kw+"]: "+str(ok)+"筆")
            time.sleep(0.3)
    print("\n  原始: "+str(len(all_raw))+"筆")
    print("\n[Layer 2] 清理（URL去重）...")
    for i,raw in enumerate(all_raw):
        if raw["fetch_status"].startswith("error"):
            all_errors.append({"run_id":RUN_ID,"stage":"fetch","source":raw["source"],
                "keyword":raw["keyword"],"error_code":raw["fetch_status"],
                "error_message":raw["fetch_status"],"raw_payload":""})
            continue
        p=process_raw(raw,i)
        if p and p["url_hash"] not in seen_url_hashes:
            seen_url_hashes.add(p["url_hash"])
            all_processed.append(p)
    print("  清理後: "+str(len(all_processed))+"筆")
    print("\n[Layer 3] Claude分析（嚴格相關性過濾）...")
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
