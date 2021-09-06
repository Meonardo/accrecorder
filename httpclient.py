import random
import string
import time
import subprocess
import signal
import os
import aiohttp
import datetime
from aiohttp import ClientSession

from janus import JanusSession, JanusSessionStatus, RecordSession, RecordSessionStatus
from recorder import RecordFile, RecordSegment, PausedFile

# janus HTTP server
JANUS_HOST = 'http://192.168.5.12:8088/janus'
# static publisher IDs
CAM1 = 1
CAM2 = 2
CAM3 = 3
CAM4 = 4
SCREEN = 9
RECORDER = 911


old_print = print


def timestamped_print(*args, **kwargs):
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(
        sep=' ',
        timespec='milliseconds')
    old_print(time_str, *args, **kwargs)


print = timestamped_print


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
        # {room: PausedFile}
        self.__pause_files = {}
        self.http_session: ClientSession = aiohttp.ClientSession()

    async def close(self):
        await self.http_session.close()

    async def configure(self, room, class_id, cloud_class_id, upload_server, forwarder, janus):
        global JANUS_HOST
        JANUS_HOST = janus + "/janus"
        success = await self.start_forwarding(room, [CAM1, CAM2, SCREEN], forwarder)
        if success:
            janus: JanusSession = self.__sessions[room]
            janus.cloud_class_id = cloud_class_id
            janus.class_id = class_id
            janus.upload_server = upload_server
        return success

    # reset
    async def reset(self, room):
        if room not in self.__sessions:
            return False
        self.__sessions.pop(room, None)

        keys = list(map(lambda x: str(room) + "-" + str(x), [CAM1, CAM2]))
        for k in keys:
            if k in self.__record_sessions:
                self.__record_sessions.pop(k, None)

        if room in self.__files:
            self.__files.pop(room, None)
        if room in self.__pause_files:
            self.__pause_files.pop(room, None)

        return True

    # region Forwarding
    async def start_forwarding(self, room, publishers, forwarder):
        session_id = await self.__login_janus()
        if session_id is not None:
            handle_id = await self.__fetch_janus_handle(session_id)
            self.__create_janus_session(room)
            for p in publishers:
                session: RecordSession = self.__create_record_session(room, p)
                if session.status != RecordSessionStatus.Default:
                    continue
                # 创建sdp文件和文件路径
                janus_session: JanusSession = self.__sessions[room]
                janus_session.janus_session_id = session_id
                janus_session.janus_handle_id = handle_id
                session.create_file_folder()
                session.create_sdp(forwarder)
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

    async def __stop_forwarding(self, session: RecordSession):
        async def _stop_stream(janus, stream):
            forwarding_obj = session.stop_forwarding_obj(stream)
            forward_message = {"request": "stop_rtp_forward", "secret": "adminpwd"}.copy()
            forward_message.update(forwarding_obj)
            payload = self.__janus_message(forward_message, janus.janus_session_id, janus.janus_handle_id)
            path = JANUS_HOST + "/" + str(janus.janus_session_id)
            async with self.http_session.post(path, json=payload) as response:
                return await response.json()

        janus: JanusSession = self.__sessions[session.room]
        if session.forwarder.audio_stream_id is not None:
            await _stop_stream(janus, session.forwarder.audio_stream_id)
        if session.forwarder.video_stream_id is not None:
            await _stop_stream(janus, session.forwarder.video_stream_id)

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
    async def start_recording(self, room, publishers, forwarder):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        if len(publishers) > 2:
            print("Room{}, recording number invalid.".format(room))
            return False
        forwarding_sessions = self.__active_sessions(room, RecordSessionStatus.Forwarding)
        if len(forwarding_sessions) == 0:
            # 未进行转发的开启转发
            success = await self.start_forwarding(room, [CAM1, CAM2, SCREEN], forwarder)
            if success:
                forwarding_sessions = self.__active_sessions(room, RecordSessionStatus.Forwarding)
            else:
                print("Room{}, RTP forwarding failed.".format(room))
                return False

        available = []
        for p in publishers:
            for p1 in forwarding_sessions:
                if str(p) == str(p1.publisher):
                    available.append(p1)

        print("Room{}, is launching.".format(room))
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
        janus.status = JanusSessionStatus.Recording

    def __record_screen_cam(self, screen: RecordSession, cam: RecordSession):
        print("Room{r}, recording screen & cam{c}".format(r=screen.room, c=cam.publisher))
        janus_session: JanusSession = self.__sessions[screen.room]
        if janus_session is not None:
            janus_session.recording_screen = True

        folder = screen.folder
        begin_time = int(time.time())

        s_name = str(screen.publisher) + "_" + str(begin_time) + ".ts"
        s_file_path = folder + s_name
        screen_sdp = folder + screen.forwarder.name

        c_name = str(cam.publisher) + "_" + str(begin_time) + ".ts"
        c_file_path = folder + c_name
        cam_sdp = folder + cam.forwarder.name

        proc_c = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-protocol_whitelist', 'file,udp,rtp',
             '-i', cam_sdp, '-c:a', 'mp3', '-c:v', 'copy', c_file_path,
             ])
        cam.recorder_pid = proc_c.pid
        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(
            p=cam.publisher, r=cam.room, pid=proc_c.pid))
        # '-use_wallclock_as_timestamps', '1'
        proc_s = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-protocol_whitelist', 'file,udp,rtp',
             '-i', screen_sdp, '-c:a', 'mp3', '-c:v', 'copy', s_file_path,
             ])
        screen.recorder_pid = proc_s.pid

        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(
            p=screen.publisher, r=screen.room, pid=proc_s.pid))
        screen.status = RecordSessionStatus.Recording
        cam.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=s_name, begin_time=begin_time, room=screen.room, publisher=screen.publisher,
                                cam_name=c_name)
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
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-protocol_whitelist', 'file,udp,rtp', '-use_wallclock_as_timestamps', '1', '-i', sdp, '-c',
             'copy', file_path])
        session.recorder_pid = proc.pid

        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(p=session.publisher, r=session.room, pid=proc.pid))
        session.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher)
        if session.room not in self.__files:
            file = RecordFile(room=session.room, file=segment)
            self.__files[session.room] = file
        else:
            file: RecordFile = self.__files[session.room]
            file.files.append(segment)

        return file

    # 切换摄像头
    async def switch_camera(self, room, publisher):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        if publisher not in [CAM1, CAM2]:
            print("Room{r}, CAM{c} is invalidate".format(r=room, c=publisher))
            return False
        cam: RecordSession = self.__find_record_session(room, publisher)
        if cam.status == RecordSessionStatus.Recording:
            print("Room{} is not recording".format(room))
            return False

        janus: JanusSession = self.__sessions[room]
        if janus.recording_screen:
            screen = self.__find_record_session(room, SCREEN)
            if screen is None:
                return False
            self.__stop_recording_session(screen, False)
            recording_cam = self.__recording_cam(room)
            self.__stop_recording_session(recording_cam, False)
            self.__record_screen_cam(screen, cam)
        else:
            recording_cam = self.__recording_cam(room)
            self.__stop_recording_session(recording_cam, False)
            recording_cam.status = RecordSessionStatus.Forwarding
            self.__record_cam(cam)
        return True

    # 开始录制屏幕
    async def start_recording_screen(self, room):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
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
            print("Room{} not configure yet".format(room))
            return False
        janus_session: JanusSession = self.__sessions[room]
        if janus_session is None:
            return False
        if not janus_session.recording_screen:
            print("Room{}, Screen is not recording yet".format(room))
            return False
        screen = self.__find_record_session(room, SCREEN)
        if screen is None:
            print("Room{}, Can not find screen session".format(room))
            return False
        # 结束录屏但继续录制摄像头
        self.__stop_recording_session(screen, False)
        cam = self.__recording_cam(room)
        self.__stop_recording_session(cam, False)
        time.sleep(0.02)
        self.__record_cam(cam)

        janus_session.recording_screen = False

        return True

    def __stop_recording_session(self, session: RecordSession, remove=True):
        room = session.room
        if session.recorder_pid is not None:
            try:
                os.kill(session.recorder_pid, signal.SIGINT)
                session.recorder_pid = None
            except Exception as e:
                pass
            time.sleep(0.1)
        if remove:
            session.status = RecordSessionStatus.Stopped
            key = str(session.room) + "-" + str(session.publisher)
            session.clean_ports()
            self.__record_sessions.pop(key, None)
        else:
            session.status = RecordSessionStatus.Forwarding

        # 更新文件信息
        file: RecordFile = self.__files[room]
        end_time = int(time.time())
        if file is not None:
            segment: RecordSegment = file.files[-1]
            segment.end_time = end_time
            if segment.publisher == session.publisher:
                # merge if needed
                segment.merge()

    # 暂停录制
    async def pause_recording(self, room):
        await self.stop_recording(room, True)

    # 停止录制
    async def stop_recording(self, room, pause=False):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        if room not in self.__files:
            print("Room{}, file not found".format(room))
            return False
        sessions = self.__active_sessions(room)
        for s in sessions:
            await self.__stop_forwarding(s)
            self.__stop_recording_session(s)

        if len(sessions) == 0:
            return False
        await self.__processing_file(room, pause)

        # 清理资源
        self.__sessions.pop(room, None)
        self.__files.pop(room, None)

        return True

    async def __processing_file(self, room, pause=False):
        print("Starting processing all the files from room = ", room)

        if room in self.__files:
            file: RecordFile = self.__files[room]
            session: JanusSession = self.__sessions[room]
            if file is not None:
                print("\n\n\n----------PROCESSING--------\n\n\n")
                session.status = JanusSessionStatus.Processing
                file.process()
                # 清理所有的文件
                file.clear_all_files()
                if pause:
                    if room not in self.__pause_files:
                        paused_file = PausedFile(room, file)
                        self.__pause_files[room] = paused_file
                    else:
                        paused_file: PausedFile = self.__pause_files[room]
                        paused_file.files.append(file)
                else:
                    if room in self.__pause_files:
                        self.__pause_files.pop(room, None)
                session.status = JanusSessionStatus.Uploading
                resp = await file.upload(session, self.http_session)
                if resp is not None:
                    print(resp)
                    session.status = JanusSessionStatus.Finished

    # endregion
