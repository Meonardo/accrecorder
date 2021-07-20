import os
from posixpath import join
import subprocess
import signal

from enum import Enum
from typing import Iterator
from janus import FILE_ROOT_PATH, SCREEN

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

class RecordSegment:
    def __init__(self, name, room, publisher, begin_time, end_time=None):
        self.name = name
        self.room = room
        self.publisher = publisher
        self.begin_time = begin_time
        self.end_time = end_time
        self.is_screen = int(publisher) == SCREEN

class RecordFile:
    def __init__(self, room, cam:RecordSegment, screen:RecordSegment=None):
        self.room = room
        self.cameras = [cam]
        self.screens = [screen]
        self.status:RecordStatus = RecordStatus.Defalut

        self.folder = FILE_ROOT_PATH + str(self.room)
        self._join_file_path = None
        self._file_cuts = None

    def process(self):
        self.screens = list(filter(None, self.screens))
        self.cameras = list(filter(None, self.cameras))

        # 拼接所有的摄像头文件
        self._join_cameras()
        # 裁剪与屏幕对应的文件
        if len(self.screens) > 0:
            self._separate_files()
            # 合并画中画
            self._merge()
            # 拼接
            self._join_all_files()
        else:
            print("*******Done.*******")

    # 将所有的摄像头文件拼接
    def _join_cameras(self):
        print("Starting join all the camera files")
        
        file_names = list(map(lambda s: "file " + self.folder + "/" + s.name, self.cameras))
        contents = str.join("\r\n", file_names)

        cmd_file_path =  self.folder + "/join.txt"

        # 删除原来有的
        if os.path.isfile(cmd_file_path):
            print("File exits: ", cmd_file_path, " removing now...")
            os.remove(cmd_file_path)

        f = open(cmd_file_path, "a+")
        f.write(contents)
        f.close()

        self._join_file_path = self.folder + "/joind.ts"
        subprocess.Popen(['ffmpeg', '-f', 'concat', '-safe', '0', '-i', cmd_file_path, '-c', 'copy', self._join_file_path])

        self.status = RecordStatus.Processing
    
    # 将合并的摄像头文件根据屏幕文件进行分段
    def _separate_files(self):
        cuts = self._cal_cuts()
        self._file_cuts = cuts

        print("File will be cut to: \n", cuts)
        
        print("--------CUT [START]--------")

        index = 0
        segments = []
        for cut in cuts:
            cut.name = "cut_{i}".format(i=index)
            segments.append(cut.begin)
            index += 1
        
        subprocess.Popen(['ffmpeg', '-i', self._join_file_path, '-c', 'copy', '-f', 'segment', '-segment_times', str.join(",", segments), '{f}/cut_%d.mp4'.format(f=self.folder)])

        print("--------CUT [END]--------")

    # 计算分段
    def _cal_cuts(self):
        begin = self.cameras[0].begin_time
        end = self.cameras[0].end_time

        def ti(segment:RecordSegment):
            return MergeFile(begin=segment.begin_time-begin, end=segment.end_time-begin, merge=True)
        merges = list(map(ti, filter(None, self.screens)))

        first = MergeFile(begin=0, end=merges[0].begin, merge=False)
        cuts = [first]

        for index in range(len(merges)):
            cur = merges[index]
            if index == 0:
                continue
            else:
                pre = merges[index-1]
                cuts.append(MergeFile(begin=pre.end, end=cur.begin, merge=False))
            cuts.append(cur)

        cuts.append(MergeFile(begin=merges[-1].end, end=end, merge=False))

        return cuts

    # [PiP]形式融合屏幕和摄像头画面
    def _merge(self):
        print("Starting merge all the camera & screen files")

    # 拼接所有文件
    def _join_all_files(self):
        print("Starting join all the files to single mp4 file")