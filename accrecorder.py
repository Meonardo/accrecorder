#!/usr/bin/python
import os
import argparse
import asyncio

from aiohttp import web
from wsclient import WebSocketClient
from httpclient import HTTPClient

ROOT = os.path.dirname(__file__)
client = HTTPClient()


# common response   
def json_response(success, code, data):
    return {"success": success, "code": code, "data": data}
    # return json.dumps(dict, indent = 4).encode(encoding='utf_8')


# index
async def index(request):
    content = "Recording the conference!"
    return web.Response(content_type="text/html", text=content)


# check start command
async def start(request):
    form = await request.post()
    print(u"[START]:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    resp = json_response(False, 0, "default response")

    room = form["room"]
    if not room.isdigit():
        resp = json_response(False, -1, "Please input correct Room number!")

    publisher = form["publisher"]
    if not publisher.isdigit():
        resp = json_response(False, -2, "Please input correct publisher identifier!")

    success = await client.start_recording(room, [1])
    if success:
        resp = json_response(True, 0, "Start recording...")
    else:
        resp = json_response(False, -3, "Current room {r} is recording".format(r=room))

    print("[END]")
    return web.json_response(resp)


# check stop command
async def stop(request):
    form = await request.post()
    print(u"[START]:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    resp = json_response(False, 0, "default response")

    room = form["room"]
    if not room.isdigit():
        resp = json_response(False, -1, "Please input correct Room number!")

    success = await client.stop_recording(room)
    if success:
        resp = json_response(True, 0, "Stop recording")
    else:
        resp = json_response(False, -3, "Current publisher is not recording")

    print("[END]")
    return web.json_response(resp)


async def recording_screen(request):
    form = await request.post()
    print(u"[START]:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    resp = json_response(False, 0, "default response")

    room = form["room"]
    if not room.isdigit():
        resp = json_response(False, -1, "Please input correct Room number!")
    # 1 start recording screen, 2 stop recording screen, other commands are invalidate
    cmd = form["cmd"]
    if not cmd.isdigit():
        resp = json_response(False, -4, "Please input correct command!")
    cmd = int(cmd)
    if 0 < cmd < 3:
        if cmd == 1:
            success = await client.start_recording_screen(room)
            if not success:
                resp = json_response(False, -4, "Current screen is recording!")
            else:
                resp = json_response(True, 0, "Screen start recording")
        else:
            success = await client.stop_recording_screen(room)
            if not success:
                resp = json_response(False, -4, "Current screen is NOT recording!")
            else:
                resp = json_response(True, 0, "Screen stop recording")
    else:
        resp = json_response(False, -5, "Please input invalid command, 1 to start recording screen and 2 to stop "
                                        "recording screen")
    print("[END]")
    return web.json_response(resp)


# 切换摄像头
async def switch_camera(request):
    form = await request.post()
    print(u"[START]:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    resp = json_response(False, 0, "default response")

    room = form["room"]
    if not room.isdigit():
        resp = json_response(False, -1, "Please input correct Room number!")

    publisher = form["publisher"]
    if not publisher.isdigit():
        resp = json_response(False, -2, "Please input correct publisher identifier!")

    success = await client.switch_camera(room, int(publisher))
    if success:
        resp = json_response(True, 0, "Switch to CAM{}".format(publisher))
    else:
        resp = json_response(False, -3, "You already have record CAM{}".format(publisher))

    print("[END]")
    return web.json_response(resp)


async def on_shutdown(app):
    print("Web server is shutting down...")
    # close ws
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrecorder")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--janus", default="ws://192.168.5.12:8188", help="Janus gateway address (default: 127.0.0.1:8188)"
    )
    parser.add_argument(
        "--port", type=int, default=9002, help="Port for HTTP server (default: 9002)"
    )
    args = parser.parse_args()

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)

    app.router.add_post("/record/start", start)
    app.router.add_post("/record/stop", stop)
    app.router.add_post("/record/screen", recording_screen)
    app.router.add_post("/record/camera", switch_camera)

    ws = WebSocketClient(args.janus)
    loop = asyncio.get_event_loop()

    try:
        web.run_app(
            app, access_log=None, host=args.host, port=args.port
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping now!")


