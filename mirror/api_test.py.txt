import anthropic
import os
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

message = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=100,
    messages=[{"role": "user", "content": 
        "Write exactly one sentence under 30 words about this trade: "
        "TSLA call, $5.2M premium, 4 DTE, bullish signal. "
        "Start with a dollar amount."}]
)

raw = message.content[0].text
print(f"Raw response: [{raw}]")
print(f"Length: {len(raw)}")
print(f"First char: [{raw[0]}]")