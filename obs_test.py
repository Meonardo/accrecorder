import asyncio
import simpleobsws

loop = asyncio.get_event_loop()
ws = simpleobsws.obsws(host='192.168.5.45', port=9999, password='kick', loop=loop)


async def on_event(data):
    print('New event! Type: {} | Raw Data: {}'.format(data['update-type'], data))


async def make_request():
    await ws.connect()
    
    # We get the current OBS version. More request data is not required
    result = await ws.call('GetVersion') 
    if 'version' in result:
        print(result['version'])

    # r = await ws.call('GetSourceSettings', {'sourceName': 'CAM', 'sourceType': 'ffmpeg_source'})
    # print(r)
    # return

    scene_name = 'MainScene1'
    source_name = "Screen"
    scene = await find_scene(scene_name)
    if scene is not None:
        source = find_source(scene, scene_name)
        if source is not None:
            pass
    else:
        await create_scene(scene_name)

        screen = await create_screen_capture(scene_name, source_name)
        print("Create Source:", screen)

        cam1 = "rtsp://192.168.5.160/1"
        rtsp = await create_rtsp_source(scene_name, "CAM1", cam1)
        print("Create Source:", rtsp)


async def find_scene(name: str):
    scenes = await ws.call('GetSceneList')
    if 'scenes' in scenes:
        scenes = scenes['scenes']
        for scene in scenes:
            if scene['name'] == name:
                print("Current Scene:", scene)
                return scene


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
    obj = {
        "sceneName": scene_name,
        "sourceName": "screen",
        "sourceKind": "monitor_capture",
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
        "cx": width,
        "cy": height,
        "locked": True,
        "name": source_name,
        "render": True,
        "x": x,
        "y": y,
        "source_cx": 1920,
        "source_cy": 1080,
        "is_local_file": False,
        "hw_decode": True,
        "input": rtsp,
        "input_format": "rtsp",
        "buffering_mb": 0.2
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

