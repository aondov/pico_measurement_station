import wifi
import socketpool
import os
import analogio
import math
import digitalio
import board
import time
import rtc
import json
import adafruit_ntp
import adafruit_datetime as datetime
import adafruit_hcsr04

# WiFi details
SSID = os.getenv('WIFI_SSID')
PASSWORD = os.getenv('WIFI_PASSWORD')

# NTP details
NTP_SERVER = os.getenv('NTP_SERVER')
TZ = os.getenv('TIMEZONE')

# TFTP server details
TFTP_SERVER = os.getenv('TFTP_SERVER')
TFTP_PORT = os.getenv('TFTP_PORT')

# Enable/disable printing messages to console
PRINT_OUTPUT = True

# Increase output verbosity
VERBOSE = True

# Initialize the built-in LED on Pico board and turn it off
led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT
led.value = False


# Indicate error state on Pico - 10 consecutive blinks on the built-in LED
def indicate_error() -> None: 
    for i in range(22):
        led.value = not led.value
        time.sleep(0.5)
    
    
# Turn on/off the built-in LED
def led_light(switch) -> None:
    global led
    led.value = switch


# Print log message in console
def logprint(sev: str, service: str, message: str) -> None:
    if not PRINT_OUTPUT:
        return
    
    if sev.lower() == "s":
        print(f"({service.lower()}) [SUCCESS]: {message}")
    elif sev.lower() == "e":
        print(f"({service.lower()}) [ERROR]: {message}")
    else:
        print(f"({service.lower()}) [INFO]: {message}")


# Get formatted time from the input struct time 
def get_format_time(current_time: struct_time) -> str:
    try:
        formatted_time = "{:04}-{:02}-{:02} {:02}:{:02}:{:02}".format(
            current_time[0], current_time[1], current_time[2], 
            current_time[3], current_time[4], current_time[5])
        
        return formatted_time
    except Exception as e:
        logprint("e", "ntp", str(e))
        indicate_error(led)
        return None
    
    
# Synchronize and/or initialize internal RTC from Pico with an external NTP server
def sync_ntp_to_rtc(pool: socketpool.SocketPool) -> None:
    try:
        # Retrieve current time from an external NTP server
        ntp = adafruit_ntp.NTP(pool, server=NTP_SERVER, tz_offset=TZ)
        ntp_time = ntp.datetime
        
        # Set the internal RTC time
        rtc.RTC().datetime = ntp_time
        
        if VERBOSE:
            logprint("s", "ntp", f"RTC updated from NTP: {get_format_time(ntp_time)}")
    except Exception as e:
        logprint("e", "ntp", str(e))
        indicate_error()
        return
    
    
# Connect Pico to WiFi
def connect_wifi() -> socketpool.SocketPool:
    try:
        # Connect to Wi-Fi
        logprint("i", "wifi", "Connecting to Wi-Fi...")
        wifi.radio.connect(SSID, PASSWORD)
        logprint("s", "wifi", "Connected!")

        if VERBOSE:
            logprint("i", "wifi", f"IP Address: {wifi.radio.ipv4_address}")

        # Create a network socket pool
        return socketpool.SocketPool(wifi.radio)
    except Exception as e:
        logprint("e", "wifi", str(e))
        indicate_error()
        return None


# Send log file to a TFTP server
def send_log_file(pool: socketpool.SocketPool, server_ip: str, input_data: str, filename: str) -> None:
    try:
        err_flag = False
        
        data = input_data.replace("'", '"')
        
        logprint("i", "tftp", "Sending measurement data...")

        # Create socket for network communication
        sock = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)
        
        # Set timeout for connection
        sock.settimeout(5)

        if VERBOSE:
            curr_time = get_format_time(time.localtime())
            logprint("i", "tftp", f"Sync time: {curr_time}")

        # Build WRQ (write request) message to port 69 and send it
        mode = b'octet'
        wrq_packet = b'\x00\x02' + filename.encode() + b'\x00' + mode + b'\x00'
        sock.sendto(wrq_packet, (server_ip, TFTP_PORT))
        
        if VERBOSE:
            logprint("i", "tftp", "Sent WRQ packet to server...")

        # Receive ACK (block 0) from server and get a new server port for data transfer
        buffer = bytearray(516)
        bytes_received, server_address = sock.recvfrom_into(buffer)
        response = buffer[:bytes_received]
        
        if VERBOSE:
            logprint("i", "tftp", f"Server response: {response}, from {server_address}")

        # Check if the TFTP server acknowledged the WRQ message
        if not response.startswith(b'\x00\x04\x00\x00'):
            logprint("e", "tftp", "Server did not ACK WRQ message properly.")
            sock.close()
            indicate_error()
            return

        if VERBOSE:
            logprint("s", "tftp", f"Server acknowledged WRQ. New port: {server_address[1]}")

        # Send data blocks to a new TFTP server port
        block_number = 1
        offset = 0
        while True:
            block = data[offset:offset+512]
            data_packet = b'\x00\x03' + block_number.to_bytes(2, 'big') + block
            sock.sendto(data_packet, server_address)

            # Wait for ACK for sent block of data
            bytes_received, _ = sock.recvfrom_into(buffer)
            response = buffer[:bytes_received]
            
            if VERBOSE:
                logprint("i", "tftp", f"ACK for block {block_number}: {response}")

            # Check if ACK was received for sent block of data (successful delivery)
            if not response.startswith(b'\x00\x04' + block_number.to_bytes(2, 'big')):
                logprint("e", "tftp", "Did not get proper ACK. Stopping.")
                err_flag = True
                break

            if len(block) <= 512:
                logprint("s", "tftp", "File transfer complete!")
                break

            offset += 512
            block_number += 1
            time.sleep(0.5)  # Wait to avoid flooding the server

        # Gracefully close the connection socket and release its resources
        sock.close()

        if err_flag:
            indicate_error()
    except Exception as e:
        logprint("e", "tftp", str(e))
        indicate_error()
        return


# Get average value from input values
def get_avg(values: list) -> float:
    summ = 0.0
    
    for value in values:
        summ += float(value)
            
    return summ / len(values)


# Get median from input values
def get_median(values: list) -> float:
    sorted_values = sorted(values)
    mid = len(sorted_values) // 2
    
    return (sorted_values[mid] if len(sorted_values) % 2 == 1 
            else (sorted_values[mid - 1] + sorted_values[mid]) / 2)


# Execute 10 temperature measurements and return average value from these measurements
def measure_temp(thermistor, cycles = 10, sleeping = 3) -> float:
    logprint("i", "temp", "Measuring temperature...")
    
    temps = []

    # Voltage Divider
    voltage_in = 3.3
    resistance = 10000  # 10k Resistor

    # Steinhart Constants
    A = 0.001129148
    B = 0.000234125
    C = 0.0000000876741

    if VERBOSE:
        logprint("i", "temp", "Partial measurements:")
        
    for i in range(cycles):
        # Read the voltage from ADC
        voltage_out = (thermistor.value * voltage_in) / 65535
        
        # Calculate Resistance
        curr_resistance = (voltage_out * resistance) / (voltage_in - voltage_out)

        # Steinhart - Hart Equation
        temp_kelvin = 1 / (A + (B * math.log(curr_resistance)) + C * math.pow(math.log(curr_resistance), 3))

        # Convert from Kelvin to Celsius
        temp_celsius = temp_kelvin - 273.15

        if PRINT_OUTPUT and VERBOSE:
            print(f"\t{i+1}. Measured temperature (partial): {round(temp_celsius, 1)}°C")
            
        temps.append(temp_celsius)
        
        time.sleep(sleeping)  # Small delay before next reading
        
    final_temp = get_avg(temps)  # Return average value
    
    logprint("s", "temp", f"Measured temperature (final): {round(final_temp, 1)}°C")
    
    return final_temp


# Execute 10 distance measurements and return median value from these measurements
def measure_dist(sonar, cycles = 10, sleeping = 1) -> float:
    measurements = []
    
    logprint("i", "dist", "Measuring distance...")
    
    if VERBOSE:
        logprint("i", "dist", "Partial measurements:")
        
    for i in range(cycles):
        try:
            # Measure current distance
            distance = sonar.distance
            measurements.append(distance)
            
            if PRINT_OUTPUT and VERBOSE:
                print(f"\t{i+1}. Measured distance (partial): {round(distance, 1)} cm")
        except RuntimeError:
            continue  # Sometimes the sensor gives faulty readings

        time.sleep(sleeping)  # Small delay before next reading
        
    final_dist = get_median(measurements)  # Return median value
    
    logprint("s", "dist", f"Measured distance (final): {round(final_dist, 1)} cm")
            
    return final_dist


# Check if internal RTC is set
def is_rtc_set() -> bool:
    rtc_time = time.localtime()  # Get current RTC time
    return rtc_time[0] >= 2025  # Check if year is reasonable (2025 or more)


# Check if Pico is connected to WiFi
def is_wifi_connected() -> bool:
    return wifi.radio.connected


# Visual separator for output
def separator() -> None:
    if PRINT_OUTPUT:
        print(60*'-')
        

# Project configuration
def configuration() -> dict:
    num_of_cycles = 10
    num_of_measurements = 10
    
    sleep_between_cycles = 60   # Seconds
    sleep_between_temperature = 3    # Seconds
    sleep_between_distance = 1    # Seconds
    
    measurement_filename = "data.json"  # Filename of the measurement file
    
    return {"num_of_cycles": num_of_cycles,
            "num_of_measurements": num_of_measurements,
            "sleep_between_cycles": sleep_between_cycles,
            "sleep_between_temperature": sleep_between_temperature,
            "sleep_between_distance": sleep_between_distance,
            "measurement_filename": measurement_filename}
    
    
def main():
    conf = configuration()  # Get configuration
    measurements = {"data": []}  # Initialize measurement dictionary (JSON format)
    pool = None
    sonar = adafruit_hcsr04.HCSR04(trigger_pin=board.GP2, echo_pin=board.GP3 )  # Initialize sonar (distance measurement)
    thermistor = analogio.AnalogIn(board.A0)  # Initialize thermistor (temperature measurement)
    cycle = 1

    while True:
        if PRINT_OUTPUT:
            print(f"\n##### CYCLE {cycle} #####\n")
            
        # Turn on the built-in LED to indicate measurement start
        led_light(1)
        
        # Connect to WiFi if not connected already
        if not is_wifi_connected():
            pool = connect_wifi()
            separator()

        # Set internal RTC via NTP if not set already
        if not is_rtc_set():
            sync_ntp_to_rtc(pool, NTP_SERVER, TZ, verbose=VERBOSE, print_output=PRINT_OUTPUT)
            separator()
        elif VERBOSE:
            time_data = get_format_time(time.localtime())
            logprint("i", "ntp", f"Internal RTC already set to {time_data}, no need to update")
            separator()

        # Execute measurements
        temperature = measure_temp(thermistor, cycles=conf["num_of_measurements"], sleeping=conf["sleep_between_temperature"])
        separator()
        distance = measure_dist(sonar, cycles=conf["num_of_measurements"], sleeping=conf["sleep_between_distance"])
        separator()
        
        data_dict = {"temperature": temperature, "distance": distance, "timestamp": get_format_time(time.localtime())}
        measurements["data"].append(data_dict)
        
        if PRINT_OUTPUT and VERBOSE:
            print("Control output: ")
            pretty_json = "\n".join(f'  "{k}": {repr(v)}' for k, v in data_dict.items())
            print("{\n" + pretty_json + "\n}")
            separator()

        # Check if cycle threshold for sending measurement data is reached
        if cycle >= conf["num_of_cycles"]:
            send_log_file(pool, TFTP_SERVER, f"{measurements}", conf["measurement_filename"])
            measurements["data"].clear()
            cycle = 1
            separator()
        else:
            cycle += 1

        # Turn off the built-in LED to indicate measurement stop
        led_light(0)
        
        sleep_period = conf["sleep_between_cycles"]   # Sleep between measurements
        if PRINT_OUTPUT:
            print(f"Going to sleep for {sleep_period} seconds...")
            separator()
        time.sleep(sleep_period)
    

main()