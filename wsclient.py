from math import pi
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

from janus import JanusSession, JanusSessionStatus, PluginData, Media, RecordSessionStatus, WebrtcUp, SlowLink, HangUp, \
    Ack, RecordSession
from recorder import RecordFile, RecordSegment
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
    # {room: JanusSession}
    _sessions = {}
    # {str(room + publisher): RecordSession}
    _record_sessions = {}
    # {room: RecordFile}
    _files = {}

    async def connect(self):
        self.conn = await websockets.connect(self.server, subprotocols=['janus-protocol'])

    async def close(self):
        await self.conn.close()

    def _cur_session(self, room):
        r = int(room)
        if r in self._sessions:
            return self._sessions[r].session
        return None

    def _cur_handle(self, room):
        r = int(room)
        if r in self._sessions:
            return self._sessions[r].handle
        return None

    async def _create(self, room):
        transaction = "Create_{r}".format(r=room)
        await self.conn.send(json.dumps({
            "janus": "create",
            "room": room,
            "transaction": transaction
        }))

    async def _attach(self, room):
        transaction = "Attach_{r}".format(r=room)
        await self.conn.send(json.dumps({
            "janus": "attach",
            "session_id": self._cur_session(room),
            "plugin": "janus.plugin.videoroom",
            "transaction": transaction
        }))

    async def _sendmessage(self, body, room, jsep=None):
        print("send message, body", body)

        transaction = transaction_id()
        janus_message = {
            "janus": "message",
            "session_id": self._cur_session(room),
            "handle_id": self._cur_handle(room),
            "transaction": transaction,
            "body": body
        }
        if jsep is not None:
            janus_message["jsep"] = jsep
        await self.conn.send(json.dumps(janus_message))

    async def _keepalive(self, room):
        session = self._cur_session(room)
        handle = self._cur_handle(room)
        if session is None or handle is None:
            return

        while True:
            try:
                await asyncio.sleep(30)
                transaction = transaction_id()
                await self.conn.send(json.dumps({
                    "janus": "keepalive",
                    "session_id": self._cur_session(room),
                    "handle_id": self._cur_handle(room),
                    "transaction": transaction
                }))
            except KeyboardInterrupt:
                return

    async def _handle_pre_join(self, room, transaction, raw):
        session: JanusSession = self._sessions[int(room)]
        if transaction == "Create":
            session.session = raw["data"]["id"]
            await self._attach(int(room))
        elif transaction == "Attach":
            session.handle = raw["data"]["id"]
            # join the room
            joinmessage = {"request": "join", "ptype": "publisher", "room": int(room), "pin": str(session.pin),
                           "display": session.display, "id": RECORDER}
            await self._sendmessage(joinmessage, room=room)

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
            if janus == "success" and "transaction" in raw:
                transaction = str(raw["transaction"])
                r, _, room = transaction.partition("_")
                if len(r) > 0 and len(room) > 0:
                    await self._handle_pre_join(room=room, transaction=r, raw=raw)
            if "plugindata" in raw:
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

    async def _handle_plugin_data(self, data: PluginData):
        print("handle plugin data: \n", data)

        if data.jsep is not None:
            print("handle jesp data")
        if data.data is not None:
            events_type = data.data["videoroom"]
            if events_type == "joined":
                await self._handle_joind(data.data)
            elif events_type == "event":
                await self._handle_events(data.data)
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

    async def _handle_events(self, data):
        key = "leaving"
        if key in data:
            room = data["room"]
            publisher = data[key]
            await self._handle_leave(room, publisher)
        elif "publishers" in data:
            await self._handle_joind(data)

    async def _handle_leave(self, room, publisher):
        session = self._find_recordsession(room, publisher)
        if session is not None:
            self._stop_forwarding(session)

    async def _handle_rtp_forward(self, data):
        room = int(data["room"])
        assert room
        publisher = int(data["publisher_id"])
        assert publisher

        session: RecordSession = self._find_recordsession(room, publisher)
        if session is not None:
            if "rtp_stream" in data:
                rtsp_stream = data["rtp_stream"]
                if "audio_stream_id" in rtsp_stream:
                    session.update_forwarder(a_stream=rtsp_stream["audio_stream_id"])
                if "video_stream_id" in rtsp_stream:
                    session.update_forwarder(v_stream=rtsp_stream["video_stream_id"])
                self._launch_recorder(session)

                print("Now publisher {p} in the room {r} is forwarding".format(p=session.publisher, r=session.room))
                session.status = RecordSessionStatus.Forwarding

    async def loop(self):
        await self.connect()

        assert self.conn

        while True:
            try:
                msg = await self._recv()
                if isinstance(msg, PluginData):
                    await self._handle_plugin_data(msg)
                elif isinstance(msg, Media):
                    print(msg)
                elif isinstance(msg, WebrtcUp):
                    print(msg)
                elif isinstance(msg, SlowLink):
                    print(msg)
                elif isinstance(msg, HangUp):
                    print(msg)
                elif not isinstance(msg, Ack):
                    print(msg)
            except (KeyboardInterrupt, ConnectionClosed):
                return

    # ???????????????????????????
    def _is_forwarding(self, key):
        if key in self._record_sessions:
            return True
        return False

    # ??? publisherid = 0 ????????????????????????
    async def start_recording(self, room, pin):
        display = "record_" + str(room)

        if room in self._sessions:
            session: JanusSession = self._sessions[room]
            if session.status.value < JanusSessionStatus.Processing.value:
                print("Current recorder is in the room")
                return False

        session = JanusSession(room=room, pin=pin, display=display)
        session.status = JanusSessionStatus.Starting
        self._sessions[room] = session

        await self._create(room=room)

        loop = asyncio.get_event_loop()
        loop.create_task(self._keepalive(room=room))
        session.loop = loop

        return True

    # ???????????? publisher 
    async def _start_recording(self, room, publisher):
        if publisher not in [CAM1, CAM2, SCREEN]:
            return

        session_key = str(room) + "-" + str(publisher)
        start_time = int(time.time())
        if not self._is_forwarding(session_key):
            session = RecordSession(room=room, publisher=publisher, startedTime=start_time)
            self._record_sessions[session_key] = session
            session.status = RecordSessionStatus.Started

            # preparations: 
            self._create_folders(session)
            self._create_sdp(session)

            # ????????????
            await self._forward_rtp(session)
        else:
            print("The publisher {p} in the room {r} is forwarding".format(p=publisher, r=room))

    @staticmethod
    def _create_folders(session: RecordSession):
        print("Creating room file folder...")
        session.create_file_folder()

    @staticmethod
    def _create_sdp(session: RecordSession):
        print("Creating SDP file for ffmpeg...")
        session.create_sdp()

    # forwarding_rtp to local server
    async def _forward_rtp(self, session: RecordSession):
        forwarding_obj = session.forwarding_obj()
        forwardmessage = {"request": "rtp_forward", "secret": "adminpwd"}.copy()
        forwardmessage.update(forwarding_obj)
        await self._sendmessage(forwardmessage, room=session.room)

    def _launch_recorder(self, session: RecordSession):
        folder = session.folder
        begin_time = int(time.time())
        name = str(session.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name
        sdp = folder + session.forwarder.name

        proc = subprocess.Popen(
            ['ffmpeg', '-loglevel', 'info', '-hide_banner', '-protocol_whitelist', 'file,udp,rtp', '-i', sdp, '-c',
             'copy', file_path])
        session.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=session.publisher, r=session.room))
        session.status = RecordSessionStatus.Recording

        # ??????????????????
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher)
        if session.room not in self._files:
            file = RecordFile(room=session.room, cam=segment)
            self._files[session.room] = file
        else:
            file: RecordFile = self._files[session.room]
            if segment.is_screen:
                file.screens.append(segment)
            else:
                file.cameras.append(segment)

    # ????????????????????????
    async def stop_recording(self, room):
        if room not in self._sessions:
            return False
        await self._stop_all_sessions(room)
        self._processing_file(room)
        return True

    def _find_recordsession(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        if session_key in self._record_sessions:
            return self._record_sessions[session_key]
        return None

    async def _stop_all_sessions(self, room):
        p = [CAM1, CAM2, SCREEN]

        def l(p):
            return self._find_recordsession(room=room, publisher=p)

        sessions = list(map(l, p))
        sessions = filter(None, sessions)

        for session in sessions:
            await self._stop_session(session)

        print("Now leaving room...")
        janus_session: JanusSession = self._sessions[room]
        await self._leave_room(janus_session)

        # janus_session.loop.stop()
        # janus_session.loop.close()

    async def _leave_room(self, session: JanusSession):
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "destroy",
            "session_id": session.session,
            "transaction": transaction
        }))
        session.status = JanusSessionStatus.Stopped

    async def _stop_session(self, session: RecordSession):
        async def _stop_stream(stream):
            forwarding_obj = session.stop_forwarding_obj(stream)
            forwardmessage = {"request": "stop_rtp_forward", "secret": "adminpwd"}.copy()
            forwardmessage.update(forwarding_obj)
            await self._sendmessage(forwardmessage, room=session.room)

        if session.forwarder.audio_stream_id is not None:
            await _stop_stream(session.forwarder.audio_stream_id)
        if session.forwarder.video_stream_id is not None:
            await _stop_stream(session.forwarder.video_stream_id)

        self._stop_forwarding(session)

    def _stop_forwarding(self, session: RecordSession):
        room = session.room
        publisher = session.publisher

        os.kill(session.recorder_pid, signal.SIGINT)
        session.recorder_pid = None

        print("Now publisher {p} in the room {r} is Stopped recording".format(p=session.publisher, r=session.room))
        session.status = RecordSessionStatus.Stopped
        session.clean_ports()

        key = str(session.room) + "-" + str(session.publisher)
        self._record_sessions.pop(key, None)

        # ??????????????????
        file: RecordFile = self._files[room]
        end_time = int(time.time())
        if file is not None:
            if int(publisher) == SCREEN:
                segment: RecordSegment = file.screens[-1]
                segment.end_time = end_time
            else:
                segment: RecordSegment = file.cameras[-1]
                segment.end_time = end_time

    def _processing_file(self, room):
        print("Starting processing all the files from room = ", room)

        if room in self._files:
            file: RecordFile = self._files[room]
            if file is not None:
                file.process()

                print("\n\n\n----------TEST--------\n\n\n")
                session: JanusSession = self._sessions[room]
                session.status = JanusSessionStatus.Processing
