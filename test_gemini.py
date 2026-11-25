import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.environ["GEMINI_API_KEY"].strip()
model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
print("model repr:", repr(model), "| key length:", len(key))
r = requests.post(
    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    params={"key": key},
    json={"contents": [{"role": "user", "parts": [{"text": "Say hi"}]}]},
    timeout=60,
)
print(r.status_code)
print(r.text[:600])
