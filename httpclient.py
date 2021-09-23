import random
import string
import time
import subprocess
import signal
import os
import aiohttp
import datetime

from aiohttp import ClientSession
from janus import RecorderStatus, RecordSession, RecordSessionStatus, RecordManager
from recorder import RecordFile, RecordSegment, PausedFile

SCREEN = 'screen'

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
        # {room: RecordManager}
        self.__sessions = {}
        # {room: RecordFile}
        self.__files = {}
        # {room: PausedFile}
        self.__pause_files = {}
        self.http_session: ClientSession = aiohttp.ClientSession()

    async def close(self):
        await self.http_session.close()

    def __create_recorder(self, room) -> RecordManager:
        if room in self.__sessions:
            session: RecordManager = self.__sessions[room]
            if session.status.value < RecorderStatus.Processing.value:
                print("Current recorder is in the room")
                return session

        recorder = RecordManager(room=room)
        recorder.status = RecorderStatus.Starting
        self.__sessions[room] = recorder
        return recorder

    def __create_record_session(self, room, publisher, mic=None):
        if room not in self.__sessions:
            return None
        recorder: RecordManager = self.__sessions[room]
        session_key = str(publisher)
        start_time = int(time.time())
        if session_key not in recorder.sessions:
            record_session = RecordSession(room=room, publisher=publisher, started_time=start_time, mic=mic)
            recorder.sessions[session_key] = record_session
            return record_session
        return recorder.sessions[session_key]

    async def configure(self, room, class_id, cloud_class_id, upload_server):
        recorder = self.__create_recorder(room)
        recorder.cloud_class_id = cloud_class_id
        recorder.class_id = class_id
        recorder.upload_server = upload_server

        return True

    # reset
    async def reset(self, room):
        if room not in self.__sessions:
            return False
        self.__sessions.pop(room, None)

        if room in self.__files:
            self.__files.pop(room, None)
        if room in self.__pause_files:
            self.__pause_files.pop(room, None)

        return True

    # region Recording

    def __find_record_session(self, room, publisher):
        if room not in self.__sessions:
            return None
        recorder: RecordManager = self.__sessions[room]
        session_key = str(publisher)
        if session_key in recorder.sessions:
            return recorder.sessions[session_key]
        return None

    def __active_sessions(self, room, status=None):
        if room not in self.__sessions:
            return None
        recorder: RecordManager = self.__sessions[room]
        p = recorder.sessions.keys()

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
        sessions = self.__active_sessions(room, status=RecordSessionStatus.Recording)
        if sessions is not None:
            sessions = list(filter(lambda x: x.publisher == SCREEN, sessions))
            if len(sessions) > 0:
                return sessions[0]
        return None

    # 开始录制视频
    async def start_recording(self, room, publisher, mic, screen):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False

        recorder: RecordManager = self.__sessions[room]
        if recorder is not None:
            recorder.recording_screen = screen

        available = []
        if screen:
            session = self.__create_record_session(room, SCREEN)
            if session is not None:
                available.append(session)
        session = self.__create_record_session(room, publisher, mic)
        if session is not None:
            available.append(session)

        print("Room{}, is launching.".format(room))
        self.__launch_recorder(room, available)

        return True

    def __launch_recorder(self, room, sessions: [RecordSession]):
        recorder: RecordManager = self.__sessions[room]
        if len(sessions) > 1 and recorder.recording_screen:
            screen = self.__find_record_session(room=room, publisher=SCREEN)
            sessions.remove(screen)
            cam = sessions[0]
            self.__record_screen_cam(screen=screen, cam=cam)
        else:
            self.__record_cam(session=sessions[0])
        recorder.status = RecorderStatus.Recording

    def __record_screen_cam(self, screen: RecordSession, cam: RecordSession):
        print("Room{r}, recording screen & cam{c}".format(r=screen.room, c=cam.publisher))
        recorder: RecordManager = self.__sessions[screen.room]
        if recorder is not None:
            recorder.recording_screen = True

        folder = screen.folder
        begin_time = int(time.time())

        s_name = str(screen.publisher) + "_" + str(begin_time) + ".ts"
        s_file_path = folder + s_name

        c_name = str(cam.publisher) + "_" + str(begin_time) + ".ts"
        c_file_path = folder + c_name

        if cam.mic is not None:
            mic = 'audio=' + cam.mic
            proc_c = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'info',
                 '-use_wallclock_as_timestamps', '1', '-rtsp_transport', 'tcp',
                 '-i', cam.publisher, '-itsoffset', '1', '-f', 'dshow', '-i', mic,
                 '-c:v', 'copy', '-c:a', 'aac', c_file_path,
                 ])
        else:
            proc_c = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'info',
                 '-use_wallclock_as_timestamps', '1', '-rtsp_transport', 'tcp',
                 '-i', cam.publisher,
                 '-c:v', 'copy', c_file_path
                 ])
        cam.recorder_pid = proc_c.pid
        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(
            p=cam.publisher, r=cam.room, pid=proc_c.pid))
        print("Room{r}, CAM{c} File create at:".format(r=cam.room, c=cam.publisher), c_file_path)
        # '-use_wallclock_as_timestamps', '1'
        video = 'video=screen-capture-recorder'
        proc_s = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-f', 'dshow',
             '-i', video, '-c:v', 'h264_qsv',
             '-b:v', '4M', '-preset', 'fast', '-tune', 'zerolatency', s_file_path
             ])
        screen.recorder_pid = proc_s.pid

        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(
            p=screen.publisher, r=screen.room, pid=proc_s.pid))
        print("Room{r}, Screen{c} File create at:".format(r=screen.room, c=screen.publisher), s_file_path)
        screen.status = RecordSessionStatus.Recording
        cam.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=s_name, begin_time=begin_time, room=screen.room, publisher=screen.publisher,
                                cam_name=c_name, folder=screen.folder)
        if screen.room not in self.__files:
            file = RecordFile(room=screen.room, folder=screen.folder, file=segment)
            self.__files[screen.room] = file
        else:
            file: RecordFile = self.__files[screen.room]
            file.files.append(segment)

    def __record_cam(self, session: RecordSession):
        folder = session.folder
        begin_time = int(time.time())
        name = str(session.publisher) + "_" + str(begin_time) + ".ts"
        file_path = folder + name

        if session.mic is not None:
            mic = 'audio=' + session.mic
            proc = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'info',
                 '-use_wallclock_as_timestamps', '1', '-rtsp_transport', 'tcp',
                 '-i', session.publisher, '-itsoffset', '1', '-f', 'dshow', '-i', mic,
                 '-c:v', 'copy', '-c:a', 'aac', file_path,
                 ])
        else:
            proc = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'info',
                 '-use_wallclock_as_timestamps', '1', '-rtsp_transport', 'tcp',
                 '-i', session.publisher,
                 '-c:v', 'copy', file_path
                 ])
        session.recorder_pid = proc.pid

        print("Room{r}, CAM{c} File create at:".format(r=session.room, c=session.publisher), file_path)
        print("Now publisher {p} in the room {r} is recording...\nFFmpeg subprocess pid: {pid}".format(p=session.publisher, r=session.room, pid=proc.pid))
        session.status = RecordSessionStatus.Recording

        # 保存文件信息
        segment = RecordSegment(name=name, begin_time=begin_time, room=session.room, publisher=session.publisher, folder=session.folder)
        if session.room not in self.__files:
            file = RecordFile(room=session.room, folder=session.folder, file=segment)
            self.__files[session.room] = file
        else:
            file: RecordFile = self.__files[session.room]
            file.files.append(segment)

        return file

    def __publisher_exist(self, room, publisher):
        if room not in self.__sessions:
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        return publisher in recorder.sessions

    # 切换摄像头
    async def switch_camera(self, room, publisher):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        if not self.__publisher_exist(room, publisher):
            print("Room{r}, CAM{c} is invalidate".format(r=room, c=publisher))
            return False
        cam: RecordSession = self.__find_record_session(room, publisher)
        if cam.status == RecordSessionStatus.Recording:
            print("Room{} is not recording".format(room))
            return False

        recorder: RecordManager = self.__sessions[room]
        if recorder.recording_screen:
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
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        else:
            if recorder.status == RecorderStatus.Default:
                return False

        screen: RecordSession = self.__find_record_session(room, SCREEN)
        if screen.status == RecordSessionStatus.Recording:
            return False

        cam = self.__recording_cam(room)
        if cam is None:
            return False
        self.__stop_recording_session(cam, False)
        self.__record_screen_cam(screen, cam)

        return True

    # 结束录制屏幕
    async def stop_recording_screen(self, room):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        if not recorder.recording_screen:
            print("Room{}, Screen is not recording yet".format(room))
            return False
        screen = self.__find_record_session(room, SCREEN)
        if screen is None:
            print("Room{}, Can not find screen session".format(room))
            return False
        # 结束录屏但继续录制摄像头
        self.__stop_recording_session(screen, False)
        cam = self.__recording_cam(room)
        if cam is None:
            return False
        self.__stop_recording_session(cam, False)
        time.sleep(0.02)
        self.__record_cam(cam)

        recorder.recording_screen = False

        return True

    def __remove_record_session(self, room, publisher):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        recorder.sessions.pop(publisher, None)
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
            self.__remove_record_session(room, session.publisher)
        else:
            session.status = RecordSessionStatus.Forwarding

        # 更新文件信息
        file: RecordFile = self.__files[room]
        end_time = int(time.time())
        if file is not None:
            # wait for subprocess to terminate
            time.sleep(1)
            segment: RecordSegment = file.files[-1]
            segment.end_time = end_time
            if segment.publisher == session.publisher:
                # merge if needed
                segment.merge()

    # 暂停录制
    async def pause_recording(self, room):
        return await self.stop_recording(room, True)

    # 停止录制
    async def stop_recording(self, room, pause=False):
        if room not in self.__sessions:
            print("Room{} not configure yet!".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder.status.value < RecorderStatus.Recording.value:
            print("Room{}, not recording yet, please try again.".format(room))
            return False
        if room not in self.__files:
            print("Room{}, file not found!".format(room))
            return False
        sessions = self.__active_sessions(room)
        for s in sessions:
            remove = not pause
            self.__stop_recording_session(s, remove)

        if len(sessions) == 0:
            return False
        return self.__processing_file(room, pause)

    def __processing_file(self, room, pause=False):
        print("Starting processing all the files from room =", room)

        if room in self.__files:
            file: RecordFile = self.__files[room]
            recorder: RecordManager = self.__sessions[room]
            if file is not None:
                print("\n\n\n----------PROCESSING--------\n\n\n")
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
                recorder.status = RecorderStatus.Processing
                file.add_process_callback(self)
                success = file.process(recorder=recorder, pause=pause)
                return success

    def file_processing_callback(self, room):
        # 清理资源
        self.__sessions.pop(room, None)
        self.__files.pop(room, None)

    # endregion
