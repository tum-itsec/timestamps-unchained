#!/usr/bin/env python3

import host_primitives
import sys
import json
import matplotlib.pyplot as plt
import numpy as np
import time
from time import sleep

READY_MARKER = b"READY\r\n"
# unit: meters per second
SPEED_OF_LIGHT = 299792458
# unitless (count)
AVG_WINDOW_SIZE = 10
# unit: seconds
DIAGRAM_LENGTH = 30

class NoLineException(Exception):
	pass

def parse_single_line(esp, tsmap, timeout=0.01):
	line = esp.readline(timeout)
	if not line:
		raise NoLineException(f"Expected a line from {esp.name}, but did not arrive.")
	j = json.loads(line)
	match j["type"]:
		case "timestamp":
			tsmap[(esp.name, j["direction"], j["frame_id"])] = j["timestamp"]
		case "error":
			print(f"Error in {esp.name}: {j['message']}", file=sys.stderr)
		case _:
			print(f"Unknown message type {j['type']}. Full line: {line}", file=sys.stderr)
	return True

def wait_until_rx(esp, tsmap, frameid, timeout=0.01):
	while (esp.name, "rx", frameid) not in tsmap:
		parse_single_line(esp, tsmap, timeout)

def is_twr_dataset_complete_1(tsmap, frameid_base):
	return (True
		and ("esp1", "tx", frameid_base+0) in tsmap
		and ("esp1", "rx", frameid_base+1) in tsmap
		and ("esp1", "tx", frameid_base+2) in tsmap
		)

def is_twr_dataset_complete_2(tsmap, frameid_base):
	return (True
		and ("esp2", "rx", frameid_base+0) in tsmap
		and ("esp2", "tx", frameid_base+1) in tsmap
		and ("esp2", "rx", frameid_base+2) in tsmap
		)

def parse_until_twr_dataset_complete_1(esp1, frameid_base, tsmap, timeout=0.01):
	while not is_twr_dataset_complete_1(tsmap, frameid_base):
		parse_single_line(esp1, tsmap, timeout)

def parse_until_twr_dataset_complete_2(esp2, frameid_base, tsmap, timeout=0.01):
	while not is_twr_dataset_complete_2(tsmap, frameid_base):
		parse_single_line(esp2, tsmap, timeout)

def drain(esp, timeout=0.01):
	while True:
		try:
			parse_single_line(esp, {}, timeout)
		except NoLineException:
			return

def main():
	if len(sys.argv) != 3:
		print(f"Usage: {sys.argv[0]} <esp_uart_path 1> <esp_uart_path 2>", file=sys.stderr)
		sys.exit(1)

	# It seems that we mustn't wait too long after booting before reading from the ESP.
	# So, we first fully initialize the first one before starting with the second one.
	print("Waiting for ESP 1 to ready up...", file=sys.stderr)
	esp1 = host_primitives.ESP(sys.argv[1])
	esp1.name = "esp1"
	esp1.read_until(READY_MARKER)
	print("Waiting for ESP 2 to ready up...", file=sys.stderr)
	esp2 = host_primitives.ESP(sys.argv[2])
	esp2.name = "esp2"
	esp2.read_until(READY_MARKER)
	print("Both ESPs ready!", file=sys.stderr)

	# Print header to stderr
	print("who,what,id,ts/value")

	plt.ion()
	fig, ax = plt.subplots()
	plot_raw = ax.scatter([], [], c=(0,0,1))
	plot_avg = ax.scatter([], [], c=(1,0,0))
	plot_simple = ax.scatter([], [], c=(0,1,0))
	plot_simple2 = ax.scatter([], [], c=(0,1,1))
	ax.set_xlim(0, DIAGRAM_LENGTH)
	ax.set_ylim(-100, 100)
	ax.set_yticks([0])
	ax.set_yticks(range(-100, 100, 1), minor=True)
	ax.grid(axis='y', which='major', color=(0,0,0))
	ax.grid(axis='y', which='minor')
	ax.yaxis.set_minor_formatter(ax.yaxis.get_major_formatter())
	x_data_raw, y_data_raw = [], []
	x_data_avg, y_data_avg = [], []
	y_data_raw_simple = []
	y_data_raw_simple2 = []
	i = 0
	j = 0
	start_time = time.monotonic()
	window = []
	while True:
		i += 3
		if i+2 >= 256:
			i -= 256
		try:
			tsmap = {}
			esp1.write(f"send {i+0}\n".encode())
			wait_until_rx(esp2, tsmap, i+0)
			# sleep(0.01)
			esp2.write(f"send {i+1}\n".encode())
			wait_until_rx(esp1, tsmap, i+1)
			# sleep(0.01)
			esp1.write(f"send {i+2}\n".encode())
			wait_until_rx(esp2, tsmap, i+2)
			# sleep(0.01)
			parse_until_twr_dataset_complete_1(esp1, i, tsmap)
			parse_until_twr_dataset_complete_2(esp2, i, tsmap)
		except NoLineException as e:
			print(f"TWR exchange {i} failed: {e}", file=sys.stderr)
			drain(esp1)
			drain(esp2)
			continue


		# Success!
		t1 = tsmap[('esp1', 'tx', i+0)]
		t2 = tsmap[('esp2', 'rx', i+0)]
		t3 = tsmap[('esp2', 'tx', i+1)]
		t4 = tsmap[('esp1', 'rx', i+1)]
		t5 = tsmap[('esp1', 'tx', i+2)]
		t6 = tsmap[('esp2', 'rx', i+2)]

		print(f"a,s,{j},{t1}")
		print(f"b,r,{j},{t2}")
		print(f"b,s,{j+1},{t3}")
		print(f"a,r,{j+1},{t4}")
		print(f"a,s,{j+2},{t5}")
		print(f"b,r,{j+2},{t6}")
		# Skip always one measurement
		j += 4

		RA = t4 - t1
		DB = t3 - t2
		DA = t5 - t4
		RB = t6 - t3

		dAB = (RA * RB - DA * DB) / (RA + RB + DA + DB)
		dist = dAB / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT

		dAB_simple = (RA - DB) / 2
		dist_simple = dAB_simple / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
		dAB_simple2 = (RB - DA) / 2
		dist_simple2 = dAB_simple2 / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
		# print(dist_simple2)

		#print(f"ra {RA}, db {DB}, da {DA}, rb {RB}, dAB {dAB}, dist {dist}, raw tsmap {tsmap}")
		# print(f"{dist:06.3f}")

		x_data_raw.append(time.monotonic() - start_time)
		y_data_raw.append(dist)
		y_data_raw_simple.append(dist_simple)
		y_data_raw_simple2.append(dist_simple2)
		if len(window) < AVG_WINDOW_SIZE:
			window.append(dist)
		else:
			window = window[-AVG_WINDOW_SIZE-1:] + [dist]
			x_data_avg.append(time.monotonic() - start_time)
			y_data_avg.append(float(np.mean(window)))

		plot_raw.set_offsets(np.c_[x_data_raw, y_data_raw])
		plot_avg.set_offsets(np.c_[x_data_avg, y_data_avg])
		plot_simple.set_offsets(np.c_[x_data_raw, y_data_raw_simple])
		plot_simple2.set_offsets(np.c_[x_data_raw, y_data_raw_simple2])
		plt.pause(0.01)
		now = time.monotonic()
		if now - start_time > DIAGRAM_LENGTH:
			ax.set_xlim(now - start_time - DIAGRAM_LENGTH, now - start_time)

if __name__ == "__main__":
	main()
