import json
import urllib.request
import urllib.error

url = "http://127.0.0.1:18080/solve"
request_body = {
    "problem_text": "求不定积分 ∫ x^2 * e^x dx",
    "section_no": "",
    "problem_no": ""
}

print("=== 发送请求 ===")
print("URL:", url)
print("请求体:", json.dumps(request_body, ensure_ascii=False))

req = urllib.request.Request(
    url,
    data=json.dumps(request_body).encode('utf-8'),
    headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; Python-urllib/3.12)"  # 添加 User-Agent
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("状态码:", resp.status)
        body = resp.read().decode()
        print("响应体:", body)
except urllib.error.HTTPError as e:
    print("HTTP 错误:", e.code)
    print("响应头:", e.headers)
    print("错误体:", e.read().decode())
except Exception as e:
    print("其他错误:", e)
