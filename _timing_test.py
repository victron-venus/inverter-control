import sys, time
sys.path.insert(0, "/data/inverter-control")
from inverter_control.victron import VictronDBus

v = VictronDBus()
last_gt = None
for i in range(10):
    t0 = time.time()
    d = v.get_system_data()
    gt = d.get("gt", 0)
    elapsed = time.time() - t0
    changed = "*" if gt != last_gt else ""
    print(f"  cycle {i}: gt={gt:6d}  g1={d['g1']:5d}  g2={d['g2']:5d}  took={elapsed:.3f}s {changed}")
    last_gt = gt
    time.sleep(1)
print("Done")
