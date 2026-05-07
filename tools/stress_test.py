import epics
import time
import threading
import random
import statistics

TARGET_PV = 'TGT:MEOP:Voltage1_VC'
BURST_SIZE = 100  # Number of back-to-back updates


class StressBenchmark:
    def __init__(self):
        self.latencies = []
        self.event = threading.Event()
        self.start_time = None
        self.last_value = 0
        self.pv = epics.PV(TARGET_PV, callback=self.on_change)

    def on_change(self, value=None, **kw):
        if self.start_time is not None:
            end_time = time.perf_counter()
            self.latencies.append((end_time - self.start_time) * 1000)
            self.start_time = None
            self.event.set()

    def get_new_random(self):
        while True:
            new_val = random.uniform(0.1, 3.9)  # Stay away from hard limits
            if abs(new_val - self.last_value) >= 0.3:
                return new_val

    def run_stress_test(self):
        print(f"Connecting to {TARGET_PV}...")
        if not self.pv.wait_for_connection(timeout=5):
            print("Connection Failed.")
            return

        print(f"Starting Stress Test: {BURST_SIZE} rapid-fire updates...")
        self.last_value = self.pv.get()

        test_start = time.perf_counter()

        for i in range(BURST_SIZE):
            self.event.clear()
            target_val = self.get_new_random()
            self.last_value = target_val

            self.start_time = time.perf_counter()
            self.pv.put(target_val)

            # Wait for response, but with a shorter timeout for stress
            if not self.event.wait(timeout=0.5):
                print(f"!!! SATURATION REACHED at iteration {i} !!!")
                break

            # NO SLEEP HERE - We want to hit the IOC as fast as it responds

            if i % 10 == 0 and i > 0:
                recent_avg = sum(self.latencies[-10:]) / 10
                print(f"Progress: {i}/{BURST_SIZE} | Recent Latency Avg: {recent_avg:.3f} ms")

        test_end = time.perf_counter()
        total_time = test_end - test_start

        self.print_results(total_time)

    def print_results(self, total_time):
        if not self.latencies: return
        print(f"\n" + "=" * 30)
        print(f"STRESS TEST COMPLETE")
        print(f"=" * 30)
        print(f"Total Transactions: {len(self.latencies)}")
        print(f"Total Time:         {total_time:.3f} s")
        print(f"Throughput:         {len(self.latencies) / total_time:.2f} updates/sec")
        print(f"Average Latency:    {statistics.mean(self.latencies):.3f} ms")
        print(f"Median Latency:     {statistics.median(self.latencies):.3f} ms")
        print(f"Std Deviation:      {statistics.stdev(self.latencies):.3f} ms")
        print(f"Max Latency (Peak): {max(self.latencies):.3f} ms")
        print(f"=" * 30)


if __name__ == "__main__":
    StressBenchmark().run_stress_test()