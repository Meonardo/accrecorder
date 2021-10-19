import asyncio
import os
import simpleobsws
import time
import aiohttp

from threading import Thread
from aiohttp import FormData
from urllib.parse import urlparse
from recorder import RecordStatus, RecorderStatus, RecordManager, print


def async_func(f):
    def wrapper(*args, **kwargs):
        thr = Thread(target = f, args = args, kwargs = kwargs)
        thr.start()
    return wrapper


loop = asyncio.get_event_loop()
ws = simpleobsws.obsws(host='127.0.0.1', port=9999, password='kick', loop=loop)

MAIN_SCENE = 'MainScene'
SCREEN_W = 1920
SCREEN_H = 1080
CAM_SCALE = 1 / 3
SCREEN_SOURCE_NAME = 'Screen'
OUTPUT_FILE_EXT = 'mp4'
OUTPUT_IMG_EXT = 'png'


class SceneItem:
    def __init__(self, scene, obj: dict):
        self.scene = scene
        self.name = obj['name']
        self.type = obj['type']
        self.id = obj['id']
        self.visible = True
        # self.scale = 1
        # self.width = obj['width']
        # self.height = obj['height']
        self.x = obj['x']
        self.y = obj['y']
        self.locked = obj['locked']
        self.muted = obj['muted']

    async def set_visible(self, visible):
        obj = {
            "scene-name": self.scene,
            "item": self.name,
            "visible": visible
        }
        r = await ws.call('SetSceneItemProperties', obj)
        self.visible = visible
        print("Update visible status", r)
        return r

    async def delete(self):
        obj = {
            'scene': self.scene,
            'item': {
                'id': self.id,
                'name': self.name
            }
        }
        r = await ws.call('DeleteSceneItem', obj)
        print("Delete Scene Item {s}: {r}".format(s=self.name, r=r))

    async def reset(self):
        obj = {
            'scene': self.scene,
            'item': {
                'id': self.id,
                'name': self.name
            }
        }
        r = await ws.call('ResetSceneItem', obj)
        print("Reset Scene Item {s}: {r}".format(s=self.name, r=r))

    async def update_position_scale(self, x, y, scale, animated=True):
        obj = {
            "scene-name": self.scene,
            "item": self.name,
            'position': {
                'alignment': 5,
                'x': x,
                'y': y
            },
            'scale': {
                'x': scale,
                'y': scale
            },
        }
        return await ws.call('SetSceneItemProperties', obj)


class Scene:
    def __init__(self, name: str, items: list, is_current=True):
        self.name = name
        self.sources: list[SceneItem] = items
        self.cam_sources: list[SceneItem] = [item for item in items if item.name != SCREEN_SOURCE_NAME]
        self.is_current = is_current
        self.mic = None

    def find_item(self, name) -> SceneItem:
        if len(self.sources) == 0:
            return None
        else:
            for item in self.sources:
                if item.name == name:
                    return item

    async def add_transition_override(self):
        transition = {
            "sceneName": self.name,
            "transitionName": "fade_transition",
            "transitionDuration": 400
        }
        return await ws.call('SetSceneTransitionOverride', transition)

    @staticmethod
    async def file_settings(folder, room, file_format):
        await ws.call('SetFilenameFormatting', {"filename-formatting": file_format})
        await ws.call('SetRecordingFolder', {"rec-folder": folder})

    @staticmethod
    async def __start_recording():
        await ws.call('StartRecording')

    @staticmethod
    async def __stop_recording():
        await ws.call('StopRecording')

    async def start_recording(self, cam, screen):
        cam_item = self.find_item(cam)
        if cam_item is not None:
            await cam_item.set_visible(True)
            # 全屏显示摄像头
            if not screen:
                await cam_item.update_position_scale(0, 0, 1)
        if screen:
            screen_item = self.find_item(SCREEN_SOURCE_NAME)
            if screen_item is not None:
                await screen_item.set_visible(True)

        # 开始录制
        await self.__start_recording()

    async def stop_recording(self):
        for source in self.sources:
            await source.set_visible(False)

        # 结束录制
        await self.__stop_recording()

    # 截屏
    async def screenshot(self, file):
        obj = {
            "sourceName": self.name,
            "saveToFilePath": file
        }
        await ws.call('TakeSourceScreenshot', obj)


class ObsClient:

    def __init__(self):
        # {room: RecordManager}
        self.__sessions = {}
        self.scene: Scene = None
        self.obs_connected = False

    def close(self):
        self.scene = None
        loop.run_until_complete(
            ws.disconnect()
        )
        self.obs_connected = False
        loop.close()

    def __create_recorder(self, room) -> RecordManager:
        if room in self.__sessions:
            session: RecordManager = self.__sessions[room]
            if session.status.value < RecorderStatus.Processing.value:
                print("Current recorder is in the room")
                return session

        recorder = RecordManager(room=room)
        recorder.status = RecorderStatus.Starting
        recorder.create_file_folder()
        self.__sessions[room] = recorder
        return recorder

    @staticmethod
    def __rtsp_to_str(rtsp) -> str:
        o = urlparse(rtsp)
        r = o.netloc.replace('.', '_') + o.path.replace('/', '_')
        return r

    async def __create_scene(self, cam1, cam2, mic):
        if not self.obs_connected:
            await ws.connect()
            self.obs_connected = True

        scene_name = MAIN_SCENE
        scene = await self.__find_scene(scene_name)
        if scene is not None:
            for source in scene.sources:
                await source.reset()
        else:
            # 创建当前 scene
            await self.create_scene(scene_name)
        # 选中当前 Scene
        await ws.call('SetCurrentScene', {"scene-name": MAIN_SCENE})
        # 创建 screen capture sources
        screen_source_name = SCREEN_SOURCE_NAME
        r = await self.__create_screen_capture(scene_name, screen_source_name)
        print("Create Source:", r)

        cam1_source_name = self.__rtsp_to_str(cam1)
        r = await self.__create_rtsp_source(scene_name, cam1_source_name, cam1)
        print("Create Source:", r)
        cam2_source_name = self.__rtsp_to_str(cam2)
        r = await self.__create_rtsp_source(scene_name, cam2_source_name, cam2)
        print("Create Source:", r)
        # 刷新 scene 对象
        self.scene = await self.__find_scene(scene_name)

        self.scene.mic = mic
        # 初始化位置设置和隐藏
        x = SCREEN_W - SCREEN_W * CAM_SCALE
        y = SCREEN_H - SCREEN_H * CAM_SCALE
        cam1_item = self.scene.find_item(cam1_source_name)
        if cam1_item is not None:
            await cam1_item.set_visible(False)
            await cam1_item.update_position_scale(x, y, CAM_SCALE)
        cam2_item = self.scene.find_item(cam2_source_name)
        if cam2_item is not None:
            await cam2_item.set_visible(False)
            await cam2_item.update_position_scale(x, y, CAM_SCALE)
        screen_item = self.scene.find_item(screen_source_name)
        if screen_item is not None:
            await screen_item.set_visible(False)

    @staticmethod
    async def __find_scene(name: str):
        scenes = await ws.call('GetSceneList')
        if 'scenes' in scenes:
            current = scenes['current-scene']
            scenes = scenes['scenes']
            for scene in scenes:
                if scene['name'] == name:
                    items = [SceneItem(name, item) for item in scene['sources']]
                    s = Scene(name, items, is_current=(name == current))
                    return s

    @staticmethod
    def __find_source(scene: dict, name: str):
        if 'sources' in scene:
            for source in scene['sources']:
                if source['name'] == name:
                    print("Screen Source: ", source)
                    return source

    @staticmethod
    async def create_scene(name: str):
        r = await ws.call('CreateScene', {"sceneName": name})
        print("Create Scene:", r)

    @staticmethod
    async def __create_screen_capture(scene_name: str, source_name: str):
        source_settings = {
            "alignment": 5,
            "cx": SCREEN_W,
            "cy": SCREEN_H,
            "locked": True,
            "name": source_name,
            "render": True,
            "x": 0,
            "y": 0,
            "source_cx": SCREEN_W,
            "source_cy": SCREEN_H
        }
        all_types = await ws.call('GetSourceTypesList')
        all_types = all_types['types']
        type_ids = [type['typeId'] for type in all_types if 'typeId' in type]
        print(type_ids)

        type = 'monitor_capture'
        if 'monitor_capture' in type_ids:
            type = "monitor_capture"
        elif 'display_capture' in type_ids:
            type = "display_capture"

        obj = {
            "sceneName": scene_name,
            "sourceName": source_name,
            "sourceKind": type,
            "sourceSettings": source_settings
        }
        return await ws.call('CreateSource', obj)

    @staticmethod
    async def __create_rtsp_source(scene_name: str, source_name: str, rtsp: str):
        source_settings = {
            "alignment": 5,
            "width": SCREEN_W,
            "height": SCREEN_H,
            "locked": True,
            "name": source_name,
            "render": True,
            "x": 0,
            "y": 0,
            "source_cx": SCREEN_W,
            "source_cy": SCREEN_H,
            "is_local_file": False,
            "hw_decode": True,
            "input": rtsp,
            "input_format": "rtsp",
            "buffering_mb": 1
        }
        obj = {
            "sceneName": scene_name,
            "sourceName": source_name,
            "sourceKind": "ffmpeg_source",
            "sourceSettings": source_settings
        }
        return await ws.call('CreateSource', obj)

    @staticmethod
    async def __create_file_source(scene_name: str, source_name: str, file_path: str, visible=True):
        source_settings = {
            "alignment": 5,
            "locked": True,
            "name": source_name,
            "source_cx": SCREEN_W,
            "source_cy": SCREEN_H,
            "is_local_file": True,
            "hw_decode": True,
            "looping": True,
            "visible": visible,
            "input_format": "file",
            "local_file": file_path,
        }
        obj = {
            "sceneName": scene_name,
            "sourceName": source_name,
            "sourceKind": "ffmpeg_source",
            "sourceSettings": source_settings
        }
        return await ws.call('CreateSource', obj)

    # configure
    def configure(self, room, class_id, cloud_class_id, upload_server, cam1, cam2, mic):
        recorder = self.__create_recorder(room)
        recorder.cloud_class_id = cloud_class_id
        recorder.class_id = class_id
        recorder.upload_server = upload_server

        loop.run_until_complete(
            self.__create_scene(cam1, cam2, mic)
        )
        return True

    # reset
    def reset(self, room):
        if room not in self.__sessions:
            return False
        self.__sessions.pop(room, None)

        return True

    # 开始录制视频
    def start_recording(self, room, cam, screen):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False

        recorder: RecordManager = self.__sessions[room]
        if recorder is not None:
            recorder.recording_screen = screen
            if recorder.status == RecorderStatus.Recording:
                print("Room{} is recording...".format(room))
                return False

        recorder.recording_cam = cam
        cam_source_name = self.__rtsp_to_str(cam)
        time_str = time.strftime("%Y-%m-%d_%Hh%Mm%Ss", time.localtime())
        output_format = "output_{}_".format(room) + time_str

        recorder.record_file_path = recorder.folder + output_format + "." + OUTPUT_FILE_EXT

        asyncio.set_event_loop(loop)
        tasks = [
            self.scene.file_settings(recorder.folder, room, output_format),
            self.scene.start_recording(cam_source_name, screen)
        ]
        loop.run_until_complete(
            asyncio.gather(*tasks)
        )

        recorder.status = RecorderStatus.Recording
        self.__take_screenshot(room, time_str, recorder)

        return True

    @async_func
    def __take_screenshot(self, room, time_str, recorder: RecordManager):
        output_format = "output_{}_".format(room) + time_str
        recorder.thumbnail_file_path = recorder.folder + output_format + "." + OUTPUT_IMG_EXT
        time.sleep(2)
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            self.scene.screenshot(recorder.thumbnail_file_path)
        )
        print("Room{r} Take screen shot successfully at file path {p}".format(r=room, p=recorder.thumbnail_file_path))

    # 切换摄像头
    def switch_camera(self, room, cam):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        if recorder.status != RecorderStatus.Recording:
            print("Room{} not recording yet".format(room))
            return False
        if recorder.recording_cam is not None and recorder.recording_cam == cam:
            print("Room{r} CAM {c} is recording".format(r=room, c=cam))
            return False

        recording_cam_source_name = self.__rtsp_to_str(recorder.recording_cam)
        cam_source_name = self.__rtsp_to_str(cam)
        recording_cam_item = self.scene.find_item(recording_cam_source_name)
        cam_item = self.scene.find_item(cam_source_name)

        if recorder.recording_screen:
            x = SCREEN_W - SCREEN_W * CAM_SCALE
            y = SCREEN_H - SCREEN_H * CAM_SCALE
            if cam_item and recording_cam_item:
                tasks = [
                    cam_item.update_position_scale(x, y, CAM_SCALE),
                    cam_item.set_visible(True),
                    recording_cam_item.set_visible(False),
                ]
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    asyncio.gather(*tasks)
                )
        else:
            if cam_item and recording_cam_item:
                tasks = [
                    cam_item.update_position_scale(0, 0, 1),
                    cam_item.set_visible(True),
                    recording_cam_item.set_visible(False),
                ]
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    asyncio.gather(*tasks)
                )

        recorder.recording_cam = cam
        return True

    # 开始录制屏幕
    def start_recording_screen(self, room):
        if room not in self.__sessions:
            print("Room{} is not configured yet".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        else:
            if recorder.status == RecorderStatus.Default:
                print("Room{} is not recording".format(room))
                return False
            if recorder.recording_screen:
                print("Room{} Screen is recording".format(room))
                return False

        recording_cam_source_name = self.__rtsp_to_str(recorder.recording_cam)
        screen_item = self.scene.find_item(SCREEN_SOURCE_NAME)
        cam_item = self.scene.find_item(recording_cam_source_name)
        if screen_item and cam_item:
            x = SCREEN_W - SCREEN_W * CAM_SCALE
            y = SCREEN_H - SCREEN_H * CAM_SCALE
            tasks = [
                screen_item.set_visible(True),
                cam_item.set_visible(True),
                cam_item.update_position_scale(x, y, CAM_SCALE)]
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                asyncio.gather(*tasks)
            )
            recorder.recording_screen = True
            return True
        else:
            return False

    # 结束录制屏幕
    def stop_recording_screen(self, room):
        if room not in self.__sessions:
            print("Room{} not configure yet".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder is None:
            return False
        if not recorder.recording_screen:
            print("Room{}, Screen is not recording yet".format(room))
            return False

        recording_cam_source_name = self.__rtsp_to_str(recorder.recording_cam)
        screen_item = self.scene.find_item(SCREEN_SOURCE_NAME)
        cam_item = self.scene.find_item(recording_cam_source_name)
        if screen_item and cam_item:
            tasks = [
                screen_item.set_visible(False),
                cam_item.set_visible(True),
                cam_item.update_position_scale(0, 0, 1)
            ]
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                asyncio.gather(*tasks)
            )
            recorder.recording_screen = False
            return True
        else:
            return False

    # 暂停录制
    def pause_recording(self, room):
        return self.stop_recording(room, True)

    # 停止录制
    def stop_recording(self, room, pause=False):
        if room not in self.__sessions:
            print("Room{} not configure yet!".format(room))
            return False
        recorder: RecordManager = self.__sessions[room]
        if recorder.status.value < RecorderStatus.Recording.value:
            print("Room{}, not recording yet, please try again.".format(room))
            return False

        loop.run_until_complete(
            self.scene.stop_recording()
        )
        if pause:
            recorder.status = RecorderStatus.Paused
        else:
            recorder.status = RecorderStatus.Stopped
        return self.__processing_file(room, recorder, pause)

    def __processing_file(self, room, recorder: RecordManager, pause=False):
        print("Starting processing file from room =", room)
        if recorder.record_file_path is not None:
            recorder.status = RecordStatus.Processing

            # Set a timeout 20s
            timeout = time.time() + 20
            while True:
                if time.time() > timeout:
                    break
                time.sleep(1)
                if os.path.isfile(recorder.record_file_path):
                    break

            print("\n***********\nDone! file at path: ", recorder.record_file_path, "\n***********\n")

            # 上传
            loop.run_until_complete(
                self.upload(recorder)
            )
        return True

    # 上传操作
    @staticmethod
    async def upload(recorder: RecordManager):
        recorder.status = RecordStatus.Uploading
        session = aiohttp.ClientSession()
        if recorder.class_id is None or recorder.cloud_class_id is None or recorder.upload_server is None:
            return None
        if not os.path.isfile(recorder.record_file_path) or not os.path.isfile(recorder.thumbnail_file_path):
            return None
        path = recorder.upload_server + "?classId={c}&cloudClassId={cc}".format(c=recorder.class_id,
                                                                                cc=recorder.cloud_class_id)
        print("Upload URL:", path)
        data = FormData()
        data.add_field('videoFile',
                       open(recorder.record_file_path, 'rb'),
                       filename='output.mp4',
                       content_type='multipart/form-data')
        data.add_field('imageFile',
                       open(recorder.thumbnail_file_path, 'rb'),
                       filename='thumbnail.png',
                       content_type='multipart/form-data')
        print(u"Room{r}, Uploading files begin...".format(r=recorder.room))
        try:
            async with session.post(path, data=data) as response:
                r = await response.json()
                print(u"Room{r}, Uploading files response: {o}".format(r=recorder.room, o=r))
        except Exception as e:
            print("Room{r}, received upload exception: {e}".format(r=recorder.room, e=e))

        recorder.status = RecordStatus.Finished
        await session.close()
