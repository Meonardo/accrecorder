
import platform
import random
import string
import time
import subprocess
import signal
import os
import aiohttp
from aiohttp import ClientSession

from janus import JanusSession, JanusSessionStatus, RecordSession, RecordSessionStatus
from recorder import RecordFile, RecordSegment

# janus HTTP server
JANUS_HOST = 'http://192.168.5.12:8088/janus'
# static publisher IDs
CAM1 = 1
CAM2 = 2
SCREEN = 9
RECORDER = 911


# Random Transaction ID
def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


class HTTPClient:
    def __init__(self):
        # {room: JanusSession}
        self.__sessions = {}
        # {str(room + publisher): RecordSession}
        self.__record_sessions = {}
        # {room: RecordFile}
        self.__files = {}
        self.http_session: ClientSession = aiohttp.ClientSession()

    async def close(self):
        await self.http_session.close()

    async def configure(self, room, class_id, cloud_class_id, upload_server):
        success = await self.start_forwarding(room, [CAM1, CAM2, SCREEN])
        if success:
            janus: JanusSession = self.__sessions[room]
            janus.cloud_class_id = cloud_class_id
            janus.class_id = class_id
            janus.upload_server = upload_server
        return success

    # region Forwarding
    async def start_forwarding(self, room, publishers):
        session_id = await self.__login_janus()
        if session_id is not None:
            handle_id = await self.__fetch_janus_handle(session_id)
            self.__create_janus_session(room)
            for p in publishers:
                session: RecordSession = self.__create_record_session(room, p)
                if session.status != RecordSessionStatus.Default:
                    continue
                # 创建sdp文件和文件路径
                session.create_file_folder()
                session.create_sdp()
                # 发送请求
                obj = await self.__forwarding_rtp(session_id, handle_id, session)
                if obj is not None and 'janus' in obj:
                    janus = obj['janus']
                    if janus == 'success':
                        self.__update_session_status(session, obj)
                else:
                    return False
            return True
        else:
            return False

    async def __login_janus(self):
        payload = {"janus": "create", "transaction": "Create"}
        async with self.http_session.post(JANUS_HOST, json=payload) as response:
            r = await response.json()
            if 'data' in r:
                return r['data']['id']

    async def __fetch_janus_handle(self, session_id):
        payload = {"janus": "attach", "plugin": "janus.plugin.videoroom", "transaction": "Attach"}
        path = JANUS_HOST + "/" + str(session_id)
        async with self.http_session.post(path, json=payload) as response:
            r = await response.json()
            if 'data' in r:
                return r['data']['id']

    async def __forwarding_rtp(self, session_id, handle_id, session: RecordSession):
        forwarding_obj = session.forwarding_obj()
        forward_message = {"request": "rtp_forward", "secret": "adminpwd"}.copy()
        forward_message.update(forwarding_obj)
        payload = self.__janus_message(forward_message, session_id, handle_id)
        path = JANUS_HOST + "/" + str(session_id)
        async with self.http_session.post(path, json=payload) as response:
            return await response.json()

    @staticmethod
    def __janus_message(body, session_id, handle_id):
        print("send message, body", body)

        transaction = transaction_id()
        janus_message = {
            "janus": "message",
            "session_id": int(session_id),
            "handle_id": int(handle_id),
            "transaction": transaction,
            "body": body
        }
        return janus_message

    def __create_janus_session(self, room):
        if room in self.__sessions:
            session: JanusSession = self.__sessions[room]
            if session.status.value < JanusSessionStatus.Processing.value:
                print("Current recorder is in the room")
                return

        session = JanusSession(room=room, pin=str(room), display='display')
        session.status = JanusSessionStatus.Starting
        self.__sessions[room] = session

    def __create_record_session(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        start_time = int(time.time())
        if session_key not in self.__record_sessions:
            session = RecordSession(room=room, publisher=publisher, started_time=start_time)
            self.__record_sessions[session_key] = session
            return session
        return self.__record_sessions[session_key]

    @staticmethod
    def __update_session_status(session: RecordSession, resp):
        if 'plugindata' in resp:
            plugin_data = resp['plugindata']
            if 'data' in plugin_data:
                data = plugin_data['data']
                if "rtp_stream" in data:
                    rtsp_stream = data["rtp_stream"]
                    if "audio_stream_id" in rtsp_stream:
                        session.update_forwarder(a_stream=rtsp_stream["audio_stream_id"])
                    if "video_stream_id" in rtsp_stream:
                        session.update_forwarder(v_stream=rtsp_stream["video_stream_id"])
                    session.status = RecordSessionStatus.Forwarding

    # endregion

    # region Recording

    def __find_record_session(self, room, publisher):
        session_key = str(room) + "-" + str(publisher)
        if session_key in self.__record_sessions:
            return self.__record_sessions[session_key]
        return None

    def __active_sessions(self, room, status=None):
        p = [CAM1, CAM2, SCREEN]

        def l(p):
            return self.__find_record_session(room=room, publisher=p)

        sessions = list(map(l, p))
        sessions = list(filter(None, sessions))
        if status is not None:
            sessions = list(filter(lambda x: x.status == status, sessions))
            return sessions
        return sessions

    # 正在转发且录制的摄像头
    def __recording_cam(self, room):
        p = [CAM1, CAM2]

        def l(p):
            return self.__find_record_session(room=room, publisher=p)

        sessions = list(map(l, p))
        sessions = filter(None, sessions)
        sessions = list(filter(lambda x: x.status == RecordSessionStatus.Recording, sessions))
        return sessions[0]

    # 开始录制视频
    async def start_recording(self, room, publishers):
        if room not in self.__sessions:
            return False
        if len(publishers) > 2:
            return False
        forwarding_sessions = self.__active_sessions(room, RecordSessionStatus.Forwarding)
        if len(forwarding_sessions) == 0:
            # 未进行转发的开启转发
            success = await self.start_forwarding(room, [CAM1, CAM2, SCREEN])
            if success:
                forwarding_sessions = self.__active_sessions(room, RecordSessionStatus.Forwarding)
            else:
                return False

        available = []
        for p in publishers:
            for p1 in forwarding_sessions:
                if str(p) == str(p1.publisher):
                    available.append(p1)

        self.__launch_recorder(room, available)

        return True

    def __launch_recorder(self, room, sessions: [RecordSession]):
        janus: JanusSession = self.__sessions[room]
        if len(sessions) > 1 and janus.recording_screen:
            screen = self.__find_record_session(room=room, publisher=SCREEN)
            sessions.remove(screen)
            cam = sessions[0]
            self.__record_screen_cam(screen=screen, cam=cam)
        else:
            self.__record_cam(session=sessions[0])
        janus_session: JanusSession = self.__sessions[room]
        if janus_session is not None:
            janus_session.status = JanusSessionStatus.Recording

    def __record_screen_cam(self, screen: RecordSession, cam: RecordSession):
        janus_session: JanusSession = self.__sessions[screen.room]
        if janus_session is not None:
            janus_session.recording_screen = True

        folder = screen.folder
        begin_time = int(time.time())
        name = str(screen.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name
        sdp_screen = folder + screen.forwarder.name
        sdp_cam = folder + cam.forwarder.name

        if platform.system() == "Darwin":
            encoder = "h264_videotoolbox"
        elif platform.system() == "Linux":
            encoder = "h264_nvenc"
        else:
            encoder = "h264_qsv"

        proc = subprocess.Popen(['ffmpeg',
                                 '-vsync', '1',
                                 '-protocol_whitelist', 'file,udp,rtp',
                                 '-hwaccel', 'cuda',
                                 '-hwaccel_output_format', 'cuda',
                                 '-i', sdp_screen,
                                 '-protocol_whitelist', 'file,udp,rtp',
                                 '-hwaccel', 'cuda',
                                 '-hwaccel_output_format', 'cuda',
                                 '-i', sdp_cam,
                                 '-filter_complex',
                                 '[1]scale=iw/3:ih/3[pip];[0][pip] overlay=main_w-overlay_w-20:main_h-overlay_h-20',
                                 '-codec:v', encoder,
                                 '-preset', 'p2',
                                 '-tune', 'll',
                                 '-r', '30',
                                 '-b:v', '4M',
                                 '-bufsize', '1M',
                                 '-maxrate', '5M',
                                 # '-crf', '18',
                                 '-codec:a', 'copy',
                                 file_path])
        screen.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=screen.publisher, r=screen.room))
        screen.status = RecordSessionStatus.Recording
        cam.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=screen.room, publisher=screen.publisher)
        if screen.room not in self.__files:
            file = RecordFile(room=screen.room, file=segment)
            self.__files[screen.room] = file
        else:
            file: RecordFile = self.__files[screen.room]
            file.files.append(segment)

    def __record_cam(self, session: RecordSession):
        folder = session.folder
        begin_time = int(time.time())
        name = str(session.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name
        sdp = folder + session.forwarder.name

        proc = subprocess.Popen(
            ['ffmpeg', '-protocol_whitelist', 'file,udp,rtp', '-use_wallclock_as_timestamps', '1', '-i', sdp, '-c',
             'copy', file_path])
        session.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording".format(p=session.publisher, r=session.room))
        session.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher)
        if session.room not in self.__files:
            file = RecordFile(room=session.room, file=segment)
            self.__files[session.room] = file
        else:
            file: RecordFile = self.__files[session.room]
            file.files.append(segment)

    # 切换摄像头
    async def switch_camera(self, room, publisher):
        if room not in self.__sessions:
            return False
        if publisher not in [CAM1, CAM2]:
            return False
        cam: RecordSession = self.__find_record_session(room, publisher)
        if cam.status == RecordSessionStatus.Recording:
            return False
        screen = self.__find_record_session(room, SCREEN)
        if screen is None:
            return False
        janus: JanusSession = self.__sessions[room]
        if janus.recording_screen:
            self.__stop_recording_session(screen, False)
            recording_cam = self.__recording_cam(room)
            recording_cam.status = RecordSessionStatus.Forwarding
            self.__record_screen_cam(screen, cam)
        else:
            recording_cam = self.__recording_cam(room)
            self.__stop_recording_session(recording_cam, False)
            recording_cam.status = RecordSessionStatus.Forwarding
            self.__record_cam(cam)
        return True

    # 开始录制屏幕
    async def start_recording_screen(self, room):
        janus_session: JanusSession = self.__sessions[room]
        if janus_session is None:
            return False
        else:
            if janus_session.status == JanusSessionStatus.Default:
                return False

        screen: RecordSession = self.__find_record_session(room, SCREEN)
        if screen.status == RecordSessionStatus.Recording:
            return False

        cam = self.__recording_cam(room)
        self.__stop_recording_session(cam, False)
        self.__record_screen_cam(screen, cam)

        return True

    # 结束录制屏幕
    async def stop_recording_screen(self, room):
        if room not in self.__sessions:
            return False
        janus_session: JanusSession = self.__sessions[room]
        if janus_session is None:
            return False
        if not janus_session.recording_screen:
            return False
        screen = self.__find_record_session(room, SCREEN)
        if screen is None:
            return False
        # 结束录屏但继续录制摄像头
        self.__stop_recording_session(screen, False)
        cam = self.__recording_cam(room)
        self.__record_cam(cam)

        janus_session: JanusSession = self.__sessions[screen.room]
        if janus_session is not None:
            janus_session.recording_screen = False

        return True

    def __stop_recording_session(self, session: RecordSession, remove=True):
        room = session.room
        if session.recorder_pid is not None:
            os.kill(session.recorder_pid, signal.SIGINT)
            session.recorder_pid = None

        if remove:
            session.status = RecordSessionStatus.Stopped
            key = str(session.room) + "-" + str(session.publisher)
            self.__record_sessions.pop(key, None)
            session.clean_ports()
        else:
            session.status = RecordSessionStatus.Forwarding

        # 更新文件信息
        file: RecordFile = self.__files[room]
        end_time = int(time.time())
        if file is not None:
            segment: RecordSegment = file.files[-1]
            segment.end_time = end_time

    # 停止录制
    async def stop_recording(self, room):
        if room not in self.__sessions:
            return False
        sessions = self.__active_sessions(room, RecordSessionStatus.Recording)
        for s in sessions:
            self.__stop_recording_session(s)
        if len(sessions) == 0:
            return False
        await self.__processing_file(room)

        # 清理资源
        self.__sessions.pop(room, None)
        self.__files.pop(room, None)

        return True

    async def __processing_file(self, room):
        print("Starting processing all the files from room = ", room)

        if room in self.__files:
            file: RecordFile = self.__files[room]
            session: JanusSession = self.__sessions[room]
            if file is not None:
                print("\n\n\n----------PROCESSING--------\n\n\n")
                session.status = JanusSessionStatus.Processing
                file.process()
                # session.status = JanusSessionStatus.Uploading
                # resp = await file.upload(session, self.http_session)
                # if resp is not None:
                #     print(resp)
                #     session.status = JanusSessionStatus.Finished

    # endregion
