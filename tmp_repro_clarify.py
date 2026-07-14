import asyncio
import http.server
import json
import os
import socketserver
import threading

from bank_audit.loophole.chat.clarify import generate_clarifications


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        print("RECEIVED:", body.decode("utf-8")[:500])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        content_json = '{"complete": true}'
        resp = json.dumps({"choices": [{"message": {"content": content_json}}]}, ensure_ascii=False)
        self.wfile.write(resp.encode("utf-8"))

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
os.environ["LLM_API_KEY"] = "fake"


async def main():
    try:
        result = await generate_clarifications("Привет, какие лазейки в Сбере?")
        print("RESULT:", result)
    except Exception as e:
        print("ERROR:", type(e).__name__, e)
    finally:
        srv.shutdown()


asyncio.run(main())
