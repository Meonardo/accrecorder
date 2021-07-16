from typing import Set
import websockets
import attr
import asyncio
import json
import random
import string
import time
import subprocess
import signal
import os

from janus import PluginData, Media, SessionStatus, WebrtcUp, SlowLink, HangUp, Ack, JanusSession
from websockets.exceptions import ConnectionClosed

# Random Transaction ID        
def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))

@attr.s
class WebSocketClient:
    server = attr.ib(validator=attr.validators.instance_of(str))
    _messages = attr.ib(factory=set)
    _joined = False
    _sessions = {}

    async def connect(self):
        self.conn = await websockets.connect(self.server, subprotocols=['janus-protocol'])
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "create",
            "transaction": transaction
            }))
        resp = await self.conn.recv()
        print (resp)
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed creating session"
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.session = parsed["data"]["id"]

    async def close(self):
        await self.conn.close()

    async def _attach(self, plugin):
        assert hasattr(self, "session"), "Must connect before attaching to plugin"
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "attach",
            "session_id": self.session,
            "plugin": plugin,
            "transaction": transaction
        }))
        resp = await self.conn.recv()
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed attaching to {}".format(plugin)
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.handle = parsed["data"]["id"]
    
    async def _sendmessage(self, body, jsep=None):
        print("send message, body", body)

        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"
        transaction = transaction_id()
        janus_message = {
            "janus": "message",
            "session_id": self.session,
            "handle_id": self.handle,
            "transaction": transaction,
            "body": body
        }
        if jsep is not None:
            janus_message["jsep"] = jsep
        await self.conn.send(json.dumps(janus_message))

    async def _keepalive(self):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"

        while True:
            try:
                await asyncio.sleep(30)
                transaction = transaction_id()
                await self.conn.send(json.dumps({
                    "janus": "keepalive",
                    "session_id": self.session,
                    "handle_id": self.handle,
                    "transaction": transaction
                }))
            except KeyboardInterrupt:
                return

    async def _recv(self):
        if len(self._messages) > 0:
            return self._messages.pop()
        else:
            return await self._recv_and_parse()

    async def _recv_and_parse(self):
        raw = json.loads(await self.conn.recv())
        print("Received: ", raw)
        janus = raw["janus"]

        if janus == "event":
            return PluginData(
                sender=raw["sender"],
                plugin=raw["plugindata"]["plugin"],
                data=raw["plugindata"]["data"],
                jsep=raw["jsep"] if "jsep" in raw else None
            )
        elif janus == "webrtcup":
            return WebrtcUp(
                sender=raw["sender"]
            )
        elif janus == "media":
            return Media(
                sender=raw["sender"],
                receiving=raw["receiving"],
                kind=raw["type"]
            )
        elif janus == "slowlink":
            return SlowLink(
                sender=raw["sender"],
                uplink=raw["uplink"],
                lost=raw["lost"]
            )
        elif janus == "hangup":
            return HangUp(
                sender=raw["sender"],
                reason=raw["reason"]
            )
        elif janus == "ack":
            return Ack(
                transaction=raw["transaction"]
            )
        else:
            return raw

    async def _handle_plugin_data(self, data):
        print("handle plugin data: \n", data)

        if data.jsep is not None:
            print("handle jesp data")
        if data.data is not None:
            events_type = data.data["videoroom"]
            if events_type == "joined":
                publishers = data.data["publishers"]
                print("Publishes in the room: \n")
                for publisher in publishers:
                    print("id: %(id)s, display: %(display)s" % publisher)

    async def loop(self):
        await self.connect()
        await self._attach("janus.plugin.videoroom")

        loop = asyncio.get_event_loop()
        loop.create_task(self._keepalive())

        assert self.conn

        while True:
            try:
                msg = await self._recv()
                if isinstance(msg, PluginData):
                    await self._handle_plugin_data(msg)
                elif isinstance(msg, Media):
                    print (msg)
                elif isinstance(msg, WebrtcUp):
                    print (msg)
                elif isinstance(msg, SlowLink):
                    print (msg)
                elif isinstance(msg, HangUp):
                    print (msg)
                elif not isinstance(msg, Ack):
                    print(msg)
            except (KeyboardInterrupt, ConnectionClosed):
                return

        return 0

    # 是否已经加入了房间
    def _is_forwarding(self, key):
        if key in self._sessions: 
            return True
        return False

    # 开始录制
    async def startrecording(self, room, pin, publisherid):
        display = "record_" + str(room)
        start_time = time.time()

        session = JanusSession(room=room, pin=pin, display=display, publisher=publisherid, startedTime=start_time)
        session_key = str(room) + "-" + publisherid

        if self._joined == False:
            joinmessage = { "request": "join", "ptype": "publisher", "room": room, "pin": str(room), "display": display, "id": 911 }
            await self._sendmessage(joinmessage)
            self._joined = True
        else:
            print("Current recorder is int the room")

        if self._is_forwarding(session_key) == False:
            self._sessions[session_key] = session
            session.status = SessionStatus.Started

            # preparations: 
            self._create_folders(session)
            self._create_sdp(session)

            # 开启转发
            await self._forward_rtp(session)
            return True
        else:
           print("The publisher {p} in the room {r} is forwarding".format(p=session.publisher, r=session.room))
           return False

    def _create_folders(self, session:JanusSession):
        print("Creating room file folder...")
        session.create_file_folder()

    def _create_sdp(self, session:JanusSession):
        print("Creating SDP file for ffmpeg...")
        session.create_sdp()

    # forwarding_rtp to local server
    async def _forward_rtp(self, session:JanusSession):
        forwarding_obj = session.forwarding_obj()
        forwardmessage = { "request": "rtp_forward", "secret": "adminpwd" }.copy()
        forwardmessage.update(forwarding_obj)
        await self._sendmessage(forwardmessage)

        self._launchrecorder(session)

        print("Now publisher {p} in the room {r} is forwarding".format(p=session.publisher, r=session.room))
        session.status = SessionStatus.Forwarding

    def _launchrecorder(self, session:JanusSession):
        folder = session.folder
        file_path = folder + str(session.publisher) + ".ts"
        sdp = folder + session.forwarder.name
        proc = subprocess.Popen(['ffmpeg', '-loglevel', 'debug', '-hide_banner', '-protocol_whitelist', 'file,udp,rtp', '-i', sdp, '-c:v', 'copy', '-c:a', 'copy', file_path])

        session.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=session.publisher, r=session.room))
        session.status = SessionStatus.Recording

    # 结束当前 publisher 的录制
    async def stoprecording(self, room, publisherid):
        session = self.session(room, publisherid)

        if session is not None:
            forwarding_obj = session.forwarding_obj()
            forwardmessage = { "request": "stop_rtp_forward", "secret": "adminpwd" }.update(forwarding_obj)
            await self._sendmessage(forwardmessage)

            os.kill(session.recorder_pid, signal.SIGINT)
            session.recorder_pid = None

            print("Now publisher {p} in the room {r} is Stopped recording".format(p=session.publisher, r=session.room))
            session.status = SessionStatus.Stopped

            return True
        else:
           print("The publisher {p} in the room {r} is NOT publishing".format(p=session.publisher, r=session.room))
           return False

    def session(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        return self._sessions[session_key]