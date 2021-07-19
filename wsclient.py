from typing import AnyStr, Set
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

from janus import Forwarding, JanusEvent, PluginData, Media, RecordFile, RecordSegment, SessionStatus, WebrtcUp, SlowLink, HangUp, Ack, JanusSession
from websockets.exceptions import ConnectionClosed

# Random Transaction ID        
def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))

# static publisher IDs
CAM1 = 1
CAM2 = 2
SCREEN = 9
RECORDER = 911

STOP_RECORDING = -99

@attr.s
class WebSocketClient:
    server = attr.ib(validator=attr.validators.instance_of(str))
    _messages = attr.ib(factory=set)
    _joined = False
    # {str(room + publisher): JanusSession}
    _sessions = {}
    # {room: RecordFile}
    _files = {}

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

        if janus == "event" or janus == "success":
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

    async def _handle_plugin_data(self, data:PluginData):
        print("handle plugin data: \n", data)

        if data.jsep is not None:
            print("handle jesp data")
        if data.data is not None:
            events_type = data.data["videoroom"]
            if events_type == "joined":
                await self._handle_joind(data.data)
            elif events_type == "event" :
                await self._hanlde_events(data.data)
            elif events_type == "rtp_forward":
                await self._handle_rtp_forward(data.data)

    async def _handle_joind(self, data):
        room = int(data["room"])
        assert room

        publishers = data["publishers"]
        print("new publishes in the room: \n")

        for publisher in publishers:
            id = publisher["id"]
            assert id

            print("id: %(id)s, display: %(display)s" % publisher)
            await self._start_recording(room=room, publisher=id)

    async def _hanlde_events(self, data):
        key = "leaving"
        if key in data:
            room = data["room"]
            publisher = data[key]
            await self._handle_leave(room, publisher)

    async def _handle_leave(self, room, publisher):
        session = self._findsession(room, publisher)
        if session is not None:
            self._stop_forwarding(session)

    async def _handle_rtp_forward(self, data):
        room = int(data["room"])
        assert room
        publisher = int(data["publisher_id"])
        assert publisher

        session:JanusSession = self._findsession(room, publisher)
        if session is not None:
            if data["rtp_stream"]["audio_stream_id"] is not None:
                session.update_forwarder(a_stream=data["rtp_stream"]["audio_stream_id"])
            if data["rtp_stream"]["video_stream_id"] is not None:
                session.update_forwarder(v_stream=data["rtp_stream"]["video_stream_id"])
            self._launch_recorder(session)

            print("Now publisher {p} in the room {r} is forwarding".format(p=session.publisher, r=session.room))
            session.status = SessionStatus.Forwarding

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

    # 当 publisherid = 0 开始初始录制服务
    async def start_recording(self, room, pin, publisherid):
        display = "record_" + str(room)

        if self._joined == False:
            joinmessage = { "request": "join", "ptype": "publisher", "room": room, "pin": str(pin), "display": display, "id": RECORDER }
            await self._sendmessage(joinmessage)
            self._joined = True
            return True
        else:
            print("Current recorder is in the room")
            return False

    # 录制某个 publisher 
    async def _start_recording(self, room, publisher):
        if publisher not in [CAM1, CAM2, SCREEN]:
            return

        session_key = str(room) + "-" + str(publisher)
        start_time = time.time()
        if self._is_forwarding(session_key) == False:
            session = JanusSession(room=room, publisher=publisher, startedTime=start_time)
            self._sessions[session_key] = session
            session.status = SessionStatus.Started

            # preparations: 
            self._create_folders(session)
            self._create_sdp(session)

            # 开启转发
            await self._forward_rtp(session)
        else:
           print("The publisher {p} in the room {r} is forwarding".format(p=publisher, r=room))

    def _create_folders(self, session: JanusSession):
        print("Creating room file folder...")
        session.create_file_folder()

    def _create_sdp(self, session: JanusSession):
        print("Creating SDP file for ffmpeg...")
        session.create_sdp()

    # forwarding_rtp to local server
    async def _forward_rtp(self, session: JanusSession):
        forwarding_obj = session.forwarding_obj()
        forwardmessage = { "request": "rtp_forward", "secret": "adminpwd" }.copy()
        forwardmessage.update(forwarding_obj)
        await self._sendmessage(forwardmessage)

    def _launch_recorder(self, session: JanusSession):
        folder = session.folder
        begin_time = time.time()
        name = str(session.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name
        sdp = folder + session.forwarder.name

        proc = subprocess.Popen(['ffmpeg', '-loglevel', 'info', '-hide_banner', '-protocol_whitelist', 'file,udp,rtp', '-i', sdp, '-c:v', 'copy', '-c:a', 'copy', file_path])
        session.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=session.publisher, r=session.room))
        session.status = SessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher)
        file = RecordFile(room=session.room, cam=segment)
        self._files[session.room] = file
        
    # 结束当前 publisher 的录制
    # 如果 publisherid = -99 则为停止所有的录制(下课)
    async def stop_recording(self, room, publisherid):
        session = self._findsession(room, publisherid)

        publisher = int(publisherid)
        if publisher == STOP_RECORDING:
            await self._stop_all_session(room)
            self._processing_file(room)
            return True
        else:
            if session is not None:
                self._stop_session(session)
                return True
            else:
                print("The publisher {p} in the room {r} is NOT publishing".format(p=session.publisher, r=session.room))
                return False

    def _findsession(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        if session_key in self._sessions:
            return self._sessions[session_key]
        return None
    
    async def _stop_all_session(self, room):
        p = [CAM1, CAM2, SCREEN]
        def l(p):
            return self._findsession(room=room, publisher=p)
         
        sessions = list(map(l, p))
        sessions = filter(None, sessions)
        
        for session in sessions:
            await self._stop_session(session)

    async def _stop_session(self, session: JanusSession):
        async def _stop_stream(stream):
            forwarding_obj = session.stop_forwarding_obj(stream)
            forwardmessage = { "request": "stop_rtp_forward", "secret": "adminpwd" }.copy()
            forwardmessage.update(forwarding_obj)
            await self._sendmessage(forwardmessage)  
        
        if session.forwarder.audio_stream_id is not None:
            await _stop_stream(session.forwarder.audio_stream_id)
        if session.forwarder.video_stream_id is not None:
            await _stop_stream(session.forwarder.video_stream_id)

        self._stop_forwarding(session)

    def _stop_forwarding(self, session: JanusSession):
        room = session.room
        publisher = session.publisher

        os.kill(session.recorder_pid, signal.SIGINT)
        session.recorder_pid = None

        print("Now publisher {p} in the room {r} is Stopped recording".format(p=session.publisher, r=session.room))
        session.status = SessionStatus.Stopped

        key = str(session.room) + "-" + str(session.publisher)
        self._sessions.pop(key, None)

        # 更新文件信息
        file: RecordFile = self._files[room]
        end_time = time.time()
        if file is not None:
            if int(publisher) == SCREEN:
                segment: RecordSegment = file.screens[-1]
                segment.end_time = end_time
            else:
                segment: RecordSegment = file.cameras[-1]
                segment.end_time = end_time
        
    def _processing_file(self, room):
        print("Starting processing all the files from room = ", room)
        file: RecordFile = self._files[room]
        if file is not None:
            self._join_cameras(file)
    
    # 将所有的摄像头文件拼接
    def _join_cameras(self, file:RecordFile):
        print("Starting join all the camera files")
    
    # 将合并的摄像头文件根据屏幕文件进行分段
    def _separate_camera_files(self, cam_files, screen_files):
        print("Starting separating all the camera files from screen files")

    # [PiP]形式融合屏幕和摄像头画面
    def _merge(self, file:RecordFile):
        print("Starting merge all the camera files")