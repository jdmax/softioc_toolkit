# J. Maxwell 2023
import yaml
from softioc import builder, alarm
import aioca
import asyncio


class Device():
    """
    SoftIOC to handle status changes. Makes PVs to control changes of state and species.
    """

    def __init__(self, device_name, settings):
        """
        Make control PVs for status changes
        """
        self.settings = settings
        self.device_name = device_name

        with open('states.yaml') as f:  # Load states from YAML config file
            self.states = yaml.safe_load(f)

        self.pvs = {}
        self.status = self.states['options']['status']
        self.species = self.states['options']['species']

        self.pvs['status'] = builder.mbbOut('status', *self.status, on_update_name=self.stat_update)  # come from states.yaml
        self.pvs['species'] = builder.mbbOut('species', *self.species, on_update_name=self.stat_update)

        prod_states = ['Not Ready', 'Emptying', 'Empty', 'Filling', 'Full']
        self.pvs['production'] = builder.mbbIn('production', *prod_states)

        flag_states = ['Empty', 'Cu-Sn', 'Carbon']
        self.pvs['flag'] = builder.mbbIn('Flag_state', *flag_states)



    async def stat_update(self, i, pv):
        """
        Multiple Choice PV has changed for the state or species. Go through and caput changes from states file.
        """
        j = self.pvs['status'].get()
        k = self.pvs['species'].get()
        status = self.states['options']['status'][j]
        species = self.states['options']['species'][k]

        print("Changing status to", status, species)

        group = []
        for pv in self.states[status]:  # set values and alarms for this state. Adds all puts to a group and runs concurrently.
            if isinstance(self.states[status][pv][species], list):
                group.append(self.try_put(pv+'.HIHI', self.states[status][pv][species][0]))
                group.append(self.try_put(pv+'.HIGH', self.states[status][pv][species][1]))
                group.append(self.try_put(pv+'.LOW', self.states[status][pv][species][2]))
                group.append(self.try_put(pv+'.LOLO', self.states[status][pv][species][3]))
            else:
                group.append(self.try_put(pv, self.states[status][pv][species]))
        await asyncio.gather(*group)   # Run group concurrently

        # write out to file
        last = {'status': j, 'species': k}
        with open('last.yaml', 'w') as f:  # Dump this setting to file
            yaml.dump(last, f)

        print("Change done.", status, species)

    async def try_put(self, pv, value):
        try:
            await aioca.caput(self.device_name + ":" + pv, value)
        except aioca.CANothing as e:
            print("Put error:", e, self.device_name + ":" + pv, value)

    async def connect(self):
        '''Restore state to last used, or default if none'''
        try:
            with open('last.yaml') as f:  # Load last settings from yaml
                last = yaml.safe_load(f)
        except FileNotFoundError:
            last = {'status': 0, 'species': 0}

        for pv in last:      # set to PVs
            self.pvs[pv].set(last[pv])

        print('Restored previous state:', last)

    async def do_reads(self):
        '''Read status from other PVs to determine production status'''
        if self.settings['prod_pv']:
            try:
                group = []
                curr = {}    # dict of current status keyed on PV name
                for pvname in self.settings['full_status']:   # read PVs need to determine conditions
                    group.append(self.a_get(curr, pvname))
                group.append(self.a_get(curr, 'TGT:BTARG:Flag_MI'))
                group.append(self.a_get(curr, 'TGT:BTARG:Flag_pos_1'))   # left flag
                group.append(self.a_get(curr, 'TGT:BTARG:Flag_pos_2'))   # right flag
                await asyncio.gather(*group)

                if curr['TGT:BTARG:Flag_MI'] < curr['TGT:BTARG:Flag_pos_1'] + 1:
                    self.pvs['flag'].set(1)
                elif curr['TGT:BTARG:Flag_MI'] > curr['TGT:BTARG:Flag_pos_2'] - 1:
                    self.pvs['flag'].set(2)
                else:
                    self.pvs['flag'].set(0)

                stat = self.status[self.pvs['status'].get()]
                spec = self.species[self.pvs['species'].get()]

                for pv, l in self.states['options']['thresholds'][spec]['Standby'].items():
                    if l[0] < curr[pv] < l[1]:   # if any of these are between values, send to standby
                        await aioca.caput('TGT:BTARG:status', '5')  # Set to standby

                # Check to see if the applicable conditions are satisfied
                satisfied = True
                if "Emptying" in stat or "Empty" in stat:
                    for pv, l in self.states['options']['thresholds'][spec]['Empty'].items():
                        if not l[0] < curr[pv] < l[1]:
                            satisfied = False
                elif "Filling" in stat:
                    for pv, l in self.states['options']['thresholds'][spec]['Filling'].items():
                        if not l[0] < curr[pv] < l[1]:
                            satisfied = False
                elif "Full" in stat:
                    for pv, l in self.states['options']['thresholds'][spec]['Full'].items():
                        if not l[0] < curr[pv] < l[1]:
                            satisfied = False

                # Applying states and changes based on conditions
                if 'Emptying' in stat:
                    if satisfied:
                        await aioca.caput('TGT:BTARG:status', '2')  # Set to empty
                    else:
                        self.pvs['production'].set(1)  # Emptying
                elif 'Empty' in stat:
                    if satisfied:
                        self.pvs['production'].set(2)  # Empty
                    else:
                        self.pvs['production'].set(0)    # Not Ready, something is awry
                elif 'Filling' in stat:
                    if satisfied:  # if satisfied, then we have reached full condition
                        await aioca.caput('TGT:BTARG:status', '4')  # Set to full
                    else:
                        self.pvs['production'].set(3)  # Filling
                elif 'Full' in stat:
                    if satisfied:
                        self.pvs['production'].set(4)  # Full
                    else:
                        self.pvs['production'].set(0)    # Not Ready, something is awry
                else:
                    self.pvs['production'].set(0)    # Not Ready

            except aioca.CANothing as e:
                print("Caget error:", e)
                self.pvs['production'].set(4, severity=2, alarm=alarm.STATE_ALARM)
                self.pvs['flag'].set_alarm(severity=2, alarm=alarm.STATE_ALARM)
            except Exception as e:
                print("Production status determination error:", e)
                self.pvs['production'].set(4, severity=2, alarm=alarm.STATE_ALARM)
                self.pvs['flag'].set_alarm(severity=2, alarm=alarm.STATE_ALARM)

        return True

    async def a_get(self, dict, pv):
        '''Put pv status from aioca get in passed dict'''
        dict[pv] = await aioca.caget(pv)
