import os
import subprocess
import asyncio
import aiohttp

from enum import Enum
from janus import FILE_ROOT_PATH, SCREEN
from pathlib import Path
from janus import JanusSession
from aiohttp import FormData

TIME_THRESHOLD = 3


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
    def __init__(self, name, room, publisher, begin_time, end_time=None):
        self.name = name
        self.room = room
        self.publisher = publisher
        self.begin_time = begin_time
        self.end_time = end_time
        self.is_screen = int(publisher) == SCREEN


class RecordFile:
    def __init__(self, room, file: RecordSegment):
        self.room = room
        self.cameras = []
        self.screens = []
        self.files = [file]
        self.status: RecordStatus = RecordStatus.Defalut

        self.folder = FILE_ROOT_PATH + str(self.room)
        self._join_file_path = None
        self._file_cuts = None
        self._cuts_path = None
        self._output_path = None
        self._thumbnail_path = None

        # 屏幕和Cam同时开始/结束
        self.start_simultaneously = False
        self.stop_simultaneously = False

    def process(self):
        self.files = list(filter(None, self.files))

        self._join_files()
        self._transcode()
        print("\n\n***********\nDone! file at path: ", self._output_path, "\n***********\n\n")
        print("***********\nDone! thumbnail at path: ", self._thumbnail_path, "\n***********\n\n")

    # 判断是否同时开始或者同时结束
    def _process_time(self):
        begin = self.cameras[0].begin_time
        screen_s = self.screens[0].begin_time
        end = self.cameras[-1].end_time
        screen_e = self.screens[-1].end_time

        # 如果开始时间相近, 阈值为3, 认为同时开始
        if abs(begin - screen_s) <= TIME_THRESHOLD:
            self.start_simultaneously = True
        if abs(end - screen_e) <= TIME_THRESHOLD:
            self.stop_simultaneously = True

    # 将所有的文件拼接
    def _join_files(self):
        print("Starting join all the camera files")

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

    def _transcode(self):
        audio_codec = 'mp3'
        self._join_file_path = self.folder + "/joined.ts"
        self._output_path = self.folder + "/output.mp4"
        # CLI ffmpeg -i input_file.fmt -c:v copy -c:a aac output.mp4
        p = subprocess.Popen(
            ['ffmpeg', '-i', self._join_file_path, '-c:v', 'copy', '-c:a', 'copy', self._output_path])
        p.wait()
        # CLI ffmpeg -i input.mp4 -ss 00:00:01.000 -vframes 1 output.png
        self._thumbnail_path = self.folder + "/thumbnail.png"
        p = subprocess.Popen(
            ['ffmpeg', '-i', self._output_path, '-ss', '00:00:05.000', '-vframes', '1', self._thumbnail_path])
        p.wait()

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

    # 将合并的摄像头文件根据屏幕文件进行分段
    def _separate_files(self):
        cuts = self._cal_cuts()
        self._file_cuts = cuts

        print("--------CUT [START]--------")

        self._cuts_path = self.folder + "/cuts"
        Path(self._cuts_path).mkdir(parents=True, exist_ok=True)

        index = 0
        for cut in cuts:
            cut.name = "cut_{i}.ts".format(i=index)
            p = subprocess.run(
                "ffmpeg -i {source} -ss {s} -to {e} -c:v libx264 -crf 17 -c:a copy -preset fast {t}".format(
                    s=cut.begin,
                    source=self._join_file_path,
                    e=cut.end,
                    t=self._cuts_path + "/" + cut.name), shell=True)
            index += 1

        print("--------CUT [END]--------")

    # 计算分段
    def _cal_cuts(self):
        begin = self.cameras[0].begin_time
        end = self.cameras[-1].end_time

        def ti(segment: RecordSegment):
            return MergeFile(begin=segment.begin_time - begin, end=segment.end_time - begin, merge=True)

        merges = list(map(ti, self.screens))

        first = MergeFile(begin=0, end=merges[0].begin, merge=self.start_simultaneously)
        cuts = [first]

        for index in range(len(merges)):
            cur = merges[index]
            if index == 0 and self.start_simultaneously == False:
                cuts.append(cur)
                continue
            else:
                pre = merges[index - 1]
                cuts.append(MergeFile(begin=pre.end, end=cur.begin, merge=False))
                cuts.append(cur)

        if self.stop_simultaneously == False:
            cuts.append(MergeFile(begin=merges[-1].end, end=end - begin, merge=False))

        return cuts

    # [PiP]形式融合屏幕和摄像头画面
    def _merge(self, single_segment=False):
        print("Starting merge all the camera & screen files")

        if single_segment:
            screen_target = "{f}/{n}".format(f=self.folder, n=self.screens[0].name)
            overlay_target = "{f}/{n}".format(f=self.folder, n=self.cameras[0].name)
            merged_path = "{f}/{n}".format(f=self.folder, n="/join_merged.ts")
            p = subprocess.Popen(['ffmpeg',
                                  '-i', screen_target,
                                  '-i', overlay_target,
                                  '-filter_complex',
                                  '[1]scale=iw/4:ih/4[pip];[0][pip] overlay=main_w-overlay_w-10:main_h-overlay_h-10',
                                  '-codec:v', 'libx264', '-crf', '17', '-preset', 'fast', '-codec:a', 'copy',
                                  merged_path])

            p.wait()
        else:
            filtered = list(filter(lambda x: x.merge, self._file_cuts))

            assert len(filtered) == len(self.screens)

            procs = []
            for index in range(len(self.screens)):
                screen_target = "{f}/{n}".format(f=self.folder, n=self.screens[index].name)
                cut: MergeFile = filtered[index]
                overlay_target = "{f}/{n}".format(f=self._cuts_path, n=cut.name)
                cut.merged_name = "merged_{n}.ts".format(n=index)
                merged_path = "{f}/{n}".format(f=self._cuts_path, n=cut.merged_name)

                p = subprocess.Popen(['ffmpeg',
                                      '-i', screen_target,
                                      '-i', overlay_target,
                                      '-filter_complex',
                                      '[1]scale=iw/4:ih/4[pip];[0][pip] overlay=main_w-overlay_w-10:main_h-overlay_h-10',
                                      '-codec:v', 'libx264', '-crf', '17', '-preset', 'fast', '-codec:a', 'copy',
                                      merged_path])

                procs.append(p)

            r = [p.wait() for p in procs]
            print(r)
        print("\n\n***********\nMerge Done!\n***********\n\n")

    # 拼接所有文件
    def _join_all_files(self):
        print("Starting join all the files to single mp4 file")

        file_path = self._cuts_path

        def l(file: MergeFile):
            name = file.name
            if file.merge:
                name = file.merged_name
            return "file " + file_path + "/" + name

        file_names = list(map(l, self._file_cuts))
        contents = str.join("\r\n", file_names)

        cmd_file_path = self.folder + "/join_merged.txt"

        # 删除原来有的
        if os.path.isfile(cmd_file_path):
            print("File exits: ", cmd_file_path, " removing now...")
            os.remove(cmd_file_path)

        f = open(cmd_file_path, "a+")
        f.write(contents)
        f.close()

        target = self.folder + "/join_merged.ts"
        p = subprocess.Popen(['ffmpeg', '-f', 'concat', '-safe', '0', '-i', cmd_file_path, '-c', 'copy', target])
        p.wait()

        self.status = RecordStatus.Finished

        print("\n\n***********\nDone! file at path: ", target, "\n***********\n\n")
