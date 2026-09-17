# Linebot Chatgpt Project

LINE Bot 串接 DOAJ(Directory of Open Access Journals)學術文章搜尋 API 與 ChatGPT,讓使用者透過 LINE 對話查詢學術文章並取得摘要。

## 運作流程

1. 使用者在 LINE 輸入 `query` 開始查詢流程
2. 依序輸入主題關鍵字與想要的文章數量(最多 25 篇)
3. 程式呼叫 DOAJ API 取得相關文章
4. 逐篇用 ChatGPT(`gpt-4o-mini`)產生繁體中文摘要重點
5. 透過 LINE Messaging API 逐一推送結果給使用者

## 技術棧

Python + Flask + OpenAI API + LINE Messaging API + DOAJ API,支援多用戶並行處理(threading)與中途「停止」指令。

## 環境設定

複製 `.env.example` 為 `.env`,填入 `OPENAI_API_KEY`、`LINE_ACCESS_TOKEN` 與 `LINE_CHANNEL_SECRET` 後執行:

```bash
pip install -r requirements.txt
python app.py
```

> ⚠️ 本專案曾經把金鑰直接寫在程式碼裡並 commit 進 git,已改成讀環境變數,但**舊 commit 歷史裡仍留有先前的金鑰**,請務必先撤銷重發該金鑰再使用。

## Webhook 安全性修補(2026/09新增)

原本的 `/webhook` 完全沒有驗證請求來源——任何人只要知道網址,就能偽造請求讓伺服器用這個 Bot 的 LINE token 推播訊息給任意 `userId`,或觸發 DOAJ+OpenAI API 呼叫消耗額度。現在比照 civil-law-qa-bot 的做法加上 LINE 官方簽章驗證(`base64(HMAC-SHA256(channel_secret, request_body))` 要跟 `X-Line-Signature` header 相符),簽章不對直接拒絕(400)。

同時修了兩個防禦性問題:原本只處理 `events[0]`,LINE 一次送多個事件時後面的會被忽略;原本假設每個事件都是文字訊息,收到貼圖/加好友等其他事件類型會直接 `KeyError` 讓 webhook 整個 500。現在會逐一處理 `events` 陣列裡的所有事件,並且對非文字訊息事件直接略過而不是崩潰。
