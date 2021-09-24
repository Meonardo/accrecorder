import asyncio
import os
import subprocess
import platform
import aiohttp
import random
import string
import time
import weakref
import copy
import datetime

from threading import Thread
from pathlib import Path
from enum import Enum
from aiohttp import FormData

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


class RecordStatus(Enum):
    Defalut = 1
    Started = 2
    Processing = 10
    Finished = 19
    Uploading = 20
    Uploaded = 29

    Failed = -1


class MergeFile:
    def __init__(self, begin, end, merge):
        self.begin = begin
        self.end = end
        self.merge = merge
        self.name = None
        self.merged_name = None


class RecordSegment:
    def __init__(self, name, room, publisher, folder, begin_time, end_time=None, cam_name=None):
        self.name = name
        self.room = room
        self.publisher = publisher
        self.begin_time = begin_time
        self.end_time = end_time
        self.cam_name = cam_name
        self.folder = folder
        self.is_screen = str(publisher) == 'screen'
        self.merge_finished = False

    def __str__(self):
        return "RecordSegment: filename: {fn}, publisher: {p}, room: {r}\n"\
            .format(fn=self.name, p=self.publisher, r=self.room)

    @async_func
    def merge(self):
        if not self.is_screen or self.cam_name is None:
            return
        screen_file = self.folder + "\\" + self.name
        cam_file = self.folder + "\\" + self.cam_name
        output_path = self.folder + "\\" + filename() + ".ts"
        p = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'info',
                              '-i', screen_file,
                              '-i', cam_file,
                              '-filter_complex',
                              '[1]scale=iw/3:ih/3[pip];[0][pip] overlay=main_w-overlay_w-20:main_h-overlay_h-20',
                              '-codec:v', 'h264_qsv', '-preset', 'fast', '-b:v', '6M',
                              '-codec:a', 'copy',
                              output_path])

        print("Starting merging {s} & {c}...".format(s=self.name, c=self.cam_name))
        p.wait()
        ret = subprocess.run('rename {s} {t}'.format(s=output_path, t=screen_file), shell=True)
        print(ret)
        self.merge_finished = True

        return


class RecordFile:
    def __init__(self, room, folder, file: RecordSegment):
        self.room = room
        self.files = [file]
        self.status: RecordStatus = RecordStatus.Defalut
        self.folder = folder
        self._join_file_path = None
        self._file_cuts = None
        self._cuts_path = None
        self._output_path = None
        self._thumbnail_path = None
        self.paused_file = []
        self.parent = None

    def add_process_callback(self, target):
        self.parent = weakref.ref(target)()

    def process(self, recorder: RecordManager, pause):
        self.files = list(filter(None, self.files))
        targets = self.files

        for file in targets:
            file_path = self.folder + "\\" + file.name
            if not os.path.isfile(file_path):
                print("Room{r}, file({f}) not exits: ".format(r=self.room, f=file_path))
                return False

        self._processing(recorder, pause, targets)
        return True

    @async_func
    def _processing(self, recorder: RecordManager, pause, targets):
        clear_targets = copy.deepcopy(targets)
        # 拼接
        self._join_files(targets)
        # 转码
        self._transcode()
        print("\n\n***********\nDone! file at path: ", self._output_path, "\n***********\n\n")
        print("***********\nDone! thumbnail at path: ", self._thumbnail_path, "\n***********\n\n")
        # 上传
        recorder.status = RecorderStatus.Uploading
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        r: aiohttp.ClientSession = loop.run_until_complete(self.upload(recorder))
        if r is not None:
            loop.run_until_complete(r.close())
        loop.close()
        print(u"Room{r}, Uploading files finished".format(r=self.room))
        # 清理所有的文件
        if self.parent is not None:
            if not pause:
                self.parent.file_processing_callback(self.room)
            self.parent = None
        self.clear_all_files(pause, clear_targets)
        if pause:
            recorder.status = RecorderStatus.Paused
        else:
            recorder.status = RecorderStatus.Finished

    # 将所有的文件拼接
    def _join_files(self, targets):
        print("Starting join all the camera files")

        while True:
            try:
                merging = next((True for file in targets if file.is_screen and not file.merge_finished), False)
                if merging:
                    time.sleep(1)
                    print("---------- Wait for all merge tasks -----------")
                    continue
                else:
                    break
            except StopIteration:
                time.sleep(1)
                continue

        time_str = time.strftime("%Y-%m-%d_%Hh%Mm%Ss", time.localtime())
        file_names = list(map(lambda s: "file " + self.folder + "\\" + s.name, targets))
        contents = str.join("\r\n", file_names)
        cmd_file_path = self.folder + "\\join_{}.txt".format(time_str)

        # 删除原来有的
        if os.path.isfile(cmd_file_path):
            print("File exits: ", cmd_file_path, " removing now...")
            os.remove(cmd_file_path)

        f = open(cmd_file_path, "a+")
        f.write(contents)
        f.close()

        self._join_file_path = self.folder + "\\joined_{}.ts".format(time_str)
        p = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', cmd_file_path, '-c', 'copy', self._join_file_path])
        p.wait()

        self.status = RecordStatus.Processing

    # 转码操作
    def _transcode(self):
        time_str = time.strftime("%Y-%m-%d_%Hh%Mm%Ss", time.localtime())
        self._output_path = self.folder + "\\output_{}.mp4".format(time_str)
        # CLI ffmpeg -i input_file.fmt -c:v copy -c:a aac output.mp4
        p = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', self._join_file_path, '-c', 'copy', self._output_path])
        p.wait()
        # CLI ffmpeg -i input.mp4 -ss 00:00:01.000 -vframes 1 output.png
        self._thumbnail_path = self.folder + "\\thumbnail_{}.png".format(time_str)
        p = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', self._output_path, '-ss', '00:00:01.000', '-vframes', '1', self._thumbnail_path])
        p.wait()

    # 上传操作
    async def upload(self, recorder: RecordManager):
        session = aiohttp.ClientSession()
        if recorder.class_id is None or recorder.cloud_class_id is None or recorder.upload_server is None:
            return None
        if not os.path.isfile(self._output_path) or not os.path.isfile(self._thumbnail_path):
            return None
        path = recorder.upload_server + "?classId={c}&cloudClassId={cc}".format(c=recorder.class_id, cc=recorder.cloud_class_id)
        print("Upload URL:", path)
        data = FormData()
        data.add_field('videoFile',
                       open(self._output_path, 'rb'),
                       filename='output.mp4',
                       content_type='multipart/form-data')
        data.add_field('imageFile',
                       open(self._thumbnail_path, 'rb'),
                       filename='thumbnail.png',
                       content_type='multipart/form-data')
        print(u"Room{r}, Uploading files begin...".format(r=self.room))
        try:
            async with session.post(path, data=data) as response:
                r = await response.json()
                print(u"Room{r}, Uploading files response: {o}".format(r=self.room, o=r))
        except Exception as e:
            print("Room{r}, received upload exception: {e}".format(r=self.room, e=e))
        return session

    # 获取上传文件的信息，如：时长和文件大小
    def fetch_filesize(self):
        import json

        file_path = self._output_path
        result = subprocess.check_output(
            f'ffprobe -v quiet -print_format json -show_format "{file_path}"',
            shell=True).decode()
        print(result)
        format_info = json.loads(result)['format']
        print(format_info)

        duration = format_info['duration']
        file_size = format_info['size']
        return duration, file_size

    # 清除所有辅助文件， 仅保留截图和输出视频
    def clear_all_files(self, pause, targets):
        if not pause:
            command = "pushd {} && del 1_* && del 2_* && del 9_* && del join*".format(self.folder)
        else:
            file_names = list(map(lambda s: "del " + s.name, targets))
            contents = str.join(" && ", file_names)
            command = "pushd {} && ".format(self.folder) + contents + " && del {file}".format(file=self._join_file_path)

            for t in targets:
                count = len(self.files)
                index = 0
                while index < count:
                    file = self.files[index]
                    if t.name == file.name:
                        del self.files[index]
                        break
                    index += 1

        print("Room{r}, clean file cmd: {cmd}".format(r=self.room, cmd=command))
        ret = subprocess.run(command, shell=True)
        print(ret)


class PausedFile:
    def __init__(self, room, file: RecordFile):
        self.room = room
        self.files = [file]
