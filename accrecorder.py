#!/usr/bin/python
import os
import argparse
import json
import asyncio

from os import curdir, sep
from collections import namedtuple
from aiohttp import web
from wsclient import WebSocketClient

ROOT = os.path.dirname(__file__)
ws: WebSocketClient = None
                      
# common response   
def json_response(success, code, data):
    return {"success": success, "code": code, "data": data}
    # return json.dumps(dict, indent = 4).encode(encoding='utf_8')

# index
async def index(request):
    content = "Recording the conference!".encode()
    return web.Response(content_type="text/html", text=content)

# check start command 
async def start(request):
    form = await request.post()
    print(u"[START]:Incoming Request: {r}, form: {f}".format(r=request, f=form))

    resp = json_response(False, 0, "default response")

    room = form["room"]
    if room.isdigit() == False:
        resp = json_response(False, -1, "Please input correct Room number!")

    # publisher = form["publisher"]
    # if room.isdigit() == False:
    #     resp = json_response(False, -2, "Please input correct publisher identifier!")

    success = await ws.start_recording(int(room), form["pin"])
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
    if room.isdigit() == False:
        resp = json_response(False, -1, "Please input correct Room number!")

    # publisher = form["publisher"]
    # if room.isdigit() == False:
    #     resp = json_response(False, -2, "Please input correct publisher identifier!")

    success = await ws.stop_recording(int(room))
    if success:
        resp = json_response(True, 0, "Stop recording")
    else:
        resp = json_response(False, -3, "Current publisher {p} is not recording".format(p=publisher))        

    print("[END]")
    return web.json_response(resp)

async def on_shutdown(app):
    print("Web server is shutting down...")
    # close ws
    loop.run_until_complete(ws.close())

    loop.stop()
    loop.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrecorder")
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

    ws = WebSocketClient(args.janus)
    loop = asyncio.get_event_loop()
    
    try:
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, host="127.0.0.1", port=args.port)    
        loop.run_until_complete(site.start())
        loop.run_until_complete(ws.loop())
        
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping now!")
        loop.run_until_complete(ws.close())
        

