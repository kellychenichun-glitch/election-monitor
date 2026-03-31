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





