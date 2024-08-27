import json
import logging
import time
import re
import os
from google.cloud import firestore
from datetime import timedelta
from typing import Any, Optional, Union

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import LLMResult
from flask import Flask, request, jsonify
from slack_bolt.adapter.flask import SlackRequestHandler
CHAT_UPDATE_INTERVAL_SEC = 1

load_dotenv()

db = firestore.Client()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

# Slackアプリの初期化
app = App(
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    token=os.environ["SLACK_BOT_TOKEN"],
    process_before_response=True,
)

class SlackStreamingCallbackHandler(BaseCallbackHandler):
    last_send_time = time.time()
    message = ""

    def __init__(self, channel, ts):
        self.channel = channel
        self.ts = ts
        self.interval = CHAT_UPDATE_INTERVAL_SEC
        self.update_count = 0

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        self.message += token
        now = time.time()
        if now - self.last_send_time > self.interval:
            app.client.chat_update(
                channel=self.channel, ts=self.ts, text=f"{self.message}\n\nTyping.."
            )
            self.last_send_time = now
            self.update_count += 1

            if self.update_count / 10 > self.interval:
                self.interval = self.interval * 2

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        message_context = "AIChat answers are not always correct, so be sure to check important information."
        message_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": self.message}},
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": message_context}],
            },
        ]
        app.client.chat_update(
            channel=self.channel,
            ts=self.ts,
            blocks=message_blocks,
        )

@app.event("app_mention")
def handle_mention(event, say):
    channel = event["channel"]
    thread_ts = event["ts"]
    message = re.sub("<@.*>", "", event["text"])

    # メッセージをFirestoreに保存
    doc_ref = db.collection('chat_history').document(thread_ts)
    doc_ref.set({
        'message': message,
        'timestamp': thread_ts
    })

    llm = ChatOpenAI(
        model_name=os.environ["OPENAI_API_MODEL"],
        temperature=float(os.environ["OPENAI_API_TEMPERATURE"]),
        callbacks=[],
    )
    callback_handler = SlackStreamingCallbackHandler(channel=channel, ts=thread_ts)
    llm.callbacks.append(callback_handler)

    response = llm.predict(message)
    say(text=response, channel=channel, thread_ts=thread_ts)

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


handler = SlackRequestHandler(slack_app)

@app.route("/slack/events", methods=["POST"])
def slack_events():
    if request.headers.get('X-Slack-Retry-Num'):
        return jsonify(status=200, headers={"X-Slack-No-Retry": "1"})

    return handler.handle(request)
