import sys, time
sys.path.insert(0, "/data/inverter-control")
from inverter_control.victron import VictronDBus

v = VictronDBus()
for i in range(5):
    t0 = time.time()
    
    d = v.get_system_data()
    m = v.get_mppt_data()
    p = v.get_tasmota_pv_power()
    s = v.get_battery_chain_socs()
    inv = v.get_inverter_state()
    e = v.get_ess_mode()
    b = v.get_all_batteries()
    mc = v.get_mppt_chargers()
    
    elapsed = time.time() - t0
    print(f"  cycle {i}: total={elapsed:.3f}s  gt={d.get('gt',0)}")
print("Done")
