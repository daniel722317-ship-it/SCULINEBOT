# ===東吳大學資料系 2025 年 LINEBOT ===
import base64
import logging
import os
import tempfile

import markdown
from bs4 import BeautifulSoup
from flask import Flask, abort, request, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, TextMessageContent
from openai import OpenAI

# === 初始化OpenAI模型 ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
text_system_prompt = "你是一個中文的AI助手，請用繁體中文回答"

# === 先建立第一個對話，之後可以延續這個對話 ===
response = client.responses.create(
    model="gpt-4o-mini",
    input=[{"role": "system", "content": text_system_prompt}],
)

message_id = response.id

# === 初始設定 ===
static_tmp_path = tempfile.gettempdir()
os.makedirs(static_tmp_path, exist_ok=True)
base_url = os.getenv("SPACE_HOST")  # e.g., "your-space-name.hf.space"

# === Flask 應用初始化 ===
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
app.logger.setLevel(logging.INFO)

line_channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
line_channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

configuration = Configuration(access_token=line_channel_access_token)
handler = WebhookHandler(line_channel_secret)


# === AI Query 包裝 ===
def query(payload, previous_response_id):
    second_response = client.responses.create(
        model="gpt-4o-mini",
        previous_response_id=previous_response_id,
        input=[{"role": "user", "content": f"{payload}"}],
    )
    return second_response


# === 靜態圖檔路由 ===
@app.route("/images/<filename>")
def serve_image(filename):
    return send_from_directory(static_tmp_path, filename)


# === LINE Webhook 接收端點 ===
@app.route("/")
def home():
    return {"message": "Line Webhook Server"}


@app.route("/", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.warning("Invalid signature. Please check channel credentials.")
        abort(400)

    return "OK"


# === 處理文字訊息 ===
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    global message_id
    user_input = event.message.text.strip()
    if user_input.startswith("AI "):
        prompt = user_input[3:].strip()
        try:
            response = client.images.generate(
                model="dall-e-3",
                prompt=f"使用下面的文字來畫一幅畫：{prompt}",
                size="1024x1024",
                quality="standard",
                n=1,
            )
            image_url = response.data[0].url
            app.logger.info(image_url)
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            ImageMessage(
                                original_content_url=image_url,
                                preview_image_url=image_url,
                            )
                        ],
                    )
                )
        except Exception as e:
            app.logger.error(f"DALL·E 3 API error: {e}")
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，生成圖像時發生錯誤。")],
                    )
                )
    else:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            response = query(event.message.text, previous_response_id=message_id)
            message_id = response.id
            html_msg = markdown.markdown(response.output_text)
            soup = BeautifulSoup(html_msg, "html.parser")

            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=soup.get_text())],
                )
            )


# === 處理圖片訊息 ===
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):

    # === 以下是處理圖片回傳部分 === #

    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        content = blob_api.get_message_content(message_id=event.message.id)
        image_bytes = content

    # Step 2：轉成 base64 字串
    base64_string = base64.b64encode(image_bytes).decode("utf-8")

    # Step 3：組成 OpenAI 的 data URI 格式
    data_uri = f"data:image/png;base64,{base64_string}"
    app.logger.info(f"Data URI: {data_uri}")

    # Step 4：將圖片存到本地端
    with tempfile.NamedTemporaryFile(
        dir=static_tmp_path, suffix=".jpg", delete=False
    ) as tf:
        tf.write(content)
        filename = os.path.basename(tf.name)

    image_url = f"https://{base_url}/images/{filename}"

    app.logger.info(f"Image URL: {image_url}")

    # === 以下是處理解釋圖片部分 === #
    response = client.responses.create(
        model="gpt-4.1-nano",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "describe the image in traditional chinese",
                    },
                    {
                        "type": "input_image",
                        "image_url": data_uri,
                    },
                ],
            }
        ],
    )
    app.logger.info(response.output_text)

    # === 以下是回傳圖片部分 === #

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    ImageMessage(
                        original_content_url=image_url, preview_image_url=image_url
                    ),
                    TextMessage(text=response.output_text),
                ],
            )
        )