#!/usr/bin/env python3
"""Test INA219 power monitoring integration."""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
from sources.ina219_power import INA219PowerMonitor

def main():
    print("INA219 Power Monitor Test")
    print("=" * 50)
    
    monitor = INA219PowerMonitor()
    
    if not monitor.available:
        print("❌ INA219 not available")
        print("   - Check I2C connection")
        print("   - Verify address (default: 0x43)")
        print("   - Install smbus2: pip install smbus2")
        return
    
    print("✅ INA219 detected and ready\n")
    
    try:
        while True:
            data = monitor.get_data()
            
            if data:
                print(f"Load Voltage:  {data.get('load_voltage_v', 'N/A'):6.3f} V")
                print(f"Current:       {data.get('current_a', 'N/A'):6.3f} A")
                print(f"Power:         {data.get('power_w', 'N/A'):6.3f} W")
                print(f"Battery:       {data.get('battery_percent', 'N/A'):5.1f}%")
                print("")
            else:
                print("No data available")
            
            time.sleep(2)
            
    except KeyboardInterrupt:
        print("\nTest stopped")

if __name__ == "__main__":
    main()
