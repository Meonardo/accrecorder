#!/usr/bin/python
import argparse
import subprocess

from aiohttp import web
from httpclient import HTTPClient
from recorder import print

client = HTTPClient()


# common response
def json_response(success, code, data):
    # 满足Windows客户端需求，进行修改
    if success:
        state = 1
    else:
        state = code
    resp = {"state": state, "code": data}
    print("Send response: ", resp)
    print("[END] \n")
    return web.json_response(resp)


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


def check_gpu():
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


# index
async def index(request):
    content = "Recording the conference!"
    return web.Response(content_type="text/html", text=content)


# Configure record server per room
async def configure(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -3, "Please input correct Room number!")

    if 'upload_server' not in form:
        return json_response(False, -2, "Please input upload server address!")
    upload_server = str(form['upload_server'])
    if not upload_server.startswith("http://"):
        return json_response(False, -2, "Upload server address is invalidate!")

    if 'class_id' not in form:
        return json_response(False, -4, "Please input class_id!")

    class_id = form['class_id']
    video_codec = 'h264_qsv'
    if check_gpu():
        video_codec = 'h264_nvenc'
    success = await client.configure(room, class_id, str(room), upload_server, video_codec)
    if success:
        return json_response(True, 0, "Room {} is configured".format(room))
    else:
        return json_response(False, -6, "Current room {} already configured".format(room))


# Reset record session in case of client had unexceptional satiation
async def reset(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -2, "Please input correct Room number!")

    success = await client.reset(room)
    if success:
        return json_response(True, 0, "Room {} is reset".format(room))
    else:
        return json_response(False, -3, "Current room {} not exist!".format(room))


# check start command
async def start(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    if 'cam' not in form:
        return json_response(False, -2, "Please input publisher ids to record!")
    cam = str(form["cam"])
    if not cam.startswith('rtsp'):
        return json_response(False, -3, "Please input correct publisher identifier!")

    enable_screen = False
    if 'screen' in form:
        enable_screen = int(form['screen']) == 1

    mic = None
    if 'mic' in form:
        mic = str(form['mic'])
        if not check_mic(mic):
            return json_response(False, -4, "Invalidate microphone device!")

    success = await client.start_recording(room, cam, mic, enable_screen)
    if success:
        return json_response(True, 0, "Start recording at room {}".format(room))
    else:
        return json_response(False, -9, "Current room {r} is recording".format(r=room))


# check stop command
async def stop(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    success = await client.stop_recording(room)
    if success:
        return json_response(True, 0, "Stop recording at room {}".format(room))
    else:
        return json_response(False, -3, "Current room is not recording")


# check stop command
async def pause(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    success = await client.pause_recording(room)
    if success:
        return json_response(True, 0, "Pause recording at room {}".format(room))
    else:
        return json_response(False, -3, "Current room is not recording")


async def recording_screen(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")
    # 1 start recording screen, 2 stop recording screen, other commands are invalidate
    if 'cmd' not in form:
        return json_response(False, -2, "Please input command!")
    cmd = form["cmd"]
    if not cmd.isdigit():
        return json_response(False, -4, "Please input correct command!")
    cmd = int(cmd)
    if 0 < cmd < 3:
        if cmd == 1:
            success = await client.start_recording_screen(room)
            if not success:
                return json_response(False, -4, "Recording screen does not available currently!")
            else:
                return json_response(True, 0, "Screen start recording at room {}".format(room))
        else:
            success = await client.stop_recording_screen(room)
            if not success:
                return json_response(False, -4, "Current screen is NOT recording!")
            else:
                return json_response(True, 0, "Screen stop recording at room {}".format(room))
    else:
        return json_response(False, -5, "Please input invalid command, 1 to start recording screen and 2 to stop "
                                        "recording screen")


# 切换摄像头
async def switch_camera(request):
    form = await request.post()
    print(u"[START]\n"
          u"Incoming request from {s}, {r} \n"
          u"Form: {f}".format(s=request.remote, r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    if 'cam' not in form:
        return json_response(False, -2, "Please input publisher ids to record!")
    cam = str(form["cam"])
    if not cam.startswith('rtsp'):
        return json_response(False, -3, "Please input correct publisher identifier!")

    mic = None
    if 'mic' in form:
        mic = str(form['mic'])
        if not check_mic(mic):
            return json_response(False, -4, "Invalidate microphone device!")

    success = await client.switch_camera(room, cam, mic)
    if success:
        return json_response(True, 0, "Switch to CAM {}".format(cam))
    else:
        return json_response(False, -3, "You already have record CAM {}".format(cam))


async def on_shutdown(app):
    print("Web server is shutting down...")
    # close client session
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrecorder")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9002, help="Port for HTTP server (default: 9002)"
    )
    args = parser.parse_args()

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)

    app.router.add_post("/record/configure", configure)
    app.router.add_post("/record/reset", reset)
    app.router.add_post("/record/start", start)
    app.router.add_post("/record/stop", stop)
    app.router.add_post("/record/pause", pause)
    app.router.add_post("/record/screen", recording_screen)
    app.router.add_post("/record/camera", switch_camera)

    try:
        print("Starting web server...")
        web.run_app(
            app, access_log=None, host=args.host, port=args.port
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("Web server stopped.")


