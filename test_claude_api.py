"""Claude API 연결 테스트: '안녕' 전송 후 응답 출력."""

import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not API_KEY:
    print("ANTHROPIC_API_KEY가 .env에 없습니다.", file=sys.stderr)
    sys.exit(1)

client = Anthropic(api_key=API_KEY)

message = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=256,
    messages=[{"role": "user", "content": "안녕"}],
)

for block in message.content:
    if block.type == "text":
        print(block.text)
