import psutil
from softioc import builder
from .base_device import BaseDevice


class Device(BaseDevice):
    """
    Monitor total resource intensity specifically for master_ioc.py instances.
    Calculates aggregate CPU and Memory for all IOCs managed in screens,
    excluding the ioc_heath IOC itself (which can use up to 3% CPU).
    """

    def __init__(self, device_name, settings):
        # Initialize the cache for process objects
        self._proc_cache = {}
        super().__init__(device_name, settings)

    def _create_pvs(self):
        """Create aggregate monitoring PVs"""
        # Aggregate CPU usage of all master_ioc instances
        self.pvs['TOTAL_IOC_CPU'] = builder.aIn(
            f'TOTAL_IOC_CPU',
            initial_value=0,
            EGU='%',
            PREC=2,
            **self.sevr
        )

        # Aggregate Memory usage of all master_ioc instances
        self.pvs['TOTAL_IOC_MEM'] = builder.aIn(
            f'TOTAL_IOC_MEM',
            initial_value=0,
            EGU='MB',
            PREC=1,
            **self.sevr
        )

        # Count of running master_ioc processes found
        self.pvs['IOC_COUNT'] = builder.longIn(
            f'IOC_COUNT',
            initial_value=0,
            **self.sevr
        )

    def _create_connection(self):
        """No hardware connection; returns a list of target keywords for filtering"""
        return ['python', 'master_ioc.py']

    async def do_reads(self):
        total_cpu = 0.0
        total_mem_rss = 0.0
        found_pids = set()

        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    pid = proc.info['pid']

                    if 'python' in (proc.info['name'] or '').lower():
                        cmdline = proc.info['cmdline'] or []
                        if any('master_ioc.py' in arg for arg in cmdline):
                            if any('ioc_health' in arg for arg in cmdline):
                                continue
                            # If we don't have this PID in cache, add it
                            if pid not in self._proc_cache:
                                self._proc_cache[pid] = proc

                            # Query the cached object for CPU since last call
                            # Use the cached object, NOT the one from process_iter
                            cached_proc = self._proc_cache[pid]
                            total_cpu += cached_proc.cpu_percent(interval=None)
                            total_mem_rss += cached_proc.memory_info().rss / (1024 * 1024)
                            found_pids.add(pid)

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Clean up the cache for IOCs that were closed
            self._proc_cache = {pid: p for pid, p in self._proc_cache.items() if pid in found_pids}

            self.pvs['TOTAL_IOC_CPU'].set(total_cpu)
            self.pvs['TOTAL_IOC_MEM'].set(total_mem_rss)
            self.pvs['IOC_COUNT'].set(len(found_pids))
            return True

        except Exception as e:
            print(f"Error in aggregate read: {e}")
            return False