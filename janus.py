import platform
import random
import os
from pathlib import Path
from enum import Enum


class JanusSessionStatus(Enum):
    Default = 1
    Starting = 2
    Forwarding = 3
    Recording = 4
    Stopped = 5
    Processing = 6
    Uploading = 6
    Finished = 7

    Failed = -1


class RecordSessionStatus(Enum):
    Default = 1
    Started = 2
    Forwarding = 3
    Recording = 4
    Uploading = 4
    Stopped = 5
    Failed = -1


# 屏幕的ID
SCREEN = 9
# 端口管理
PORTS = []


def random_port():
    p = random.randint(20001, 50000)
    if p not in PORTS:
        PORTS.append(p)
        return p
    else:
        return random_port()


class JanusSession:
    def __init__(self, room, pin, display) -> None:
        self.room = room
        self.pin = pin
        self.display = display
        self.session = None
        self.handle = None
        self.status: JanusSessionStatus = JanusSessionStatus.Default
        self.loop = None
        self.recording_screen = False
        self.janus_session_id = None
        self.janus_handle_id = None
        self.class_id = None
        self.cloud_class_id = None
        self.upload_server = None


# RTP forwarding 参数
class JanusRTPForwarder:
    def __init__(self, vp, ap, forwarder, vpt=102, apt=96):
        self.videoport = vp
        self.audioport = ap
        self.videopt = vpt
        self.audiopt = apt
        self.videocodec = "H264/90000"
        self.videofmpt = "packetization-mode=1;profile-level-id=42e01f"
        self.audiocodec = "opus/48000/2"
        self.avformat_v = "58.76.100"
        self.forward_host = forwarder
        self.name = None
        self.video_stream_id = None
        self.audio_stream_id = None

    def create_sdp(self, path, name):
        self.name = name

        f = open(path, "a+")
        if self.audioport == -1:
            f.write(
                "v=0\r\no=- 0 0 IN IP4 {host}\r\ns={name}\r\nc=IN IP4 {host}\r\nt=0 0\r\na=tool:libavformat {"
                "avformat_v}\r\nm=video {videoport} RTP/AVP {videopt}\r\na=rtpmap:{videopt} {videocodec}\r\na=fmtp:{"
                "videopt} {videofmpt}\r\n "
                    .format(
                    name=name,
                    host=self.forward_host,
                    avformat_v=self.avformat_v,
                    videoport=self.videoport,
                    videopt=self.videopt,
                    videofmpt=self.videofmpt,
                    videocodec=self.videocodec
                ))
        else:
            f.write(
                "v=0\r\no=- 0 0 IN IP4 {host}\r\ns={name}\r\nc=IN IP4 {host}\r\nt=0 0\r\na=tool:libavformat {"
                "avformat_v}\r\nm=audio {audioport} RTP {audiopt}\r\na=rtpmap:{audiopt} {audiocodec}\r\nm=video {"
                "videoport} RTP/AVP {videopt}\r\na=rtpmap:{videopt} {videocodec}\r\na=fmtp:{videopt} {videofmpt}\r\n "
                    .format(
                    name=name,
                    host=self.forward_host,
                    avformat_v=self.avformat_v,
                    audioport=self.audioport,
                    audiopt=self.audiopt,
                    videoport=self.videoport,
                    videopt=self.videopt,
                    videofmpt=self.videofmpt,
                    audiocodec=self.audiocodec,
                    videocodec=self.videocodec
                ))
        f.close()


class RecordSession:
    def __init__(self, room, publisher, started_time):
        self.room = room
        self.publisher = publisher
        self.started_time = started_time

        self.status = RecordSessionStatus.Default
        self.forwarder: JanusRTPForwarder = None
        self.folder = None
        self.recorder_pid = None

    # 创建录像房间的文件夹, 当前房间会话的所有文件都在此文件夹中
    def create_file_folder(self):
        if platform.system() == "Darwin":
            file_dir = "/Users/amdox/File/Combine/.recordings/" + str(self.room)
        else:
            file_dir = "/home/h/videos/" + str(self.room)
        Path(file_dir).mkdir(parents=True, exist_ok=True)
        self.folder = file_dir + "/"

        print("\nroom folder created at: ", self.folder, "\n")

    # 创建 sdp file 给 rtp forwarding -> FFMpeg    
    def create_sdp(self, forwarder):
        assert self.folder

        if self.publisher == SCREEN:
            self.forwarder = JanusRTPForwarder(vp=random_port(), ap=-1, forwarder=forwarder)
        else:
            self.forwarder = JanusRTPForwarder(vp=random_port(), ap=random_port(), forwarder=forwarder)

        # t = time.time()
        name = "{p}_janus.sdp".format(p=self.publisher)
        file_path = self.folder + name

        # 删除原来有的 sdp 文件
        if os.path.isfile(file_path):
            print("File exits: ", file_path, " removing now...")
            os.remove(file_path)

        self.forwarder.create_sdp(path=file_path, name=name)

        print("\nsdp file created at: ", file_path, "\n")

    def update_forwarder(self, v_stream=None, a_stream=None):
        if v_stream is not None:
            self.forwarder.video_stream_id = v_stream
        if a_stream is not None:
            self.forwarder.audio_stream_id = a_stream

    def forwarding_obj(self):
        if self.publisher == SCREEN:
            return {
                "host": self.forwarder.forward_host,
                "video_port": self.forwarder.videoport,
                "video_pt": self.forwarder.videopt,
                "publisher_id": int(self.publisher),
                "room": int(self.room),
            }
        return {
            "host": self.forwarder.forward_host,
            "audio_port": self.forwarder.audioport,
            "video_port": self.forwarder.videoport,
            "audio_pt": self.forwarder.audiopt,
            "video_pt": self.forwarder.videopt,
            "publisher_id": int(self.publisher),
            "room": int(self.room),
        }

    def stop_forwarding_obj(self, stream):
        if self.publisher == SCREEN:
            return {
                "stream_id": int(stream),
                "video_port": self.forwarder.videoport,
                "audio_pt": self.forwarder.audiopt,
                "video_pt": self.forwarder.videopt,
                "publisher_id": int(self.publisher),
                "room": self.room,
            }
        return {
            "stream_id": int(stream),
            "audio_port": self.forwarder.audioport,
            "video_port": self.forwarder.videoport,
            "audio_pt": self.forwarder.audiopt,
            "video_pt": self.forwarder.videopt,
            "publisher_id": int(self.publisher),
            "room": self.room,
        }

    # 回收本机的 RTP forwarding listen port
    def clean_ports(self):
        if self.forwarder.audioport is not None:
            if self.forwarder.audioport in PORTS:
                PORTS.remove(self.forwarder.audioport)
        if self.forwarder.videoport in PORTS:
            PORTS.remove(self.forwarder.videoport)
