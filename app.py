from flask import Flask, request, jsonify, abort
import requests
from openai import OpenAI
import time
import threading
import os
import hmac
import hashlib
import base64

app = Flask(__name__)

# 初始化 OpenAI GPT 客戶端
client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url="https://api.chatanywhere.tech/v1"
)

# LINE Messaging API 的設定
LINE_ACCESS_TOKEN = os.environ["LINE_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]


def verify_line_signature(body_bytes: bytes, signature: str) -> bool:
    """驗證請求真的來自 LINE 平台,不是任何人拿 webhook 網址亂打的假請求。

    原本這個 /webhook 完全沒有驗證,任何人只要知道網址,就能偽造請求讓
    伺服器用這個 Bot 的 LINE token 去推播訊息給任意 userId、或觸發
    DOAJ+OpenAI 呼叫消耗額度。驗證方式是 LINE 官方演算法:
    base64(HMAC-SHA256(channel_secret, request_body)) 要跟
    X-Line-Signature header 完全相同。
    """
    if not signature:
        return False
    hash_ = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    expected_signature = base64.b64encode(hash_).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)

# 全局變量，存儲用戶上下文和處理狀態
user_context = {}
user_processing = set()  # 用來追踪哪些用戶正在處理查詢

# 步驟 1：從 DOAJ API 獲取文章數據
def fetch_doaj_articles(query, num_results=5):
    if num_results > 25:  # 設定文章數量上限
        num_results = 25
    url = f"https://doaj.org/api/v2/search/articles/{query}"
    params = {"pageSize": num_results}
    print(f"正在從 DOAJ 搜索與 \"{query}\" 相關的文章...")
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        articles = data.get("results", [])
        
        print(f"成功獲取到 {len(articles)} 篇文章。\n")
        return [{"title": article["bibjson"]["title"],
                 "link": article["bibjson"].get("link", [{}])[0].get("url", "無有效連結")}
                for article in articles]
    except Exception as e:
        print(f"無法獲取文章數據，錯誤: {str(e)}")
        return []

# 步驟 2：使用 ChatGPT API 提取摘要重點
def chat_gpt(prompt):
    print(f"正在提取摘要重點...")
    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="gpt-4o-mini",
        )
        summary = chat_completion.choices[0].message.content
        print(f"提取完成，摘要重點（前100字）：{summary[:100]}...\n")
        return summary
    except Exception as e:
        error_message = f"錯誤: {str(e)}"
        print(f"提取失敗: {error_message}")
        return error_message

# 步驟 3：透過 LINE API 傳送摘要
def send_to_line(user_id, message):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    data = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"訊息已成功傳送至 LINE。\n")
    except Exception as e:
        print(f"無法傳送訊息到 LINE，錯誤: {str(e)}")

# 處理查詢並發送結果
def process_query(user_id, query, num_results):
    try:
        # 記錄用戶處理中狀態
        user_processing.add(user_id)
        
        # 獲取文章並提取摘要
        articles = fetch_doaj_articles(query, num_results=num_results)
        
        # 通知用戶獲取到的文章數量
        send_to_line(user_id, f"成功抓取到 {len(articles)} 篇文章，開始進行摘要分析...")

        for i, article in enumerate(articles):
            if user_id not in user_processing:
                break
            print(f"處理第 {i + 1} 篇文章: {article['title']}")
            if article["link"] and article["link"] != "無有效連結":
                article['summary'] = chat_gpt(f"以下是學術文章的標題和連結，請根據文章生成摘要重點：\n標題: {article['title']}\n連結: {article['link']}\n\n摘要重點，輸出請使用繁體中文，勿列點：")
            else:
                article['summary'] = "無有效連結，無法提取摘要。"
            
            # 傳送到 LINE
            message = f"文章標題: {article['title']}\n摘要: {article['summary']}\n連結: {article['link']}"
            send_to_line(user_id, message)
            time.sleep(1)
        if user_id in user_processing:
            # 所有結果輸出完成後通知用戶
            send_to_line(user_id, "所有結果已經輸出完成，感謝您的使用！")
    finally:
        # 清除用戶處理中狀態和上下文
        if user_id in user_processing:
            user_processing.remove(user_id)
        user_context.pop(user_id, None)

# LINE Webhook 處理
@app.route('/webhook', methods=['POST'])
def webhook():
    # 驗證請求真的來自 LINE(2026/09新增,原本完全沒有驗證)
    signature = request.headers.get('X-Line-Signature', '')
    body_bytes = request.get_data()
    if not verify_line_signature(body_bytes, signature):
        print("簽章驗證失敗,拒絕請求")
        abort(400)

    body = request.get_json(silent=True) or {}
    print("收到的 Webhook 數據：", body)

    # 逐一處理這批 webhook 裡的所有事件,不是只處理第一個
    # (LINE平台可能一次送多個events;之前的版本只看events[0],其他事件會被忽略)
    for event in body.get('events', []):
        handle_single_event(event)

    return jsonify({"status": "success"}), 200


def handle_single_event(event: dict):
    """處理單一 LINE 事件,對非文字訊息(貼圖、加好友、postback等)防禦性略過。"""
    # 只處理文字訊息事件,其他事件類型(加入好友、封鎖、貼圖、圖片等)直接略過
    # 而不是讓 body['message']['text'] 這種假設拋出 KeyError 讓整個webhook 500
    if event.get('type') != 'message':
        print(f"略過非訊息事件: {event.get('type')}")
        return
    message = event.get('message', {})
    if message.get('type') != 'text':
        print(f"略過非文字訊息: {message.get('type')}")
        return

    source = event.get('source', {})
    user_id = source.get('userId')
    if not user_id:
        print("事件缺少 userId,略過")
        return
    user_message = message.get('text', '').strip().lower()

    # 處理停止命令
    if user_message == "停止":
        if user_id in user_processing:
            user_processing.remove(user_id)
        user_context.pop(user_id, None)
        send_to_line(user_id, "操作已停止，請輸入 'query' 重新開始。")
        return

    # 如果用戶正在處理中，忽略新消息
    if user_id in user_processing:
        send_to_line(user_id, "目前正在處理您的請求，請稍後再試！")
        return

    # 檢查用戶是否在上下文中
    if user_id not in user_context:
        # 初次輸入
        if user_message == "query":
            user_context[user_id] = {"stage": "awaiting_query"}
            send_to_line(user_id, "請輸入主題關鍵字：")
        else:
            send_to_line(user_id, "請先輸入 'query' 開始查詢流程！")
    else:
        # 根據上下文階段處理
        context = user_context[user_id]
        if context["stage"] == "awaiting_query":
            # 用戶輸入主題關鍵字
            context["query"] = user_message
            context["stage"] = "awaiting_num"
            send_to_line(user_id, "請輸入想要查詢的文章數量（最多 25 篇）：")
        elif context["stage"] == "awaiting_num":
            # 用戶輸入文章數量
            try:
                num_results = int(user_message)
                if num_results < 1:
                    raise ValueError
                context["num_results"] = num_results
                send_to_line(user_id, f"已收到您的查詢：主題 '{context['query']}'，文章數量：{num_results} 篇。正在處理中...")

                # 啟動新線程處理查詢
                threading.Thread(target=process_query, args=(user_id, context["query"], num_results)).start()
            except ValueError:
                send_to_line(user_id, "請輸入有效的文章數量（必須是正整數）！")
        else:
            send_to_line(user_id, "請重新開始查詢流程，輸入 'query'！")

if __name__ == "__main__":
    # app.run(debug=True, port=os.getenv("PORT", default=5000), host='0.0.0.0')
    # 本地測試用
    app.run(debug=True, port=5000, host='localhost')