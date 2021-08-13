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
            join_message = {"request": "join", "ptype": "publisher", "room": int(room), "pin": str(session.pin),
                            "display": session.display, "id": RECORDER}
            await self._sendmessage(join_message, room=room)

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
                await self._handle_joined(data.data)
            elif events_type == "event":
                await self._handle_events(data.data)
            elif events_type == "rtp_forward":
                await self._handle_rtp_forward(data.data)

    async def _handle_joined(self, data):
        room = int(data["room"])
        assert room

        publishers = data["publishers"]
        print("publishes in the room: \n", publishers)
        self._update_publishers(room, publishers)
        sessions = self._active_sessions(room)
        for session in sessions:
            if session.publisher == SCREEN and session.status == RecordSessionStatus.Forwarding:
                continue
            # 开启转发
            await self._forward_rtp(session)

    async def _handle_events(self, data):
        key = "leaving"
        if key in data:
            room = data["room"]
            publisher = data[key]
            await self._handle_leave(room, publisher)
        elif "publishers" in data:
            await self._handle_joined(data)

    async def _handle_leave(self, room, publisher):
        session = self._find_record_session(room, publisher)
        if session is not None:
            self._stop_forwarding(session)

    async def _handle_rtp_forward(self, data):
        room = int(data["room"])
        assert room
        publisher = int(data["publisher_id"])
        assert publisher

        session: RecordSession = self._find_record_session(room, publisher)
        if session is not None:
            if "rtp_stream" in data:
                rtsp_stream = data["rtp_stream"]
                if "audio_stream_id" in rtsp_stream:
                    session.update_forwarder(a_stream=rtsp_stream["audio_stream_id"])
                if "video_stream_id" in rtsp_stream:
                    session.update_forwarder(v_stream=rtsp_stream["video_stream_id"])

                print("Now publisher {p} in the room {r} is forwarding".format(p=session.publisher, r=session.room))
                session.status = RecordSessionStatus.Forwarding
                # update forward publishers & record
                self._update_forwarding(room)

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
                break

    # 是否已经加入了房间
    def _is_forwarding(self, key):
        if key in self._record_sessions:
            return True
        return False

    # 开始初始录制服务
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

        return True

    def _update_publishers(self, room, publishers):
        for p in publishers:
            publisher = p["id"]
            if publisher not in [CAM1, CAM2, SCREEN]:
                continue
            session_key = str(room) + "-" + str(publisher)
            start_time = int(time.time())
            if not self._is_forwarding(session_key):
                session = RecordSession(room=room, publisher=publisher, started_time=start_time)
                self._record_sessions[session_key] = session

                # preparations:
                self._create_folders(session)
                self._create_sdp(session)

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
        forward_message = {"request": "rtp_forward", "secret": "adminpwd"}.copy()
        forward_message.update(forwarding_obj)
        await self._sendmessage(forward_message, room=session.room)

    def _update_forwarding(self, room):
        sessions = self._active_sessions(room)
        forwarding_sessions = []
        for session in sessions:
            if session.status == RecordSessionStatus.Forwarding:
                forwarding_sessions.append(session)
        if len(forwarding_sessions) == len(sessions):
            self._launch_recorder(room, forwarding_sessions)

    def _launch_recorder(self, room, sessions: [RecordSession]):
        janus: JanusSession = self._sessions[room]
        if len(sessions) > 1 and janus.recording_screen:
            screen = self._find_record_session(room=room, publisher=SCREEN)
            sessions.remove(screen)
            cam = sessions[0]
            self._record_screen_cam(screen=screen, cam=cam)
        else:
            self._record_cam(session=sessions[0])
        janus_session: JanusSession = self._sessions[room]
        if janus_session is not None:
            janus_session.status = JanusSessionStatus.Recording

    def _record_screen_cam(self, screen: RecordSession, cam: RecordSession):
        janus_session: JanusSession = self._sessions[screen.room]
        if janus_session is not None:
            janus_session.recording_screen = True

        folder = screen.folder
        begin_time = int(time.time())
        name = str(screen.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name
        sdp_screen = folder + screen.forwarder.name
        sdp_cam = folder + cam.forwarder.name

        proc = subprocess.Popen(['ffmpeg',
                                 '-protocol_whitelist', 'file,udp,rtp',
                                 '-i', sdp_screen,
                                 '-protocol_whitelist', 'file,udp,rtp',
                                 '-i', sdp_cam,
                                 '-filter_complex',
                                 '[1]scale=iw/3:ih/3[pip];[0][pip] overlay=main_w-overlay_w-20:main_h-overlay_h-20',
                                 '-codec:v', 'libx264',
                                 '-preset', 'ultrafast',
                                 '-tune', 'zerolatency',
                                 '-r', '30',
                                 '-b:v', '5M',
                                 '-crf', '18',
                                 '-codec:a', 'copy',
                                 file_path])
        screen.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=screen.publisher, r=screen.room))
        screen.status = RecordSessionStatus.Recording
        cam.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=screen.room, publisher=screen.publisher)
        if screen.room not in self._files:
            file = RecordFile(room=screen.room, file=segment)
            self._files[screen.room] = file
        else:
            file: RecordFile = self._files[screen.room]
            file.files.append(segment)

    def _record_cam(self, session: RecordSession):
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

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher)
        if session.room not in self._files:
            file = RecordFile(room=session.room, file=segment)
            self._files[session.room] = file
        else:
            file: RecordFile = self._files[session.room]
            file.files.append(segment)

    def _find_record_session(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        if session_key in self._record_sessions:
            return self._record_sessions[session_key]
        return None

    def _active_sessions(self, room):
        p = [CAM1, CAM2, SCREEN]

        def l(p):
            return self._find_record_session(room=room, publisher=p)

        sessions = list(map(l, p))
        sessions = list(filter(None, sessions))
        return sessions

    # 正在转发且录制的摄像头
    def _recording_cam(self, room):
        p = [CAM1, CAM2]

        def l(p):
            return self._find_record_session(room=room, publisher=p)

        sessions = list(map(l, p))
        sessions = filter(None, sessions)
        sessions = list(filter(lambda x: x.status == RecordSessionStatus.Recording, sessions))
        return sessions[0]

    # 结束当前房间录制
    async def stop_recording(self, room):
        if room not in self._sessions:
            return False
        await self._stop_all_sessions(room)
        self._processing_file(room)
        return True

    async def _stop_all_sessions(self, room):
        sessions = self._active_sessions(room)
        for session in sessions:
            await self._stop_session(session)

        print("Now leaving room...")
        janus_session: JanusSession = self._sessions[room]
        await self._leave_room(janus_session)

    async def _leave_room(self, session: JanusSession):
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "destroy",
            "session_id": session.session,
            "transaction": transaction
        }))
        session.status = RecordSessionStatus.Stopped

    async def _stop_session(self, session: RecordSession):
        async def _stop_stream(stream):
            forwarding_obj = session.stop_forwarding_obj(stream)
            forward_message = {"request": "stop_rtp_forward", "secret": "adminpwd"}.copy()
            forward_message.update(forwarding_obj)
            await self._sendmessage(forward_message, room=session.room)

        if session.forwarder.audio_stream_id is not None:
            await _stop_stream(session.forwarder.audio_stream_id)
        if session.forwarder.video_stream_id is not None:
            await _stop_stream(session.forwarder.video_stream_id)

        self._stop_forwarding(session)

    # 停止录制/转发(清理所占端口)
    def _stop_forwarding(self, session_p: RecordSession):
        session: RecordSession = session_p
        room = session.room
        publisher = session.publisher
        janus: JanusSession = self._sessions[room]

        if publisher == SCREEN:
            screen = self._find_record_session(room, SCREEN)
            if screen is not None:
                self._stop_recording_session(screen, True)
        else:
            if janus.recording_screen:
                screen = self._find_record_session(room, SCREEN)
                if screen is not None:
                    self._stop_recording_session(screen, False)
            # 按需移除Cam
            self._stop_recording_session(session, True)

    def _stop_recording_session(self, session: RecordSession, remove=True):
        room = session.room
        if session.recorder_pid is not None:
            os.kill(session.recorder_pid, signal.SIGINT)
            session.recorder_pid = None

        print("Now publisher {p} in the room {r} is Stopped recording".format(p=session.publisher, r=session.room))

        if remove:
            session.status = RecordSessionStatus.Stopped
            key = str(session.room) + "-" + str(session.publisher)
            self._record_sessions.pop(key, None)
            session.clean_ports()
        else:
            session.status = RecordSessionStatus.Forwarding

        # 更新文件信息
        file: RecordFile = self._files[room]
        end_time = int(time.time())
        if file is not None:
            segment: RecordSegment = file.files[-1]
            segment.end_time = end_time

    def _stop_recording_cam(self, session: RecordSession):
        room = session.room
        if session.recorder_pid is not None:
            os.kill(session.recorder_pid, signal.SIGINT)
            session.recorder_pid = None

        print("Now publisher {p} in the room {r} is Stopped recording".format(p=session.publisher, r=session.room))

        session.status = RecordSessionStatus.Stopped
        key = str(session.room) + "-" + str(session.publisher)
        self._record_sessions.pop(key, None)
        session.clean_ports()

        # 更新文件信息
        file: RecordFile = self._files[room]
        end_time = int(time.time())
        if file is not None:
            segment: RecordSegment = file.files[-1]
            segment.end_time = end_time

    # 开始录制屏幕
    async def start_recording_screen(self, room):
        janus_session: JanusSession = self._sessions[room]
        if janus_session is None:
            return False
        else:
            if janus_session.status == JanusSessionStatus.Default:
                return False

        screen = self._find_record_session(room, SCREEN)
        if screen is not None:
            if screen.status != RecordSessionStatus.Recording:
                cam = self._recording_cam(room)
                self._stop_recording_session(cam, False)
                self._record_screen_cam(screen, cam)
                return True

        publisher = SCREEN
        session_key = str(room) + "-" + str(publisher)
        start_time = int(time.time())
        session = RecordSession(room=room, publisher=publisher, started_time=start_time)
        self._record_sessions[session_key] = session
        session.status = RecordSessionStatus.Started

        # preparations:
        self._create_folders(session)
        self._create_sdp(session)

        cam = self._recording_cam(room)
        self._stop_recording_session(cam, False)
        self._record_screen_cam(session, cam)

        return True

    # 结束录制屏幕
    async def stop_recording_screen(self, room):
        if room not in self._sessions:
            return False
        janus_session: JanusSession = self._sessions[room]
        if janus_session is None:
            return False
        if not janus_session.recording_screen:
            return False
        screen = self._find_record_session(room, SCREEN)
        if screen is None:
            return False
        # 结束录屏但继续录制摄像头
        self._stop_recording_session(screen, False)
        cam = self._recording_cam(room)
        self._record_cam(cam)

        janus_session: JanusSession = self._sessions[screen.room]
        if janus_session is not None:
            janus_session.recording_screen = False

    def _processing_file(self, room):
        print("Starting processing all the files from room = ", room)

        if room in self._files:
            file: RecordFile = self._files[room]
            if file is not None:
                file.process()

                print("\n\n\n----------TEST--------\n\n\n")
                session: JanusSession = self._sessions[room]
                session.status = JanusSessionStatus.Processing
