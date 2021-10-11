import asyncio
import simpleobsws

loop = asyncio.get_event_loop()
ws = simpleobsws.obsws(host='127.0.0.1', port=9993, password='kick', loop=loop) 
# Every possible argument has been passed, but none are required. See lib code for defaults.

async def on_event(data):
    print('New event! Type: {} | Raw Data: {}'.format(data['update-type'], data)) # Print the event data. Note that `update-type` is also provided in the data

async def on_switchscenes(data):
    print('Scene switched to "{}". It has these sources: {}'.format(data['scene-name'], data['sources']))

async def make_request():
    await ws.connect()
    
    # We get the current OBS version. More request data is not required
    result = await ws.call('GetVersion') 
    if 'version' in result:
        print(result['version'])

try:
    loop.run_until_complete(make_request())
    # By not specifying an event to listen to, all events are sent to this callback.
    ws.register(on_event) 
    ws.register(on_switchscenes, 'SwitchScenes')
    
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

