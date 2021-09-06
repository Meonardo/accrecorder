#!/usr/bin/python
import os
import argparse

from aiohttp import web
from httpclient import HTTPClient, print

ROOT = os.path.dirname(__file__)
client = HTTPClient()
RTP_FORWARD_HOST = ''


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


# index
async def index(request):
    content = "Recording the conference!"
    return web.Response(content_type="text/html", text=content)


# Configure record server per room
async def configure(request):
    form = await request.post()
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -3, "Please input correct Room number!")

    if 'upload_server' not in form:
        return json_response(False, -2, "Please input upload server address!")

    if 'class_id' not in form:
        return json_response(False, -4, "Please input class_id!")

    if 'janus' not in form:
        return json_response(False, -4, "Please input Janus HTTP transport address, for example: http://192.168.5.12:8088")
    janus = str(form['janus'])
    if not janus.startswith("http"):
        return json_response(False, -5,
                             "Please input correct Janus HTTP transport address, for example: http://192.168.5.12:8088")

    class_id = form['class_id']
    upload_server = form['upload_server']
    success = await client.configure(room, class_id, str(room), upload_server, RTP_FORWARD_HOST, janus)
    if success:
        return json_response(True, 0, "Room {} is configured".format(room))
    else:
        return json_response(False, -6, "Current room {} already configured".format(room))


# Reset record session in case of client had unexceptional satiation
async def reset(request):
    form = await request.post()
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

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
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    if 'publisher' not in form:
        return json_response(False, -1, "Please input publisher ids to record!")
    publisher = str(form["publisher"])
    list_str = publisher.split(',')
    for p in list_str:
        if not p.isdigit():
            return json_response(False, -2, "Please input correct publisher identifier!")

    success = await client.start_recording(room, list_str, RTP_FORWARD_HOST)
    if success:
        return json_response(True, 0, "Start recording at room {}".format(room))
    else:
        return json_response(False, -3, "Current room {r} is recording".format(r=room))


# check stop command
async def stop(request):
    form = await request.post()
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

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
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

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
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

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
    print(u"[START]\n:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    if 'publisher' not in form:
        return json_response(False, -2, "Please input publisher id!")
    publisher = form["publisher"]
    if not publisher.isdigit():
        return json_response(False, -2, "Please input correct publisher identifier!")

    success = await client.switch_camera(room, int(publisher))
    if success:
        return json_response(True, 0, "Switch to CAM{}".format(publisher))
    else:
        return json_response(False, -3, "You already have record CAM{}".format(publisher))


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
        "--f", default="192.168.5.48", help="Janus RTP forwarding host"
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

    RTP_FORWARD_HOST = args.f

    try:
        print("Starting web server...")
        print("Janus RTP forwarding address is ", args.f)
        web.run_app(
            app, access_log=None, host=args.host, port=args.port
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("Web server stopped.")


