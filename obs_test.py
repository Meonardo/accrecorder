import asyncio
import simpleobsws

loop = asyncio.get_event_loop()
ws = simpleobsws.obsws(host='192.168.5.186', port=9999, password='kick', loop=loop)


async def on_event(data):
    print('New event! Type: {} | Raw Data: {}'.format(data['update-type'], data))


async def make_request():
    await ws.connect()
    
    # We get the current OBS version. More request data is not required
    result = await ws.call('GetVersion') 
    if 'version' in result:
        print(result['version'])

    scenes = await ws.call('GetSceneList')
    if 'current-scene' in scenes:
        current_scene = scenes['current-scene']
        print("Current Scene:", current_scene)
    print(scenes)

    new_scene = 'new-scene'
    r = await ws.call('CreateScene', {"sceneName": new_scene})
    print(r)

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

