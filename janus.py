import platform
import os
from pathlib import Path
from enum import Enum


class RecorderStatus(Enum):
    Default = 1
    Starting = 2
    # Forwarding = 3
    Recording = 4
    Stopped = 5
    Processing = 6
    Uploading = 6
    Paused = 7
    Finished = 8

    Failed = -1


class RecordSessionStatus(Enum):
    Default = 1
    Started = 2
    # Forwarding = 3
    Recording = 3
    Uploading = 4
    Stopped = 5
    Failed = -1


class RecordManager:
    def __init__(self, room):
        self.room = room
        self.sessions = {}
        self.recording_screen = False
        self.class_id = None
        self.cloud_class_id = None
        self.upload_server = None
        self.status: RecorderStatus = RecorderStatus.Default


class RecordSession:
    def __init__(self, room, publisher, started_time, mic=None):
        self.room = room
        self.publisher = publisher
        self.started_time = started_time

        self.status = RecordSessionStatus.Default
        self.folder = None
        self.recorder_pid = None
        self.mic = mic

    # 创建录像房间的文件夹, 当前房间会话的所有文件都在此文件夹中
    def create_file_folder(self):
        p = platform.system().lower()
        if p == "darwin":
            file_dir = "/Users/amdox/File/Combine/.recordings/" + str(self.room) + "/"
        elif p == "linux":
            file_dir = "/home/hd/recorder/videos/recordings/" + str(self.room) + "/"
        else:
            file_dir = os.path.expanduser(os.getenv('USERPROFILE')) + '\\recordings\\' + str(self.room) + '\\'

        Path(file_dir).mkdir(parents=True, exist_ok=True)
        self.folder = file_dir

        print("\nroom folder created at: ", self.folder, "\n")
