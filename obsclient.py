import asyncio
import os
import time

from threading import Thread
from urllib.parse import urlparse
from simpleobsws import WebSocketClient, Request, IdentificationParameters
from recorder import RecorderStatus, RecorderSession, print, async_func


loop = asyncio.get_event_loop()
parameters = IdentificationParameters(ignoreInvalidMessages=False, ignoreNonFatalRequestChecks=False)
ws = WebSocketClient(url="ws://127.0.0.1:9991", password="amdox", identification_parameters=parameters)

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
        # {'inputKind': 'monitor_capture', 'isGroup': None, 'sceneItemId': 1, 'sceneItemIndex': 0, 'sourceName': 'Screen', 'sourceType': 'OBS_SOURCE_TYPE_INPUT'},
        self.name = obj['sourceName']
        self.type = obj['sourceType']
        self.inputKind = obj['inputKind']
        self.id = int(obj['sceneItemId'])
        self.index = int(obj['sceneItemIndex'])
        self.visible = False
        # self.scale = 1
        # self.width = obj['width']
        # self.height = obj['height']
        self.x = 0
        self.y = 0
        self.locked = False
        self.muted = False

    async def set_visible(self, visible):
        obj = {
            "sceneName": self.scene,
            "sceneItemId": self.id,
            "item": self.name,
            "visible": visible
        }
        r = await ws.call(Request('SetSceneItemProperties', obj))
        self.visible = visible
        print("Update visible status", r.responseData)
        return r

    async def delete(self):
        obj = {
            'scene': self.scene,
            'item': {
                'id': self.id,
                'name': self.name
            }
        }
        r = await ws.call(Request('DeleteSceneItem', obj))
        print("Delete Scene Item {s}: {r}".format(s=self.name, r=r.responseData))

    async def reset(self):
        obj = {
            'scene': self.scene,
            'item': {
                'id': self.id,
                'name': self.name
            }
        }
        r = await ws.call(Request('ResetSceneItem', obj))
        print("Reset Scene Item {s}: {r}".format(s=self.name, r=r.responseData))

    async def update_position_scale(self, x, y, scale, animated=True):
        obj = {
            "sceneName": self.scene,
            "sceneItemId": self.id,
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
        r = await ws.call(Request('SetSceneItemProperties', obj))
        return r.responseData

    async def scaleTo(self, width, height, x, y, animated=True):
        obj = {
            "sceneName": self.scene,
            "sceneItemId": self.id,
            "item": self.name,
            'position': {
                'alignment': 5,
                'x': x,
                'y': y
            },
            'bounds': {
                'type': 'OBS_BOUNDS_SCALE_TO_WIDTH',
                'x': width,
                'y': height
            },
        }
        await ws.call(Request('SetSceneItemProperties', obj))

    async def reorder(self, items):
        if len(items) < 2:
            return

        order_items = [{"name": i} for i in items]

        obj = {
            'scene': self.name,
            'items': order_items,
        }
        print("Reorder scene items to:", items)
        await ws.call(Request('ReorderSceneItems', obj))


class Scene:
    def __init__(self, name: str, items: list[SceneItem]=None):
        self.name = name
        if items is not None and len(items) > 0:
            self.sources: list[SceneItem] = items
            self.cam_sources: list[SceneItem] = [item for item in items if item.name != SCREEN_SOURCE_NAME]
        self.is_current = True
        self.mic = None

    def find_item(self, name) -> SceneItem:
        if self.sources is None:
            return None
        if len(self.sources) == 0:
            return None
        for item in self.sources:
            if item.name == name:
                return item
            
    async def add_transition_override(self):
        transition = {
            "sceneName": self.name,
            "transitionName": "fade_transition",
            "transitionDuration": 400
        }
        r = await ws.call(Request('SetSceneTransitionOverride', transition))
        return r.responseData

    @staticmethod
    async def file_settings(folder, room, file_format):
        await ws.call(Request('SetFilenameFormatting', {"filename-formatting": file_format}))
        await ws.call(Request('SetRecordingDirectory', {"rec-folder": folder}))

    @staticmethod
    async def __start_recording():
        await ws.call(Request('StartRecord'))

    @staticmethod
    async def __stop_recording():
        await ws.call(Request('StopRecord'))

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
            "imageFormat": OUTPUT_IMG_EXT,
            "imageFilePath": file,
        }
        await ws.call(Request('SaveSourceScreenshot', obj))
    
    async def delete(self) -> bool:
        print("Deleting scene:", self.name)
        obj = {
            "sceneName": self.name,
        }
        r = await ws.call(Request('RemoveScene', obj))
        if r.ok:
            print("Scene {0} deleted".format(self.name))
        return r.ok

class ObsClient:
    def __init__(self):
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

    def __create_recorder(self, room) -> RecorderSession:
        if room in self.__sessions:
            session: RecorderSession = self.__sessions[room]
            if session.status.value < RecorderStatus.Processing.value:
                print("Current recorder is in the room")
                return session

        recorder = RecorderSession(room=room)
        recorder.status = RecorderStatus.Starting
        recorder.create_file_folder()
        self.__sessions[room] = recorder
        return recorder

    @staticmethod
    def __rtsp_to_str(rtsp) -> str:
        o = urlparse(rtsp)
        r = o.netloc.replace('.', '_') + o.path.replace('/', '_')
        return r

    @async_func
    def __retry(self, cam1, cam2, mic):
        retry_loop = asyncio.new_event_loop()
        retry_loop.run_until_complete(
            self.__reconnect_obs(cam1, cam2, mic)
        )
        retry_loop.close()

    async def __reconnect_obs(self, cam1, cam2, mic):
        try:
            await ws.connect()
            await ws.wait_until_identified()

            self.obs_connected = True
            print("Obs connected")
            await self.__create_scene(cam1, cam2, mic)
        except Exception as exc:
            print("Obs connection exception, reconnecting...", exc)
            await asyncio.sleep(3)
            await self.__reconnect_obs(cam1, cam2, mic)

    async def __create_scene(self, cam1, cam2, mic):
        if not self.obs_connected:
            try:
                await ws.connect()
                await ws.wait_until_identified()

                print("Obs connected")
                self.obs_connected = True
            except Exception as exc:
                print("Obs connection exception", exc)
                self.__retry(cam1, cam2, mic)
                return

        scene_name = MAIN_SCENE
        scene = await self.__find_scene(scene_name)
        if scene is not None:
            # Remove exists scene
            await scene.delete()

        await asyncio.sleep(1)

        # create scene
        await self.create_scene(scene_name)
        # select the scene
        await ws.call(Request('SetCurrentProgramScene', {"sceneName": scene_name}))

        # create sources
        screen_source_name = SCREEN_SOURCE_NAME
        r = await self.__create_screen_capture(scene_name, screen_source_name)
        print("Create SCREEN Source:", r)

        cam1_source_name = self.__rtsp_to_str(cam1)
        cam2_source_name = self.__rtsp_to_str(cam2)

        r = await self.__create_rtsp_source(scene_name, cam1_source_name, cam1)
        print("Create CAM1 source:", r)
        r = await self.__create_rtsp_source(scene_name, cam2_source_name, cam2)
        print("Create CAM2 Source:", r)
        
        # refresh scene & sceneItems
        self.scene = await self.__find_scene(scene_name)
        self.scene.mic = mic

        # init sceneItems pos & scale
        x = SCREEN_W - SCREEN_W * CAM_SCALE
        y = SCREEN_H - SCREEN_H * CAM_SCALE
        cam1_item = self.scene.find_item(cam1_source_name)
        if cam1_item is not None:
            await cam1_item.update_position_scale(x, y, CAM_SCALE)
        cam2_item = self.scene.find_item(cam2_source_name)
        if cam2_item is not None:
            await cam2_item.update_position_scale(x, y, CAM_SCALE)
        screen_item = self.scene.find_item(screen_source_name)
        if screen_item is not None:
            await screen_item.scaleTo(SCREEN_W, SCREEN_H, 0, 0)


    @staticmethod
    async def __find_scene(name: str):
        scenes = await ws.call(Request('GetSceneList'))
        if not scenes.ok:
            return None

        scenes = scenes.responseData
        if 'scenes' in scenes:
            scenes = scenes['scenes']
            for scene in scenes:
                if scene['sceneName'] == name:
                    r = await ws.call(Request("GetSceneItemList", {"sceneName": name}))
                    if r.ok and r.has_data():
                        items = r.responseData["sceneItems"]
                        items = [SceneItem(name, item) for item in items]
                        s = Scene(name, items)
                        return s
                    else:
                        s = Scene(name)

    @staticmethod
    async def create_scene(name: str):
        print("Creating scene:", name)
        r = await ws.call(Request('CreateScene', {"sceneName": name}))
        if not r.ok:
            print("Create Scene failed")

    @staticmethod
    async def __create_screen_capture(scene_name: str, source_name: str):
        source_settings = {
            "alignment": 5,
            "locked": True,
            "name": source_name,
            "render": True,
            "x": 0,
            "y": 0,
            "monitor": 1
        }
        all_types = await ws.call(Request('GetSourceTypesList'))
        all_types = all_types.responseData
        all_types = all_types['types']
        type_ids = [type['typeId'] for type in all_types if 'typeId' in type]

        type = 'monitor_capture'
        if 'monitor_capture' in type_ids:
            type = "monitor_capture"
        elif 'display_capture' in type_ids:
            type = "display_capture"

        obj = {
            "sceneName": scene_name,
            "sourceName": source_name,
            "sourceKind": type,
            "setVisible": False,
            "sourceSettings": source_settings
        }
        r = await ws.call(Request('CreateSource', obj))

        sceneItemId = "0"
        if r.responseData is not None and 'sceneItemId' in r.responseData:
            sceneItemId = r.responseData["sceneItemId"]
            if sceneItemId == "0":
                return None
        return r.responseData

    @staticmethod
    async def __create_rtsp_source(scene_name: str, source_name: str, rtsp: str):
        pipeline = "uridecodebin uri={0} name=bin ! queue ! video.".format(rtsp)
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
            "pipeline": pipeline,
            "sync_appsink_video": False,
            "sync_appsink_audio": False,
            "disable_async_appsink_video": True,
            "disable_async_appsink_audio": True,
            "stop_on_hide": False,
        }
        obj = {
            "sceneName": scene_name,
            "sourceName": source_name,
            "sourceKind": "gstreamer-source",
            "setVisible": False,
            "sourceSettings": source_settings
        }
        r = await ws.call(Request('CreateSource', obj))
        return r.responseData

    # configure
    def configure(self, room, cam1, cam2, mic):
        # create recorder session
        self.__create_recorder(room)
        loop.run_until_complete(
            self.__create_scene(cam1, cam2, mic)
        )
        return self.obs_connected

    # reset
    def reset(self, room):
        if room not in self.__sessions:
            return False
        self.__sessions.pop(room, None)

        return True

    # start recording
    def start_recording(self, room, cam, screen):
        if not self.obs_connected:
            return False
        if room not in self.__sessions:
            print("Room {} not configure yet".format(room))
            return False

        recorder: RecorderSession = self.__sessions[room]
        if recorder is not None:
            recorder.recording_screen = screen
            if recorder.status == RecorderStatus.Recording:
                print("Room {} is recording...".format(room))
                return False

        recorder.recording_cam = cam
        cam_source_name = self.__rtsp_to_str(cam)
        time_str = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
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
        # take a screenshot
        self.__take_screenshot(room, time_str, recorder)

        return True

    @async_func
    def __take_screenshot(self, room, time_str, recorder: RecorderSession):
        output_format = "output_{}_".format(room) + time_str
        recorder.thumbnail_file_path = recorder.folder + output_format + "." + OUTPUT_IMG_EXT
        time.sleep(2)
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            self.scene.screenshot(recorder.thumbnail_file_path)
        )
        print("Room {r} Take screen shot successfully at file path {p}".format(r=room, p=recorder.thumbnail_file_path))

    # switch cameras
    def switch_camera(self, room, cam):
        if not self.obs_connected:
            return False
        if room not in self.__sessions:
            print("Room {} not configure yet".format(room))
            return False
        recorder: RecorderSession = self.__sessions[room]
        if recorder is None:
            return False
        if recorder.status != RecorderStatus.Recording:
            print("Room {} not recording yet".format(room))
            return False
        if recorder.recording_cam is not None and recorder.recording_cam == cam:
            print("Room {r} CAM {c} is recording".format(r=room, c=cam))
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

    # start recording screen
    def start_recording_screen(self, room):
        if not self.obs_connected:
            return False
        if room not in self.__sessions:
            print("Room {} is not configured yet".format(room))
            return False
        recorder: RecorderSession = self.__sessions[room]
        if recorder is None:
            return False
        else:
            if recorder.status == RecorderStatus.Default:
                print("Room {} is not recording".format(room))
                return False
            if recorder.recording_screen:
                print("Room {} Screen is recording".format(room))
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

    # stop recording screen
    def stop_recording_screen(self, room):
        if not self.obs_connected:
            return False
        if room not in self.__sessions:
            print("Room {} not configure yet".format(room))
            return False
        recorder: RecorderSession = self.__sessions[room]
        if recorder is None:
            return False
        if not recorder.recording_screen:
            print("Room {}, Screen is not recording yet".format(room))
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

    # pause recording
    def pause_recording(self, room):
        return self.stop_recording(room, True)

    # stop recording
    def stop_recording(self, room, pause=False):
        if not self.obs_connected:
            return None
        if room not in self.__sessions:
            print("Room {} not configure yet!".format(room))
            return None
        recorder: RecorderSession = self.__sessions[room]
        if recorder.status.value < RecorderStatus.Recording.value:
            print("Room {}, not recording yet, please try again.".format(room))
            return None

        loop.run_until_complete(
            self.scene.stop_recording()
        )
        if pause:
            recorder.status = RecorderStatus.Paused
        else:
            recorder.status = RecorderStatus.Stopped

        data = self.__processing_file(room, recorder, pause)
        recorder.status = RecorderStatus.Finished
        return data

    def __processing_file(self, room, recorder: RecorderSession, pause=False):
        print("Starting processing file from room =", room)
        if recorder.record_file_path is not None:
            recorder.status = RecorderStatus.Processing

            # Set a timeout 20s
            timeout = time.time() + 20
            while True:
                if time.time() > timeout:
                    break
                time.sleep(1)
                if os.path.isfile(recorder.record_file_path):
                    break

            print("\n***********\nDone! file at path: ", recorder.record_file_path, "\n***********\n")

            duration, file_size = self.fetch_filesize(recorder.record_file_path)
            data = {
                "image_path": recorder.thumbnail_file_path,
                "video_path": recorder.record_file_path,
                "video_size": file_size,
                "video_duration": str(int(float(duration)))
            }
            return data

    # fetch recorded file info: duration & filesize
    @staticmethod
    def fetch_filesize(video_path):
        import json
        import subprocess

        try:
            result = subprocess.check_output(f'ffprobe -v quiet -print_format json -show_format "{video_path}"', shell=True).decode()
            print(result)
            format_info = json.loads(result)['format']
            print(format_info)

            duration = format_info['duration']
            file_size = format_info['size']
            return duration, file_size
        except Exception as e:
            print("fetch file info error:", e)
            return "0", "0"
