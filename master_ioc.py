# J. Maxwell 2023
from softioc import softioc, builder, asyncio_dispatcher
import asyncio
import yaml
import argparse
import importlib
import datetime
import ioc_common


async def main():
    """
    Run an IOC: load settings, create dispatcher, set name, do boilerplate,
    loop on device coroutine and start interactive interface
    Device to be set up comes from command line argument choosing option from settings file
    """
    ioc, settings = load_settings()

    ioc_common.apply_ca_env(settings)

    dispatcher = asyncio_dispatcher.AsyncioDispatcher()
    device_name = settings['general']['prefix']
    builder.SetDeviceName(device_name)

    d = DeviceIOC(device_name, ioc, settings)
    builder.LoadDatabase()
    softioc.iocInit(dispatcher)

    async def loop():
        while True:
            await d.loop()

    dispatcher(loop)  # put functions to loop in here
    softioc.interactive_ioc(globals())


class DeviceIOC():
    """Set up PVs for a given device IOC, run thread to interact with device
    """

    def __init__(self, device_name, ioc, settings):
        '''
        Arguments:
            device_name: name of device for PV prefix
            settings: dict of device settings
            records: dict of record settings
        '''
        # Import the device module
        self.module = importlib.import_module(settings[ioc]['module'])
        self.delay = settings[ioc]['delay']
        self.now = datetime.datetime.now()
        ioc_settings = settings[ioc]
        records = ioc_settings.get('records', {}) # sets records from settings file, if they exist

        # Create device instance
        self.device = self.module.Device(device_name, ioc_settings)
        self.device.connect()

        # Create timestamp PV
        self.pv_time = builder.aIn(f"MAN:{ioc}_time")
        self.pv_time.set(datetime.datetime.now().timestamp())

        # Apply record settings, if they exist for the PV
        for name, entry in self.device.pvs.items():
            if name in records:
                for field, value in records[name].items():
                    setattr(self.device.pvs[name], field, value)

    async def loop(self):
        """Read indicator PVS from controller channels.
        """
        await asyncio.sleep(self.delay)
        if await self.device.do_reads():   # get new readings from device and set into PVs
            self.pv_time.set(datetime.datetime.now().timestamp())   # set time of last successful update

def load_settings():
    """Load device settings from YAML settings file.
    Argument parser allows '-s' to give a different folder, '-i' tells which IOC to run"""

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", help="Settings file folder, default is here.")
    parser.add_argument("-i", help="Name of IOC to start")
    args = parser.parse_args()
    folder = args.s if args.s else '.'

    with open(f'{folder}/settings.yaml') as f:  # Load settings from YAML files
        settings = yaml.load(f, Loader=yaml.FullLoader)
    print(f"Loaded device settings from {folder}/settings.yaml.")

    ioc_list = list(settings.keys())
    ioc_list.remove('general')

    if args.i:
        ioc = args.i
    else:
        print("Select IOC to run from these entries in settings file using -i flag:")
        [print(f"  {x}") for x in ioc_list]
        exit()
    if ioc not in ioc_list:
        print("Given IOC not in settings file. Select from these:")
        [print(f"  {x}") for x in ioc_list]
        exit()

    return ioc, settings

if __name__ == "__main__":
    asyncio.run(main())