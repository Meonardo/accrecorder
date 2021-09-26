#!/usr/bin/python
import argparse
import subprocess
import os
import cgi
import json

from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from httpclient import HTTPClient
from recorder import print

ROOT = os.path.abspath(os.path.dirname(__file__))
ResponseStatus = namedtuple("HTTPStatus",
                            ["code", "message"])

HTTP_STATUS = {"OK": ResponseStatus(code=200, message="OK"),
               "BAD_REQUEST": ResponseStatus(code=400, message="Bad request"),
               "NOT_FOUND": ResponseStatus(code=404, message="Not found"),
               "INTERNAL_SERVER_ERROR": ResponseStatus(code=500, message="Internal server error")}

ROUTE_INDEX = "/index.html"

ROUTE_STOP = "/record/stop"
ROUTE_START = "/record/start"
ROUTE_CONFIGURE = "/record/configure"
ROUTE_RESET = "/record/reset"
ROUTE_PAUSE = "/record/pause"
ROUTE_RECORD_SCREEN = "/record/screen"
ROUTE_SWITCH_CAMERA = "/record/camera"

client = HTTPClient()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""


class HTTPStatusError(Exception):
    """Exception wrapping a value from http.server.HTTPStatus"""

    def __init__(self, status, description=None):
        """
        Constructs an error instance from a tuple of
        (code, message, description), see http.server.HTTPStatus
        """
        super(HTTPStatusError, self).__init__()
        self.code = status.code
        self.message = status.message
        self.explain = description


class RequestHandler(BaseHTTPRequestHandler):
    # 404 Not found.
    def route_not_found(self, path, query):
        """Handles routing for unexpected paths"""
        raise HTTPStatusError(HTTP_STATUS["NOT_FOUND"], "Page not found")

    # Handler for the GET requests
    def do_GET(self):
        print("Current requesting path: %s", self.path)

        path, _, query_string = self.path.partition('?')
        query_components = dict(qc.split("=") for qc in query_string.split("&"))

        print(u"[START]\n"
              u"Received GET for %s with query: %s" % (path, query_components))

        try:
            if path == ROUTE_INDEX:
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                # Send the html message
                self.wfile.write("RTSP Stream push to Janus!".encode())
            else:
                response = self.route_not_found(path, query_components)
        except HTTPStatusError as err:
            self.send_error(err.code, err.message)

        print("[END]\n")

        return

    # Handler for the POST requests
    def do_POST(self):
        path, _, _ = self.path.partition('?')

        print(u"[START]\n"
              u"Received POST for %s" % path)

        try:
            fs = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST',
                         'CONTENT_TYPE': self.headers['Content-Type'],
                         })

            form = {}
            for field in fs.list or ():
                form[field.name] = field.value

            print("In coming form: ", form)

            if path == ROUTE_START:
                r = self.start(form)
                self.send_json_response(r)
            elif path == ROUTE_STOP:
                r = self.stop(form)
                self.send_json_response(r)
            elif path == ROUTE_PAUSE:
                r = self.pause(form)
                self.send_json_response(r)
            elif path == ROUTE_RESET:
                r = self.reset(form)
                self.send_json_response(r)
            elif path == ROUTE_CONFIGURE:
                r = self.configure(form)
                self.send_json_response(r)
            elif path == ROUTE_SWITCH_CAMERA:
                r = self.switch_camera(form)
                self.send_json_response(r)
            elif path == ROUTE_RECORD_SCREEN:
                r = self.record_screen(form)
                self.send_json_response(r)

        except HTTPStatusError as err:
            self.send_error(err.code, err.message)

        print("[END]\n")

        return

    # Send a JSON Response.
    def send_json_response(self, json_dict):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        obj = json.dumps(json_dict, indent=4)
        self.wfile.write(obj.encode(encoding='utf_8'))
        print("Send response: ", obj)
        return

    @staticmethod
    def check_mic(mic):
        result = subprocess.run(['ffmpeg', '-f', 'dshow', '-list_devices', '1', '-i', 'dummy'],
                                capture_output=True, text=True, encoding="utf-8")

        # print(result)
        if result.stderr is not None:
            if mic in result.stderr:
                return True
        if result.stdout is not None:
            if mic in result.stdout:
                return True
        return False

    @staticmethod
    def check_gpu():
        try:
            result = subprocess.run(['nvidia-smi', '-L'],
                                    capture_output=True, text=True, encoding="utf-8")
            print('GPU: ', result)
            target = 'nvidia'
            if result.stderr is not None:
                if target in result.stderr:
                    return True
            if result.stdout is not None:
                if target in result.stdout:
                    return True
            return False
        except Exception as e:
            print('Check GPU codec error', e)
            return False

    # Clean resources
    @staticmethod
    def shutdown():
        print("Web server is shutting down...")
        # close client session
        client.close()

    # Send a JSON Response.
    @staticmethod
    def json_response(success, code, data):
        # 满足Windows客户端需求，进行修改
        if success:
            state = 1
        else:
            state = code
        return {"state": state, "code": data}

    # Configure record server per room
    def configure(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -3, "Please input correct Room number!")

        if 'upload_server' not in form:
            return self.json_response(False, -2, "Please input upload server address!")
        upload_server = str(form['upload_server'])
        if not upload_server.startswith("http://"):
            return self.json_response(False, -2, "Upload server address is invalidate!")

        if 'class_id' not in form:
            return self.json_response(False, -4, "Please input class_id!")

        class_id = form['class_id']
        video_codec = 'h264_qsv'
        if self.check_gpu():
            video_codec = 'h264_nvenc'
        success = client.configure(room, class_id, str(room), upload_server, video_codec)
        if success:
            return self.json_response(True, 0, "Room {} is configured".format(room))
        else:
            return self.json_response(False, -6, "Current room {} already configured".format(room))

    # Reset record session in case of client had unexceptional satiation
    def reset(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -2, "Please input correct Room number!")

        success = client.reset(room)
        if success:
            return self.json_response(True, 0, "Room {} is reset".format(room))
        else:
            return self.json_response(False, -3, "Current room {} not exist!".format(room))

    # check start command
    def start(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        if 'cam' not in form:
            return self.json_response(False, -2, "Please input publisher ids to record!")
        cam = str(form["cam"])
        if not cam.startswith('rtsp'):
            return self.json_response(False, -3, "Please input correct publisher identifier!")

        enable_screen = False
        if 'screen' in form:
            enable_screen = int(form['screen']) == 1

        mic = None
        if 'mic' in form:
            mic = str(form['mic'])
            if not self.check_mic(mic):
                return self.json_response(False, -4, "Invalidate microphone device!")

        success = client.start_recording(room, cam, mic, enable_screen)
        if success:
            return self.json_response(True, 0, "Start recording at room {}".format(room))
        else:
            return self.json_response(False, -9, "Current room {r} is recording".format(r=room))

    # check stop command
    def stop(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        success = client.stop_recording(room)
        if success:
            return self.json_response(True, 0, "Stop recording at room {}".format(room))
        else:
            return self.json_response(False, -3, "Current room is not recording")

    # check stop command
    def pause(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        success = client.pause_recording(room)
        if success:
            return self.json_response(True, 0, "Pause recording at room {}".format(room))
        else:
            return self.json_response(False, -3, "Current room is not recording")

    def record_screen(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")
        # 1 start recording screen, 2 stop recording screen, other commands are invalidate
        if 'cmd' not in form:
            return self.json_response(False, -2, "Please input command!")
        cmd = form["cmd"]
        if not cmd.isdigit():
            return self.json_response(False, -4, "Please input correct command!")
        cmd = int(cmd)
        if 0 < cmd < 3:
            if cmd == 1:
                success = client.start_recording_screen(room)
                if not success:
                    return self.json_response(False, -4, "Recording screen does not available currently!")
                else:
                    return self.json_response(True, 0, "Screen start recording at room {}".format(room))
            else:
                success = client.stop_recording_screen(room)
                if not success:
                    return self.json_response(False, -4, "Current screen is NOT recording!")
                else:
                    return self.json_response(True, 0, "Screen stop recording at room {}".format(room))
        else:
            return self.json_response(False, -5, "Please input invalid command, 1 to start recording screen and 2 to stop "
                                            "recording screen")

    # 切换摄像头
    def switch_camera(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        if 'cam' not in form:
            return self.json_response(False, -2, "Please input publisher ids to record!")
        cam = str(form["cam"])
        if not cam.startswith('rtsp'):
            return self.json_response(False, -3, "Please input correct publisher identifier!")

        mic = None
        if 'mic' in form:
            mic = str(form['mic'])
            if not self.check_mic(mic):
                return self.json_response(False, -4, "Invalidate microphone device!")

        success = client.switch_camera(room, cam, mic)
        if success:
            return self.json_response(True, 0, "Switch to CAM {}".format(cam))
        else:
            return self.json_response(False, -3, "You already have record CAM {}".format(cam))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="recorder")
    parser.add_argument(
        "--port",
        type=int,
        default=9002,
        help="HTTP port number, default is 9002",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    server = ThreadedHTTPServer(('', args.port), RequestHandler)
    print('Started httpserver on port', args.port)

    try:
        server.serve_forever()
    except (KeyboardInterrupt, Exception) as e:
        print("Received exception: ", e)
        pass
    finally:
        print("Stopping now!")
        server.shutdown()
        server.socket.close()
