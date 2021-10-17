import asyncio
from os import name
import simpleobsws
import sys

loop = asyncio.get_event_loop()
ws = simpleobsws.obsws(host='127.0.0.1', port=9999, password='kick', loop=loop)


MAIN_SCENE = 'MainScene'

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
        self.is_current = is_current

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

async def on_event(data):
    print('New event! Type: {} | Raw Data: {}'.format(data['update-type'], data))


async def make_request():
    # 连接
    await ws.connect()
    
    result = await ws.call('GetVersion') 
    if 'obs-websocket-version' in result:
        print(result['obs-websocket-version'])

    # r = await ws.call('GetSceneItemProperties', {'item': 'CAM1', 'scene-name': 'MainScene'})
    # print("SceneItemProperties", r)
    # r = await ws.call('GetTransitionList')
    # print("TransitionList", r)
    # return

    scene_name = MAIN_SCENE
    source_name = "Screen"
    scene = await find_scene(scene_name)
    if scene is not None:
        for source in scene.sources:
            await source.reset()
    else:
        # 创建当前 scene
        await create_scene(scene_name)

    # 选中当前 Scene
    r = await ws.call('SetCurrentScene', {"scene-name": MAIN_SCENE})
    print("Switch to scene", MAIN_SCENE)

    # 创建 sources
    screen = await create_screen_capture(scene_name, source_name)
    print("Create Source:", screen)

    cam1 = "/Users/meonardo/Downloads/video1.mp4"
    rtsp = await create_file_source(scene_name, "CAM1", cam1)
    print("Create Source:", rtsp)

    cam2 = "/Users/meonardo/Downloads/video2.mp4"
    rtsp2 = await create_file_source(scene_name, "CAM2", cam2, False)
    print("Create Source:", rtsp2)

    # 刷新 scene 对象
    scene = await find_scene(scene_name)

    screen = scene.find_item("Screen")
    if screen is not None:
        await screen.set_visible(False)

    scale = 1/3
    x = 1920 - 1920 * scale
    y = 1080 - 1080 * scale

    cam2 = scene.find_item("CAM2")
    if cam2 is not None:
        r = await cam2.update_position_scale(x, y, scale)
        await cam2.set_visible(False)

    await asyncio.sleep(3)

    cam1 = scene.find_item("CAM1")
    if cam1 is not None:
        r = await cam1.update_position_scale(x, y, scale)
        print("Update SourceSettings:", r)

        await asyncio.sleep(5)
        r = await cam1.update_position_scale(0, 0, 1)
        print("Update SourceSettings:", r)


async def find_scene(name: str):
    scenes = await ws.call('GetSceneList')
    if 'scenes' in scenes:
        current = scenes['current-scene']
        scenes = scenes['scenes']
        for scene in scenes:
            if scene['name'] == name:
                items = [SceneItem(name, item) for item in scene['sources']]
                s = Scene(name, items, is_current=(name==current))
                return s


def find_source(scene: dict, name: str):
    if 'sources' in scene:
        for source in scene['sources']:
            if source['name'] == name:
                print("Screen Source: ", source)
                return source


async def create_scene(name: str):
    r = await ws.call('CreateScene', {"sceneName": name})
    print("Create Scene:", r)


async def create_screen_capture(scene_name: str, source_name: str):
    source_settings = {
        "alignment": 5,
        "cx": 1920,
        "cy": 1080,
        "locked": True,
        "name": source_name,
        "render": True,
        "x": 0,
        "y": 0,
        "source_cx": 1920,
        "source_cy": 1080
    }
    type = "monitor_capture"
    if sys.platform == "darwin":
        type = "display_capture"
    obj = {
        "sceneName": scene_name,
        "sourceName": source_name,
        "sourceKind": type,
        "sourceSettings": source_settings
    }
    return await ws.call('CreateSource', obj)


async def create_rtsp_source(scene_name: str, source_name: str, rtsp: str):
    width = 1920 * (1 / 3)
    height = 1080 * (1 / 3)
    x = 1920 - width
    y = 1080 - height
    source_settings = {
        "alignment": 5,
        "width": width,
        "height": height,
        "locked": True,
        "name": source_name,
        "render": True,
        "x": x,
        "y": y,
        "source_cx": 1920,
        "source_cy": 1080,
        "is_local_file": True,
        "hw_decode": True,
        "input": rtsp,
        "input_format": "file",
        "local_file": rtsp,
        "buffering_mb": 1
    }
    obj = {
        "sceneName": scene_name,
        "sourceName": source_name,
        "sourceKind": "ffmpeg_source",
        "sourceSettings": source_settings
    }
    return await ws.call('CreateSource', obj)


async def create_file_source(scene_name: str, source_name: str, file_path: str, visible=True):
    source_settings = {
        "alignment": 5,
        "locked": True,
        "name": source_name,
        "source_cx": 1920,
        "source_cy": 1080,
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


try:
    loop.run_until_complete(make_request())
    # By not specifying an event to listen to, all events are sent to this callback.
    ws.register(on_event)
    loop.run_forever()

except (KeyboardInterrupt, Exception) as e:
    print("Received exception: ", e)
    pass
finally:
    print("Stopping now!")
    loop.run_until_complete(
        ws.disconnect()
    )
    loop.close()

