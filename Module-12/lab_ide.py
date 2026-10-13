from collections import deque
import base64
import gzip
import html
import http.cookiejar
import json
import re
import shlex
import struct
import time
import urllib.parse
import urllib.request
import uuid


def vql(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def encode(value):
    if value is None:
        return b"\x00"
    if isinstance(value, str):
        data = value.encode()
        return b"\x01" + vql(len(data)) + data
    if isinstance(value, list):
        return b"\x04" + vql(len(value)) + b"".join(encode(item) for item in value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return b"\x06" + vql(value)
    data = json.dumps(value, separators=(",", ":")).encode()
    return b"\x05" + vql(len(data)) + data


def decode(data, offset=0):
    kind = data[offset]
    offset += 1
    if kind == 0:
        return None, offset
    size, shift = 0, 0
    while True:
        byte = data[offset]
        offset += 1
        size |= (byte & 127) << shift
        if byte < 128:
            break
        shift += 7
    if kind == 6:
        return size, offset
    if kind == 4:
        result = []
        for _ in range(size):
            value, offset = decode(data, offset)
            result.append(value)
        return result, offset
    value = data[offset:offset + size]
    if kind == 1:
        value = value.decode()
    elif kind == 5:
        value = json.loads(value)
    return value, offset + size


class LabIDE:
    def __init__(self, url, password):
        import websocket
        self.websocket = websocket
        parsed = urllib.parse.urlsplit(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(url, timeout=60) as response:
            page = response.read()
        if page.startswith(b"\x1f\x8b"):
            page = gzip.decompress(page)
        if b'vscode-workbench-web-configuration' not in page:
            payload = urllib.parse.urlencode({"password": password, "base": ".", "href": url}).encode()
            with opener.open(urllib.request.Request(base + "/login", data=payload), timeout=60) as response:
                page = response.read()
            if page.startswith(b"\x1f\x8b"):
                page = gzip.decompress(page)
        match = re.search(r'<meta[^>]*id="vscode-workbench-web-configuration"[^>]*data-settings="([^"]*)"', page.decode())
        if not match:
            raise RuntimeError("Lab IDE login failed. Check the lab stack outputs.")
        settings = json.loads(html.unescape(match.group(1)))
        query = urllib.parse.urlencode({"reconnectionToken": str(uuid.uuid4()), "reconnection": "false", "skipWebSocketFrames": "false"})
        self.socket = websocket.create_connection(
            f"wss://{parsed.netloc}{settings.get('serverBasePath', '/')}?{query}",
            cookie="; ".join(f"{item.name}={item.value}" for item in jar),
            origin=base, timeout=5,
        )
        self.buffer = b""
        self.sent = 0
        self.received = 0
        self.request_id = 0
        self.responses = {}
        self.events = deque()
        token = settings.get('connectionToken') or urllib.parse.parse_qs(parsed.query).get('tkn', ['00000000000000000000'])[0]
        self.send(2, json.dumps({"type": "auth", "auth": token, "data": str(uuid.uuid4())}).encode())
        kind, body = self.receive()
        challenge = json.loads(body)
        if kind != 2 or challenge.get("type") != "sign":
            raise RuntimeError("Unexpected Lab IDE handshake: " + challenge.get('reason', challenge.get('type', str(kind))))
        self.send(2, json.dumps({"type": "connectionType", "signedData": challenge["data"], "desiredConnectionType": 1}).encode())
        self.send(1, encode({"remoteAuthority": settings.get("remoteAuthority", "remote"), "clientId": str(uuid.uuid4())}))
        self.send(1, encode([200]) + encode(None))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            kind, body = self.receive()
            if kind == 2:
                message = json.loads(body)
                if message.get("type") == "error":
                    raise RuntimeError(message.get("reason", "Lab IDE rejected the connection."))
            if kind == 1:
                header, _ = decode(body)
                if header[0] == 200:
                    return
        raise TimeoutError("Lab IDE management connection timed out.")

    def send(self, kind, body=b""):
        if kind == 1:
            self.sent += 1
        packet = struct.pack(">BIII", kind, self.sent if kind == 1 else 0, self.received, len(body)) + body
        self.socket.send_binary(packet)

    def receive(self):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if len(self.buffer) >= 13:
                kind, message_id, ack, size = struct.unpack(">BIII", self.buffer[:13])
                if len(self.buffer) >= 13 + size:
                    body = self.buffer[13:13 + size]
                    self.buffer = self.buffer[13 + size:]
                    if kind == 1:
                        self.received = message_id
                        self.send(3)
                    if kind in (1, 2):
                        return kind, body
                    continue
            try:
                data = self.socket.recv()
            except self.websocket.WebSocketTimeoutException:
                self.send(9)
                continue
            if not data:
                raise ConnectionError("Lab IDE connection closed.")
            self.buffer += data.encode() if isinstance(data, str) else data
        raise TimeoutError("Lab IDE did not respond within 60 seconds.")

    def request(self, method, args=None, event=False, wait=True):
        self.request_id += 1
        request_id = self.request_id
        self.send(1, encode([102 if event else 100, request_id, "remoteterminal", method]) + encode(args))
        if event or not wait:
            return request_id
        while request_id not in self.responses:
            self.pump()
        result, error = self.responses.pop(request_id)
        if error:
            raise RuntimeError(f"Lab IDE {method}: {result}")
        return result

    def pump(self):
        kind, body = self.receive()
        if kind != 1:
            return
        header, offset = decode(body)
        value, _ = decode(body, offset)
        if header[0] in (201, 202, 203):
            self.responses[header[1]] = (value, header[0] != 201)
        elif header[0] == 204:
            self.events.append((header[1], value))

    def run(self, script, timeout=1800):
        payload = base64.b64encode(script.encode()).decode()
        ready = 'LAB12_INPUT_READY_' + uuid.uuid4().hex
        loader = f"import base64,subprocess,sys; print({ready!r},flush=True); data=sys.stdin.buffer.read({len(payload)}); sys.exit(subprocess.run(['/bin/bash'],input=base64.b64decode(data)).returncode)"
        launcher = 'stty -echo -icanon; exec python3 -u -c ' + shlex.quote(loader)
        data_event = self.request("$onProcessDataEvent", event=True)
        exit_event = self.request("$onProcessExitEvent", event=True)
        config = {
            "configuration": {"terminal.integrated.env.linux": {}, "terminal.integrated.detectLocale": "auto"},
            "resolvedVariables": {}, "envVariableCollections": [],
            "shellLaunchConfig": {"executable": "/bin/bash", "args": ["-lc", launcher], "cwd": "/home/ec2-user/environment", "useShellEnvironment": True},
            "workspaceId": "lab12-terraform", "workspaceName": "Lab 12", "workspaceFolders": [],
            "shouldPersistTerminal": False, "options": {"shellIntegration": {"enabled": False}},
            "cols": 120, "rows": 30, "unicodeVersion": "11", "resolverEnv": {},
        }
        process = self.request("$createProcess", config)
        terminal_id = process["persistentTerminalId"]
        started = self.request("$start", [terminal_id])
        if started:
            raise RuntimeError(f"Lab IDE terminal failed to start: {started}")
        output = []
        input_sent = False
        ready_buffer = ''
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                while self.events:
                    event_id, event = self.events.popleft()
                    if event.get("id") != terminal_id:
                        continue
                    if event_id == data_event:
                        data = event["event"]
                        data = data if isinstance(data, str) else data["data"]
                        if not input_sent:
                            ready_buffer += data
                            if ready not in ready_buffer:
                                continue
                            input_sent = True
                            for offset in range(0, len(payload), 32768):
                                self.request('$input', [terminal_id, payload[offset:offset + 32768]], wait=False)
                            data = ready_buffer.split(ready, 1)[1].lstrip('\r\n')
                        output.append(data)
                        print(data, end="", flush=True)
                        self.request("$acknowledgeDataEvent", [terminal_id, len(data.encode('utf-16-le')) // 2], wait=False)
                    elif event_id == exit_event:
                        if event.get("event") != 0:
                            raise RuntimeError(f"Remote command exited with {event.get('event')}.")
                        return "".join(output)
                self.pump()
            raise TimeoutError("Lab IDE command timed out.")
        finally:
            self.request("$shutdown", [terminal_id, True], wait=False)

    def close(self):
        self.socket.close()
