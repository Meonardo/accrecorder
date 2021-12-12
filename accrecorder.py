#!/usr/bin/python
import argparse
import subprocess
import os
import cgi
import json

from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from obsclient import ObsClient
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
OBS_PROC = "accrecorder64"

obs_client = ObsClient()


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

    # Clean resources
    @staticmethod
    def shutdown():
        print("Web server is shutting down...")
        # close client session
        obs_client.close()

    # Send a JSON Response.
    @staticmethod
    def json_response(success, code, data):
        # 满足Windows客户端需求，进行修改
        if success:
            state = 1
        else:
            state = code
        if isinstance(data, dict) or isinstance(data, list):
            resp = {"state": state, "code": "Please see 'data' field.", "data": data}
        else:
            resp = {"state": state, "code": data}
        return resp

    # Configure record server per room
    def configure(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -3, "Please input correct Room number!")

        if 'cam1' not in form:
            return self.json_response(False, -2, "Please input RTSP cam address!")
        cam1 = str(form["cam1"])
        if not cam1.startswith('rtsp'):
            return self.json_response(False, -3, "Please input correct RTSP cam address!")

        if 'cam2' not in form:
            return self.json_response(False, -2, "Please input RTSP cam address!")
        cam2 = str(form["cam2"])
        if not cam2.startswith('rtsp'):
            return self.json_response(False, -3, "Please input correct RTSP cam address!")

        if 'monitor' not in form:
            return self.json_response(False, -2, "Please input monitor index!")
        monitor = str(form["monitor"])
        if not monitor.isdigit():
            return self.json_response(False, -4, "Please input correct monitor index!")

        mic = None
        if 'mic' in form:
            mic = str(form['mic'])

        success = obs_client.configure(str(room), cam1, cam2, mic, monitor)
        if success:
            return self.json_response(True, 0, "Room {} is configured".format(room))
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -6, "Current room {} already configured".format(room))

    # Reset record session in case of client had unexceptional satiation
    def reset(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -2, "Please input correct Room number!")

        success = obs_client.reset(room)
        if success:
            return self.json_response(True, 0, "Room {} is reset".format(room))
        else:
            return self.json_response(False, -3, "Current room {} not exist!".format(room))

    # Start recording
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

        success = obs_client.start_recording(room, cam, enable_screen)
        if success:
            return self.json_response(True, 0, "Start recording at room {}".format(room))
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -9, "Current room {r} is recording".format(r=room))

    # Stop recording
    def stop(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        data = obs_client.stop_recording(room)
        if data is not None:
            return self.json_response(True, 0, data)
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -3, "Current room is not recording")

    # Pause recording
    def pause(self, form):
        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        data = obs_client.pause_recording(room)
        if data is not None:
            return self.json_response(True, 0, data)
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -3, "Current room is not recording")

    # Change if need record screen
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
                success = obs_client.start_recording_screen(room)
                if not success:
                    return self.json_response(False, -4, "Recording screen does not available currently!")
                else:
                    return self.json_response(True, 0, "Screen start recording at room {}".format(room))
            else:
                success = obs_client.stop_recording_screen(room)
                if not success:
                    return self.json_response(False, -4, "Current screen is NOT recording!")
                else:
                    return self.json_response(True, 0, "Screen stop recording at room {}".format(room))
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -5, "Please input invalid command, 1 to start recording screen and 2 to stop recording screen")

    # Switch cameras
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

        success = obs_client.switch_camera(room, cam)
        if success:
            return self.json_response(True, 0, "Switch to CAM {}".format(cam))
        else:
            if not obs_client.obs_connected:
                return self.json_response(False, -10, "Connect recorder server failed.")
            return self.json_response(False, -3, "You already have record CAM {}".format(cam))


def __launch_obs():
    obs_dir = r"C:\Users\Meon\File\obs\obs-studio\build\rundir\Debug\bin\64bit"
    os.chdir(obs_dir)
    os.startfile(OBS_PROC + ".exe")


def __kill_obs():
    try:
        cmd = "powershell.exe Get-Process {0} | Stop-Process".format(OBS_PROC)
        subprocess.Popen(cmd)
        print("Kill obs proc succeffully")
    except Exception as e:
        print("Kill obs proc exception:", e)


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
        # run obs.exe
        __launch_obs()

        server.serve_forever()
    except (KeyboardInterrupt, Exception) as e:
        print("Received exception: ", e)
        pass
    finally:
        print("Stopping now!")
        # kill obs proc
        __kill_obs()

        server.shutdown()
        server.socket.close()
