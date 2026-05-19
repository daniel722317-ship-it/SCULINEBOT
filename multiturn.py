"""
東吳大學資料系 2025 LINEBOT
"""

import os

from flask import Flask, abort, request
from bs4 import BeautifulSoup
import markdown

from google import genai
from google.genai import types # 加入system prompot所需的types模組

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


# Initialize Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)
chat = client.chats.create(model="gemini-3-flash-preview",
    config=types.GenerateContentConfig(
        system_instruction="你是一個中文的AI助手，請用繁體中文回答"    
    )
)

# Initialize Flask app
app = Flask(__name__)
import os
import sys
from linebot.v3.webhook import WebhookHandler

# 1. 嘗試讀取環境變數
line_channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
line_channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
gemini_api_key = os.environ.get("GEMINI_API_KEY")

# 2. 強制在日誌印出檢查結果（終極排查核心）
print("====================================", flush=True)
print("=== 🛠️ HUGGING FACE 環境變數檢查 ====", flush=True)
print(f"1. LINE Secret 有讀到嗎？ -> {line_channel_secret is not None}", flush=True)
print(f"2. LINE Token 有讀到嗎？  -> {line_channel_access_token is not None}", flush=True)
print(f"3. Gemini Key 有讀到嗎？   -> {gemini_api_key is not None}", flush=True)
print("====================================", flush=True)

# 3. 安全防護：如果是空的就優雅攔截，不要讓後面崩潰
if line_channel_secret is None:
    print("❌【警報】LINE_CHANNEL_SECRET 讀取失敗，值為 None！", flush=True)
    print("請檢查 Settings 裡的 Secret 名字是否完全一致（注意有沒有多餘的空格）。", flush=True)
    sys.exit("排查中：因為變數為空，主動停止程式。")

# 原本的第 40 行
handler = WebhookHandler(line_channel_secret)


def query(payload: str) -> str:
    """Send a prompt to Gemini and return the response text."""
    response = chat.send_message(message=payload)
    return response.text


@app.route("/", methods=["GET"])
def home():
    """Health check endpoint."""
    return {"message": "Line Webhook Server"}


@app.route("/", methods=["POST"])
def callback():
    """Handle incoming webhook from LINE."""
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.warning(
            "Invalid signature. Please check channel credentials."
        )
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """Handle incoming text message event."""
    user_input = event.message.text.strip()
    response_text = query(user_input)
    html_msg = markdown.markdown(response_text)
    soup = BeautifulSoup(html_msg, "html.parser")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=soup.get_text())],
            )
        )