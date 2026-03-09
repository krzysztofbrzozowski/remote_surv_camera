"""INA219 Power Monitor - measures voltage, current, and power consumption."""
import logging
import time

logger = logging.getLogger(__name__)


class INA219PowerMonitor:
    """INA219 power monitoring module for measuring voltage, current, and power.
    
    This module interfaces with the INA219 current/power sensor via I2C to provide
    real-time monitoring of load voltage, current draw, power consumption, and
    estimated battery percentage.
    """
    
    def __init__(self, i2c_bus=1, addr=0x43):
        """Initialize INA219 power monitor.
        
        Args:
            i2c_bus: I2C bus number (default: 1)
            addr: I2C address (default: 0x43)
        """
        self._available = False
        self._ina219 = None
        self._last_data = {}
        self._last_poll = 0
        self._poll_interval = 2.0  # seconds between polls
        
        try:
            import smbus2
            
            # INA219 register addresses
            self._REG_CONFIG = 0x00
            self._REG_SHUNTVOLTAGE = 0x01
            self._REG_BUSVOLTAGE = 0x02
            self._REG_POWER = 0x03
            self._REG_CURRENT = 0x04
            self._REG_CALIBRATION = 0x05
            
            # Initialize I2C bus
            self.bus = smbus2.SMBus(i2c_bus)
            self.addr = addr
            
            # Calibration values for 16V 5A range with 0.01 ohm shunt
            self._cal_value = 26868
            self._current_lsb = 0.1524  # mA per bit
            self._power_lsb = 0.003048  # W per bit
            
            # Configure the INA219
            self._configure_16v_5a()
            
            # Test read to verify connection
            voltage = self._read_bus_voltage()
            if voltage is not None:
                self._available = True
                logger.info(f"INA219 Power Monitor detected at 0x{addr:02x}")
            else:
                logger.warning("INA219 not responding")
                
        except ImportError:
            logger.info("INA219 not available: smbus2 not installed")
        except Exception as e:
            logger.info(f"INA219 not available: {e}")
    
    @property
    def available(self) -> bool:
        """Check if INA219 is available and responding."""
        return self._available
    
    def _read_register(self, address: int) -> int:
        """Read a 16-bit register from the INA219."""
        try:
            data = self.bus.read_i2c_block_data(self.addr, address, 2)
            return (data[0] * 256) + data[1]
        except Exception as e:
            logger.debug(f"INA219 read error: {e}")
            return None
    
    def _write_register(self, address: int, data: int):
        """Write a 16-bit value to an INA219 register."""
        try:
            temp = [0, 0]
            temp[1] = data & 0xFF
            temp[0] = (data & 0xFF00) >> 8
            self.bus.write_i2c_block_data(self.addr, address, temp)
        except Exception as e:
            logger.debug(f"INA219 write error: {e}")
    
    def _configure_16v_5a(self):
        """Configure INA219 for 16V 5A measurement range."""
        # Bus Voltage Range: 16V
        # Gain: /2 (80mV range)
        # Bus ADC Resolution: 12-bit, 32 samples
        # Shunt ADC Resolution: 12-bit, 32 samples
        # Mode: Continuous shunt and bus voltage
        
        bus_voltage_range = 0x00  # 16V
        gain = 0x01  # /2, 80mV
        bus_adc = 0x0D  # 12-bit, 32 samples
        shunt_adc = 0x0D  # 12-bit, 32 samples
        mode = 0x07  # Continuous
        
        config = (bus_voltage_range << 13 | gain << 11 | 
                 bus_adc << 7 | shunt_adc << 3 | mode)
        
        self._write_register(self._REG_CALIBRATION, self._cal_value)
        self._write_register(self._REG_CONFIG, config)
    
    def _read_bus_voltage(self) -> float:
        """Read bus voltage in volts."""
        value = self._read_register(self._REG_BUSVOLTAGE)
        if value is None:
            return None
        return (value >> 3) * 0.004
    
    def _read_shunt_voltage(self) -> float:
        """Read shunt voltage in millivolts."""
        value = self._read_register(self._REG_SHUNTVOLTAGE)
        if value is None:
            return None
        if value > 32767:
            value -= 65535
        return value * 0.01
    
    def _read_current(self) -> float:
        """Read current in milliamps."""
        value = self._read_register(self._REG_CURRENT)
        if value is None:
            return None
        if value > 32767:
            value -= 65535
        return value * self._current_lsb
    
    def _read_power(self) -> float:
        """Read power in watts."""
        value = self._read_register(self._REG_POWER)
        if value is None:
            return None
        if value > 32767:
            value -= 65535
        return value * self._power_lsb
    
    def _calculate_percentage(self, voltage: float) -> float:
        """Calculate battery percentage from voltage.
        
        Assumes LiPo battery: 3.0V (0%) to 4.2V (100%)
        Adjust these values based on your battery type.
        """
        if voltage is None:
            return None
        
        v_min = 3.0  # Minimum battery voltage (0%)
        v_max = 4.2  # Maximum battery voltage (100%)
        
        percentage = ((voltage - v_min) / (v_max - v_min)) * 100
        return max(0, min(100, percentage))  # Clamp to 0-100%
    
    def get_data(self) -> dict:
        """Get current power monitoring data.
        
        Returns dict with:
            - load_voltage_v: Load voltage in volts
            - current_a: Current in amps
            - power_w: Power in watts
            - battery_percent: Estimated battery percentage
        """
        if not self._available:
            return {}
        
        now = time.time()
        if now - self._last_poll < self._poll_interval:
            return self._last_data
        
        self._last_poll = now
        
        # Read all values
        bus_voltage = self._read_bus_voltage()
        current_ma = self._read_current()
        power = self._read_power()
        
        data = {}
        
        if bus_voltage is not None:
            data["load_voltage_v"] = round(bus_voltage, 3)
            
            # Calculate battery percentage
            percent = self._calculate_percentage(bus_voltage)
            if percent is not None:
                data["battery_percent"] = round(percent, 1)
        
        if current_ma is not None:
            data["current_a"] = round(current_ma / 1000.0, 3)
        
        if power is not None:
            data["power_w"] = round(power, 3)
        
        self._last_data = data
        return data
