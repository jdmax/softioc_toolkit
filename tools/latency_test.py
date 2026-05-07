import epics
import time
import threading
import random

TARGET_PV = 'TGT:MEOP:Voltage1_VC'
ITERATIONS = 50


class RandomBenchmark:
    def __init__(self):
        self.latencies = []
        self.event = threading.Event()
        self.start_time = None
        self.last_value = 0
        self.pv = epics.PV(TARGET_PV, callback=self.on_change)

    def on_change(self, value=None, **kw):
        if self.start_time is not None:
            end_time = time.perf_counter()
            latency = (end_time - self.start_time) * 1000
            self.latencies.append(latency)
            print(f"Update: {value:.4f} | Latency: {latency:.3f} ms")

            self.start_time = None
            self.event.set()

    def get_new_random(self):
        """Generates a random float between 0 and 4, at least 0.3 away from last_value."""
        while True:
            new_val = random.uniform(0, 4)
            if abs(new_val - self.last_value) >= 0.3:
                return new_val

    def run(self):
        print(f"Connecting to {TARGET_PV}...")
        if not self.pv.wait_for_connection(timeout=5):
            print("Connection Failed.")
            return

        self.last_value = self.pv.get()

        for i in range(ITERATIONS):
            self.event.clear()

            # 1. Get our random value
            target_val = self.get_new_random()
            self.last_value = target_val

            # 2. Benchmark the put
            self.start_time = time.perf_counter()
            self.pv.put(target_val)

            # 3. Wait for acknowledgement
            if not self.event.wait(timeout=2.0):
                print(f"Iteration {i}: Timed out (Value was {target_val:.4f})")
                self.start_time = None

            # Short sleep to prevent flooding the network
            time.sleep(0.1)

        if self.latencies:
            print(f"\n--- Final Stats ---")
            print(f"Average: {sum(self.latencies) / len(self.latencies):.3f} ms")
            print(f"Jitter (Max-Min): {max(self.latencies) - min(self.latencies):.3f} ms")


if __name__ == "__main__":
    bench = RandomBenchmark()
    bench.run()