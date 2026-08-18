from http.server import BaseHTTPRequestHandler, HTTPServer
import json

HOST = "127.0.0.1"
PORT = 5000

latest_data = []


class BridgeHandler(BaseHTTPRequestHandler):

    def send_cors_headers(self):
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

    # Handle Chrome's CORS preflight request
    def do_OPTIONS(self):

        self.send_response(204)

        self.send_cors_headers()

        self.end_headers()

    # Receive events from Chrome extension / DDAS
    def do_POST(self):

        if self.path == "/event":

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    0
                )
            )

            body = self.rfile.read(
                content_length
            )

            try:

                data = json.loads(
                    body.decode("utf-8")
                )

                latest_data.append(data)

                print("\nDDAS EVENT RECEIVED:")
                print(
                    json.dumps(
                        data,
                        indent=4
                    )
                )

                self.send_response(200)

                self.send_cors_headers()

                self.send_header(
                    "Content-Type",
                    "application/json"
                )

                self.end_headers()

                self.wfile.write(
                    json.dumps({
                        "success": True
                    }).encode()
                )

            except Exception as error:

                self.send_response(400)

                self.send_cors_headers()

                self.send_header(
                    "Content-Type",
                    "application/json"
                )

                self.end_headers()

                self.wfile.write(
                    json.dumps({
                        "success": False,
                        "error": str(error)
                    }).encode()
                )

        else:

            self.send_response(404)

            self.send_cors_headers()

            self.end_headers()

    # DDAS dashboard reads events
    def do_GET(self):

        if self.path == "/events":

            self.send_response(200)

            self.send_cors_headers()

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.end_headers()

            self.wfile.write(
                json.dumps(
                    latest_data
                ).encode()
            )

        else:

            self.send_response(404)

            self.send_cors_headers()

            self.end_headers()


if __name__ == "__main__":

    server = HTTPServer(
        (HOST, PORT),
        BridgeHandler
    )

    print(
        f"DDAS Communication Bridge running at "
        f"http://{HOST}:{PORT}"
    )

    server.serve_forever()
