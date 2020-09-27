import threading
import socket

from .log import log

REPL_HOST = ''


class Repl:
    def __init__(self, port) -> None:
        self.sock = None
        self.port = port
        thread = threading.Thread(target=self.start)
        thread.setDaemon(True)
        thread.start()

    def start(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR or socket.SO_REUSEPORT, 1)
        self.sock.bind((REPL_HOST, self.port))
        self.sock.listen()
        while True:
            log.debug(f"REPL initialized on port {self.port}")
            conn, addr = self.sock.accept()
            with conn:
                log.debug(f"Connected by {addr}")
                while True:
                    data = conn.recv(1024)
                    if not data:
                        break
                    line = data.decode("utf-8").strip()
                    response = ""
                    if line == "ping":
                        response = "Pong!\n"
                    conn.send(response.encode("utf-8"))

    def stop(self):
        if self.sock:
            self.sock.close()