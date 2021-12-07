import os
import platform
import random
import string
import datetime

from threading import Thread
from pathlib import Path
from enum import Enum
from aiohttp import FormData
from urllib.parse import urlparse

TIME_THRESHOLD = 3
SCREEN = 'screen'


old_print = print


def timestamped_print(*args, **kwargs):
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(
        sep=' ',
        timespec='milliseconds')
    old_print(time_str, *args, **kwargs)


print = timestamped_print


# Random filename
def filename():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


def async_func(f):
    def wrapper(*args, **kwargs):
        thr = Thread(target = f, args = args, kwargs = kwargs)
        thr.start()
    return wrapper


class RecorderStatus(Enum):
    Default = 1
    Starting = 2
    Recording = 4
    Stopped = 5
    Processing = 6
    Uploading = 6
    Paused = 7
    Finished = 8

    Failed = -1


class RecorderSession:
    def __init__(self, room):
        self.room = room
        self.sessions = {}
        self.recording_screen = False
        self.class_id = None
        self.cloud_class_id = None
        self.upload_server = None
        self.status: RecorderStatus = RecorderStatus.Default
        self.recording_cam = None
        self.folder = None
        self.record_file_path = None
        self.thumbnail_file_path = None

    # 创建录像房间的文件夹, 当前房间会话的所有文件都在此文件夹中
    def create_file_folder(self):
        p = platform.system().lower()
        if p == "windows":
            file_dir = os.path.expanduser(os.getenv('USERPROFILE')) + '\\recordings\\' + str(self.room) + '\\'
        else:
            file_dir = str(Path.home()) + "/recordings/" + str(self.room) + "/"

        Path(file_dir).mkdir(parents=True, exist_ok=True)
        self.folder = file_dir

        print("\nroom folder created at: ", self.folder, "\n")


