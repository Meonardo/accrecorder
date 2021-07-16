from logging import raiseExceptions
import attr
import random
import time
from pathlib import Path

from enum import Enum

@attr.s
class JanusEvent:
    sender = attr.ib(validator=attr.validators.instance_of(int))

@attr.s
class PluginData(JanusEvent):
    plugin = attr.ib(validator=attr.validators.instance_of(str))
    data = attr.ib()
    jsep = attr.ib()

@attr.s
class WebrtcUp(JanusEvent):
    pass

@attr.s
class Media(JanusEvent):
    receiving = attr.ib(validator=attr.validators.instance_of(bool))
    kind = attr.ib(validator=attr.validators.in_(["audio", "video"]))

    @kind.validator
    def validate_kind(self, attribute, kind):
        if kind not in ["video", "audio"]:
            raise ValueError("kind must equal video or audio")

@attr.s
class SlowLink(JanusEvent):
    uplink = attr.ib(validator=attr.validators.instance_of(bool))
    lost = attr.ib(validator=attr.validators.instance_of(int))

@attr.s
class HangUp(JanusEvent):
    reason = attr.ib(validator=attr.validators.instance_of(str))

@attr.s(cmp=False)
class Ack:
    transaction = attr.ib(validator=attr.validators.instance_of(str))

@attr.s
class Jsep:
    sdp = attr.ib()
    type = attr.ib(validator=attr.validators.in_(["offer", "pranswer", "answer", "rollback"]))


class SessionStatus(Enum):
        Defalut = 1
        Started = 2
        Forwarding = 3
        Recording = 4
        Stopped = 5

        Processing = 10
        PFinished = 19

        Uploading = 30
        UFinished = 31

        Failed = -1

PORTS = []
FILE_ROOT_PATH = "/Users/amdox/File/Combine/.recordings/"

def ramdom_port():
    p = random.randint(20001, 50000)
    if p not in PORTS:
        PORTS.append(p)
        return p
    else:
        ramdom_port

class JanusSession:

    class Forwarding:
        def __init__(self, vp=ramdom_port(), ap=ramdom_port(), vpt=102, apt=96):
            self.videoport = vp
            self.audioport = ap
            self.videopt = vpt
            self.audiopt = apt
            self.videocodec = "H246/90000"
            self.videofmpt = "packetization-mode=1;profile-level-id=42e01f"
            self.audiocodec = "opus/48000/2"
            self.avformat_v = "58.76.100"
            self.forward_host = "192.168.5.99"
            self.name = None
        
        def create_sdp(self, path, name):
            self.name = name

            f = open(path,"a+")
            f.write(
                "v=0\r\no=- 0 0 IN IP4 {host}\r\ns={name}\r\nc=IN IP4 {host}\r\nt=0 0\r\na=tool:libavformat {avformat_v}\r\nm=audio {audioport} RTP {audiopt}\r\na=rtpmap:{audiopt} {audiocodec}\r\nm=video {videoport} RTP/AVP {videopt}\r\na=rtpmap:{videopt} {videocodec}\r\na=fmtp:{videopt} {videofmpt}\r\n"
                .format(
                    name = name,
                    host = self.forward_host,
                    avformat_v = self.avformat_v,
                    audioport = self.audioport,
                    audiopt = self.audiopt,
                    videoport = self.videoport,
                    videopt = self.videopt,
                    videofmpt = self.videofmpt,
                    audiocodec = self.audiocodec,
                    videocodec = self.videocodec
                    ))
            f.close()

    def __init__(self, room, pin, display, publisher, startedTime, forwarder=Forwarding()):
        self.room = room
        self.pin = pin
        self.display = display
        self.publisher = publisher
        self.startedTime = startedTime
        self.status = SessionStatus.Defalut
        self.forwarder:JanusSession.Forwarding = forwarder
        self.folder = None
        self.recorder_pid = None

    # 创建录像房间的文件夹, 当前房间会话的所有文件都在此文件夹中
    def create_file_folder(self):
        dir = FILE_ROOT_PATH + str(self.room)
        Path(dir).mkdir(parents=True, exist_ok=True)
        self.folder = dir + "/"

        print("\nroom folder created at: ", self.folder, "\n")

    # 创建 sdp file 给 rtp forwarding -> FFMpeg    
    def create_sdp(self):
        assert self.folder

        # t = time.time()
        name = "{p}_janus.sdp".format(p=self.publisher)
        file_path = self.folder + name
        self.forwarder.create_sdp(path=file_path, name=name)

        print("\nsdp file created at: ", file_path, "\n")

    def forwarding_obj(self):
        return {
            "host": self.forwarder.forward_host,
            "audio_port":self.forwarder.audioport,
            "video_port":self.forwarder.videoport, 
            "audio_pt":self.forwarder.audiopt, 
            "video_pt":self.forwarder.videopt, 
            "publisher_id": int(self.publisher), 
            "room":self.room,
        }