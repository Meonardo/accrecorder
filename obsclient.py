import asyncio
import os
import time

from threading import Thread
from urllib.parse import urlparse
from simpleobsws import WebSocketClient, Request, IdentificationParameters
from recorder import RecorderStatus, RecorderSession, print, async_func


loop = asyncio.get_event_loop()
parameters = IdentificationParameters(ignoreNonFatalRequestChecks=False)
ws = WebSocketClient(url="ws://127.0.0.1:9991", password="amdox", identification_parameters=parameters)

MAIN_SCENE = 'MainScene'
AUDIO_INPUT_NAME = "Microphone"
SCREEN_SOURCE_NAME = 'Screen'

SCREEN_W = 1920
SCREEN_H = 1080
CAM_SCALE = 1 / 3

OUTPUT_FILE_EXT = 'mp4'
OUTPUT_IMG_EXT = 'png'


class SceneItemDevice:
    def __init__(self, obj: dict):
        self.name = str(obj['itemName'])
        self.id = str(obj['itemValue'])

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
        self.x = 0
        self.y = 0
        self.locked = False
        self.muted = False
        self.device: SceneItemDevice = None

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
        self.sources = None
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
    async def file_settings(folder, file_format):
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
            # fullscreen camera sceneItem
            if not screen:
                await cam_item.update_position_scale(0, 0, 1)
        if screen:
            screen_item = self.find_item(SCREEN_SOURCE_NAME)
            if screen_item is not None:
                await screen_item.set_visible(True)

        await self.__start_recording()

    async def stop_recording(self):
        for source in self.sources:
            await source.set_visible(False)

        await self.__stop_recording()

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
    def __retry(self, cam1, cam2, mic, monitor):
        retry_loop = asyncio.new_event_loop()
        retry_loop.run_until_complete(
            self.__reconnect_obs(cam1, cam2, mic, monitor)
        )
        retry_loop.close()

    async def __connect_obs(self, cam1, cam2, mic, monitor):
        if not self.obs_connected:
            try:
                await ws.connect()
                await ws.wait_until_identified()
                print("websocket connected")
                self.obs_connected = True
            except Exception as exc:
                print("websocket connection exception", exc)
                self.__retry(cam1, cam2, mic)
                return
        print("websocket connected, creating scene & sources...")
        # remove default mic capture
        await self.__remove_default_mic_aux()
        await self.__create_scene()
        # create sources & config them
        await self.__create_sources(cam1, cam2, monitor, mic)
        # register events
        # ws.register_event_callback(self.__on_events_change)

    async def __remove_default_mic_aux(self):
        input_list = await ws.call(Request("GetInputList"))
        if input_list.has_data():
            print("input list:", input_list.responseData["inputs"])
            targets = ["Mic/Aux", "Desktop Audio"]
            for t in targets:
                # mute first(there is a bug that remove input but the obs not remove its UI)
                r = await ws.call(Request("SetInputMute", {"inputName": t, "inputMuted": True}))
                r = await ws.call(Request("RemoveInput", {"inputName": t}))
                if r.ok():
                    print("remove input {} succeed".format(t))


    async def __on_events_change(self, type, data):
        print('New event! Type: {} | Raw Data: {}'.format(type, data))

    async def __reconnect_obs(self, cam1, cam2, mic, monitor):
        try:
            await ws.connect()
            await ws.wait_until_identified()
            self.obs_connected = True
            await self.__connect_obs(self, cam1, cam2, mic, monitor)
        except Exception as exc:
            print("websocket connection exception, reconnecting...", exc)
            await asyncio.sleep(3)
            await self.__reconnect_obs(cam1, cam2, mic, monitor)

    async def __create_scene(self):
        scene = await self.__find_scene(MAIN_SCENE)
        if scene is not None:
            # Remove exists scene
            await scene.delete()
        # in case of Obs not created the scene 
        await asyncio.sleep(1)
        # create scene
        r = await ws.call(Request('CreateScene', {"sceneName": MAIN_SCENE}))
        if not r.ok:
            print("Create Scene failed")
        # select the scene
        await ws.call(Request('SetCurrentProgramScene', {"sceneName": MAIN_SCENE}))

    async def __create_sources(self, cam1, cam2, monitor, mic):
        # create sceneItems
        await self.__create_screen_capture_input(monitor)
        await self.__create_audio_capture_input(mic)
        await self.__create_rtsp_input(cam1)
        await self.__create_rtsp_input(cam2)

        # refresh scene & sceneItems
        self.scene = await self.__find_scene(MAIN_SCENE)

        # config
        x = SCREEN_W - SCREEN_W * CAM_SCALE
        y = SCREEN_H - SCREEN_H * CAM_SCALE
        cam1_source_name = self.__rtsp_to_str(cam1)
        cam2_source_name = self.__rtsp_to_str(cam2)

        cam1_item = self.scene.find_item(cam1_source_name)
        if cam1_item is not None:
            await cam1_item.update_position_scale(x, y, CAM_SCALE)
        cam2_item = self.scene.find_item(cam2_source_name)
        if cam2_item is not None:
            await cam2_item.update_position_scale(x, y, CAM_SCALE)
        screen_item = self.scene.find_item(SCREEN_SOURCE_NAME)
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

    async def __create_screen_capture_input(self, monitor):
        print("creating screen capture device: ", monitor)
        input_kind = 'monitor_capture'

        all_types = await ws.call(Request('GetSourceTypesList'))
        if all_types.ok() and all_types.has_data():
            all_types = all_types.responseData
            all_types = all_types['types']
            type_ids = [type['typeId'] for type in all_types if 'typeId' in type]
            if 'monitor_capture' in type_ids:
                input_kind = "monitor_capture"
            elif 'display_capture' in type_ids:
                input_kind = "display_capture"

        input_name = SCREEN_SOURCE_NAME
        obj = {
            "inputKind": input_kind,
            "inputName": input_name,
            "sceneName": MAIN_SCENE,
        }
        r = await ws.call(Request("CreateInput", obj))
        if r.ok():
            print("create screen capture {0} input succeed".format(monitor))

        r = await ws.call(Request("GetInputPropertiesListPropertyItems", {"inputName": input_name, "propertyName": "monitor"}))
        if r.ok() and r.has_data():
            propertyItems = r.responseData['propertyItems']
            devices = [SceneItemDevice(item) for item in propertyItems]
            for d in devices:
                if d.id == monitor:
                    # update device
                    inputSettings = {
                        "monitor": int(d.id),
                        "cursor": True,
                        # "method": "",
                    }
                    r = await ws.call(Request("SetInputSettings", {"inputName": input_name, "inputSettings": inputSettings}))
                    if r.ok():
                        print("update screen capture device to {0} successffully".format(d.name))
                    break

    async def __create_audio_capture_input(self, mic):
        print("creating audio capture device: ", mic)
        input_kind = "wasapi_input_capture"
        input_name = AUDIO_INPUT_NAME
        obj = {
            "inputKind": input_kind,
            "inputName": input_name,
            "sceneName": MAIN_SCENE,
        }
        r = await ws.call(Request("CreateInput", obj))
        if r.ok():
            print("create {0} input succeed".format(mic))
        
        r = await ws.call(Request("GetInputPropertiesListPropertyItems", {"inputName": input_name, "propertyName": "device_id"}))
        if r.ok() and r.has_data():
            propertyItems = r.responseData['propertyItems']
            devices = [SceneItemDevice(item) for item in propertyItems]
            for d in devices:
                if d.name == mic:
                    # update device
                    inputSettings = {
                        "device_id": d.id
                    }
                    r = await ws.call(Request("SetInputSettings", {"inputName": input_name, "inputSettings": inputSettings}))
                    if r.ok:
                        print("update audio input device to {0} successffully".format(d.name))
                    break
    
    async def __create_rtsp_input(self, rtsp):
        print("creating gst-rtsp input: ", rtsp)
        input_kind = "gstreamer-source"
        input_name = self.__rtsp_to_str(rtsp)
        pipeline = "uridecodebin uri={0} name=bin ! queue ! video.".format(rtsp)
        input_settings = {
            "pipeline": pipeline,
            "sync_appsink_video": False,
            "sync_appsink_audio": False,
            "disable_async_appsink_video": True,
            "disable_async_appsink_audio": True,
            "stop_on_hide": False,
        }
        obj = {
            "inputKind": input_kind,
            "inputName": input_name,
            "sceneName": MAIN_SCENE,
            "inputSettings": input_settings,
        }
        r = await ws.call(Request("CreateInput", obj))
        if r.ok():
            print("create gst-rtsp {0} source succeed".format(rtsp))

    # configure
    def configure(self, room, cam1, cam2, mic, monitor):
        # create recorder session
        self.__create_recorder(room)
        loop.run_until_complete(
            self.__connect_obs(cam1, cam2, mic, monitor)
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
            self.scene.file_settings(recorder.folder, output_format),
            self.scene.start_recording(cam_source_name, screen)
        ]
        loop.run_until_complete(
            asyncio.gather(*tasks)
        )

        recorder.status = RecorderStatus.Recording
        # take a screenshot
        self.__take_screenshot(room, output_format, recorder)

        return True

    @async_func
    def __take_screenshot(self, room, output_format, recorder: RecorderSession):
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
