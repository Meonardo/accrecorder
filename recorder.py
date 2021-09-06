import os
import subprocess
import platform
import aiohttp
import random
import string
import time
from threading import Thread

from enum import Enum
from janus import SCREEN
from janus import JanusSession
from aiohttp import FormData

TIME_THRESHOLD = 3


# Random filename
def filename():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


def async_func(f):
    def wrapper(*args, **kwargs):
        thr = Thread(target = f, args = args, kwargs = kwargs)
        thr.start()
    return wrapper


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
    def __init__(self, name, room, publisher, begin_time, end_time=None, cam_name=None):
        self.name = name
        self.room = room
        self.publisher = publisher
        self.begin_time = begin_time
        self.end_time = end_time
        self.cam_name = cam_name
        self.is_screen = int(publisher) == SCREEN
        self.merge_finished = False

    @async_func
    def merge(self):
        if not self.is_screen or self.cam_name is None:
            return
        if platform.system() == "Darwin":
            is_linux = False
            file_dir = "/Users/amdox/File/Combine/.recordings/" + str(self.room)
        else:
            file_dir = "/home/hd/recorder/videos/recordings/" + str(self.room)
            is_linux = True
        screen_file = file_dir + "/" + self.name
        cam_file = file_dir + "/" + self.cam_name
        output_path = file_dir + "/" + filename() + ".ts"
        if is_linux:
            p = subprocess.Popen(['ffmpeg',
                                  '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
                                  '-i', screen_file,
                                  '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
                                  '-i', cam_file,
                                  '-filter_complex',
                                  '[1]scale_npp=640:320:format=nv12[overlay];[0][overlay]overlay_cuda=x=1260:y=740',
                                  '-codec:v', 'h264_nvenc', '-crf', '17', '-preset', 'p6', '-b:v', '8M',
                                  '-codec:a', 'copy',
                                  output_path])
        else:
            p = subprocess.Popen(['ffmpeg',
                                  '-i', screen_file,
                                  '-i', cam_file,
                                  '-filter_complex',
                                  '[1]scale=iw/3:ih/3[pip];[0][pip] overlay=main_w-overlay_w-20:main_h-overlay_h-20',
                                  '-codec:v', 'h264_videotoolbox', '-preset', 'fast', '-b:v', '8M',
                                  '-codec:a', 'copy',
                                  output_path])
        p.wait()
        ret = subprocess.run('mv {s} {t}'.format(s=output_path, t=screen_file), shell=True)
        print(ret)
        self.merge_finished = True

        return


class RecordFile:
    def __init__(self, room, file: RecordSegment):
        self.room = room
        self.files = [file]
        self.status: RecordStatus = RecordStatus.Defalut
        if platform.system() == "Darwin":
            file_dir = "/Users/amdox/File/Combine/.recordings/"
        else:
            file_dir = "/home/hd/recorder/videos/recordings/"
        self.folder = file_dir + str(self.room)
        self._join_file_path = None
        self._file_cuts = None
        self._cuts_path = None
        self._output_path = None
        self._thumbnail_path = None
        self.paused_file = []

    def process(self):
        self.files = list(filter(None, self.files))

        self._join_files()
        self._transcode()
        print("\n\n***********\nDone! file at path: ", self._output_path, "\n***********\n\n")
        print("***********\nDone! thumbnail at path: ", self._thumbnail_path, "\n***********\n\n")

    # 将所有的文件拼接
    def _join_files(self):
        print("Starting join all the camera files")

        while True:
            try:
                merging = next((True for file in self.files if file.is_screen and not file.merge_finished), False)
                if merging:
                    time.sleep(1)
                    print("---------- Wait for all merge tasks -----------")
                    continue
                else:
                    break
            except StopIteration:
                time.sleep(1)
                continue

        file_names = list(map(lambda s: "file " + self.folder + "/" + s.name, self.files))
        contents = str.join("\r\n", file_names)

        cmd_file_path = self.folder + "/join.txt"

        # 删除原来有的
        if os.path.isfile(cmd_file_path):
            print("File exits: ", cmd_file_path, " removing now...")
            os.remove(cmd_file_path)

        f = open(cmd_file_path, "a+")
        f.write(contents)
        f.close()

        self._join_file_path = self.folder + "/joined.ts"
        p = subprocess.Popen(
            ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', cmd_file_path, '-c', 'copy', self._join_file_path])
        p.wait()

        self.status = RecordStatus.Processing

    # 转码操作
    def _transcode(self):
        audio_codec = 'aac'
        self._join_file_path = self.folder + "/joined.ts"
        time_str = time.strftime("%Y-%m-%d_%Hh%Mm%Ss", time.localtime())
        self._output_path = self.folder + "/" + "output_{}.mp4".format(time_str)
        # CLI ffmpeg -i input_file.fmt -c:v copy -c:a aac output.mp4
        p = subprocess.Popen(
            ['ffmpeg', '-i', self._join_file_path, '-c:v', 'copy', '-c:a', audio_codec, self._output_path])
        p.wait()
        # CLI ffmpeg -i input.mp4 -ss 00:00:01.000 -vframes 1 output.png
        self._thumbnail_path = self.folder + "/thumbnail_{}.png".format(time_str)
        p = subprocess.Popen(
            ['ffmpeg', '-i', self._output_path, '-ss', '00:00:05.000', '-vframes', '1', self._thumbnail_path])
        p.wait()

    # 上传操作
    async def upload(self, janus: JanusSession, session: aiohttp.ClientSession):
        if janus.class_id is None or janus.cloud_class_id is None or janus.upload_server is None:
            return None
        path = janus.upload_server + "?classId={c}&cloudClassId={cc}".format(c=janus.class_id, cc=janus.cloud_class_id)
        data = FormData()
        data.add_field('videoFile',
                       open(self._output_path, 'rb'),
                       filename='output.mp4',
                       content_type='multipart/form-data')
        data.add_field('imageFile',
                       open(self._thumbnail_path, 'rb'),
                       filename='thumbnail.png',
                       content_type='multipart/form-data')
        async with session.post(path, data=data) as response:
            return await response.json()

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
    def clear_all_files(self):
        command = "cd {}; rm 1_*; rm 2_*; rm 9_*; rm join*".format(self.folder)
        ret = subprocess.run(command, shell=True)
        print(ret)


class PausedFile:
    def __init__(self, room, file: RecordFile):
        self.room = room
        self.files = [file]
