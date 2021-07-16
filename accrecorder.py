#!/usr/bin/python
import os
import argparse
import subprocess
import signal
import json
import asyncio

from os import curdir, sep
from collections import namedtuple
from aiohttp import web
from janusgateway import janus

ROOT = os.path.dirname(__file__)
                      
# common response   
def json_response(success, code, data):
    dict = {"success": success, "code": code, "data": data}
    return json.dumps(dict, indent = 4).encode(encoding='utf_8')

# index
async def index(request):
    content = "Recording the conference!".encode()
    return web.Response(content_type="text/html", text=content)

# check start command 
async def start(request):
    params = await request.json()
    resp = json_response(False, 0, "default response")

    room = params["room"]
    if room.isdigit() == False:
        resp = json_response(False, -1, "Please input correct Room number!")

    resp = json_response(True, 0, "Start recording...")

    return web.Response(
        content_type = "application/json",
        text = resp,
    )

# check stop command
async def stop(request):
    params = await request.json()
    resp = json_response(False, 0, "default response")

    room = params["room"]
    if room.isdigit() == False:
        resp = json_response(False, -1, "Please input correct Room number!")

    resp = json_response(True, 0, "Stop recording")

    return web.Response(
        content_type = "application/json",
        text = resp,
    )

async def on_shutdown(app):
    print("Web server is shutting down...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrecorder")
    parser.add_argument(
        "--janus", default="ws://127.0.0.1:8188", help="Janus gateway address (default: 127.0.0.1:8188)"
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

    web.run_app(app, host='127.0.0.1', port=args.port)
