"""東吳大學資料系 LINEBOT"""
import os
import sys
from flask import Flask, abort, request
from bs4 import BeautifulSoup
import markdown
from google import genai
from google.genai import types  # 加入system prompt所需的types模組
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# 1. 從環境變數讀取所有金鑰
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
line_channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
line_channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

# 2. 初始化 Google Gemini (修正 Model 名稱)
client = genai.Client(api_key=GEMINI_API_KEY)
chat = client.chats.create(
    model="gemini-2.5-flash",
    config=types.GenerateContentConfig(
        system_instruction="你是一個中文的AI助手，請用繁體中文回答"
    )
)

# 3. 初始化 LINE Webhook 處理器
handler = WebhookHandler(line_channel_secret)

# 4. 建立 LINE API 的 Configuration 設定物件 (【關鍵修正】)
line_config = Configuration(access_token=line_channel_access_token)

# 初始化 Flask app
app = Flask(__name__)

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
    
    # 轉換 Markdown 格式
    html_msg = markdown.markdown(response_text)
    soup = BeautifulSoup(html_msg, "html.parser")
    
    # 【關鍵修正】把設定好的 line_config 物件傳入 ApiClient 裡面
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(                reply_token=event.reply_token,
                messages=[TextMessage(text=soup.get_text())],
            )
        )

if __name__ == "__main__":
    # 本地測試可用，在 Hugging Face 會由內建的 WSGI 伺服器啟動
    app.run(port=7860)