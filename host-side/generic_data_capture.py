#!/usr/bin/env python3

import host_primitives
from sys import stdout, exit
import json
import time
import random
from threading import Thread, Event
from queue import Queue, Empty
import argparse
from subprocess import check_output
from data_eval.common import measured_ts

MAX_SYNC_TRIES = 3
# Unit: seconds. Don't make this too big because clocks might diverge too far for frames to still be "interleaved".
DELAY_AFTER_SYNC = .1

# Kinds of variables:
# - Configuration. User inputs a value; the script can apply that setting.
#   - E B C S T L
# - Physical. User inputs a value and we have to trust that it's correct.
#   - R D
# - Measured. We can't change this value on our own, but we can measure it.
#   - H P U

ESP_MSG_MARKER = b"ESP_MSG: "

def make_argsparser(include_output=True, include_physical=True):
	parser = argparse.ArgumentParser(description="ESP Measurement Configuration")
	if include_output:
		parser.add_argument('-o', '--output', type=str, default="-", help="Output filename. Single dash \"-\" for stdout. (default: -)")
	# Configurable variables
	parser.add_argument('-E', '--encoding_speed', type=str, default='WIFI_PHY_RATE_6M', help='Encoding speed. See end of file for options. (default: WIFI_PHY_RATE_6M)')
	parser.add_argument('-B', '--bandwidth', type=int, choices=[20, 40], default=20, help='Bandwidth. 20 or 40. (default: 20)')
	parser.add_argument('-C', '--channel', type=int, default=3, help='Channel (choose least busy one, or 2 least busy ones for 40 bandwidth. (default: 3)')
	parser.add_argument('-S', '--scheduling_mode', type=str, default='busywait', help='Scheduling mode. (default: busywait)')
	parser.add_argument('-T', '--wait_time', type=float, default=0.010, help='Time. Wait time between each packet; precision depends on scheduling mode. Unit: seconds. (default: 0.010)')
	parser.add_argument('-L', '--length', type=int, default=24, help='Length of packet. Unit: bytes. Minimum 24. (default: 24)')
	if include_physical:
		parser.add_argument('-R', '--room', type=str, default='unknownroom', help='Room string like for example "gang". (default: unknownroom)')
		parser.add_argument('-D', '--distance', type=float, default=-1000.0, help='Distance. "Ground truth". Unit: meters. (default: -1000.0)')
	# ESP paths: positional
	parser.add_argument("esp_a_path", help="Path to UART for ESP A")
	parser.add_argument("esp_b_path", help="Path to UART for ESP B")
	return parser

def put_event(queue, type, data=None):
	queue.put({"type": type, "data": data})

def await_event(queue, type, timeout=None):
	putback = []
	while True:
		try:
			e = queue.get(timeout=timeout)
		except Empty as _:
			# Don't return yet; we still want to handle putback correctly
			result = None
			break
		if e["type"] == type:
			result = e["data"]
			break
		print(f"WARNING: Events received out of order! Got {e['type']} while waiting for {type}")
		putback.append(e)
	for e in putback:
		queue.put(e)
	return result

# Python's print is not thread safe, it seems. So, this instead.
def csv_print_thread(out_file, out, done):
	while not done.is_set():
		try:
			t = out.get(timeout=1)
		except Empty as _:
			continue
		if type(t) == measured_ts:
			csv = f"{t.reporter},{t.direction},{t.frameid},{t.timestamp}\n"
		else:
			csv = t + "\n"
		out_file.write(csv)

def esp_loop(evt, on_timestamp, done, uart_path, name, frameid_mult, frameid_add_tx, frameid_add_rx):
	esp = host_primitives.ESP(uart_path, name=name)

	# TODO is there really no more sane way to do this!?
	put_event(evt, "birth", int(check_output(["stat", "--printf=%W", uart_path])))

	esp.debugprint(f"ESP ready!")
	put_event(evt, "ready", esp)
	while not done.is_set():
		l = esp.readline(1)
		if not l:
			continue
		#esp.debugprint(l)
		if not l.startswith(ESP_MSG_MARKER):
			esp.debugprint(f"ESP log output: {l}")
			continue
		l = l[len(ESP_MSG_MARKER):]
		try:
			j = json.loads(l)
		except json.decoder.JSONDecodeError as _:
			esp.debugprint(f"Unparseable JSON line: {l}")
			continue
		match j["type"]:
			case "timestamp":
				dir = j["direction"]
				frameid = j["frame_id"] * frameid_mult + (frameid_add_tx if dir == 'tx' else frameid_add_rx)
				on_timestamp(measured_ts(name, 's' if dir == 'tx' else 'r', frameid, int(j['timestamp'])))
			case "sync":
				put_event(evt, "sync", j)
			case "sysid":
				put_event(evt, "sysid", j["sysid"])
			case "done":
				put_event(evt, "done")
			case "rbempty":
				put_event(evt, "rbempty")
			case "burstevent":
				if j["event"] == "done":
					put_event(evt, "burst_done")
				elif j["event"] == "start":
					pass
				else:
					esp.debugprint(f"Unhandled burst event: {j['event']}")
			case "ftmreport":
				put_event(evt, "ftmreport", {"rtt": j["rtt"], "dist": j["dist"] / 100})
			case "error":
				esp.debugprint(f"ESP ERROR: {j['message']}")
			case _:
				esp.debugprint(f"Ignoring line with unknown type {j['type']}: {j}")
	esp.quit()

def sleep_until(until):
	to_wait = until - time.monotonic()
	if to_wait <= 0:
		print("Can't keep up! T too small")
		return
	time.sleep(to_wait)

class State:
	def from_args(done, on_timestamp, args, include_physical=True, enable_ftm=False):
		return State(
			done = done,
			on_timestamp = on_timestamp,
			esp_a_path = args.esp_a_path,
			esp_b_path = args.esp_b_path,
			E = args.encoding_speed,
			B = args.bandwidth,
			C = args.channel,
			S = args.scheduling_mode,
			T = args.wait_time,
			L = args.length,
			R = args.room if include_physical else None,
			D = args.distance if include_physical else None,
			enable_ftm = enable_ftm,
		)

	def __init__(self, done, on_timestamp, esp_a_path, esp_b_path, E, B, C, S, T, L, R, D, enable_ftm=False):
		self.done = done

		# Variables from args - for description see parse_args.
		self.E = E
		self.B = B
		self.C = C
		self.S = S
		self.T = T
		self.L = L
		self.R = R
		self.D = D

		# Measured variables
		# Hardware. Which devices are used.
		self.H = {}
		# Power cycle. Unique string for each time the respective ESP got power.
		self.P = {}
		# Uptime. How long ESPs had power at measurement start. Unit: seconds.
		self.U = {}
		# Date. When was this measurement taken. Unit: unix seconds.
		self.date = time.time()

		self.evt_a = evt_a = Queue()
		self.evt_b = evt_b = Queue()

		print("Waiting for ESPs to ready up...")
		self.esp_a_t = Thread(target=lambda: esp_loop(evt_a, on_timestamp, done, esp_a_path, "a", 2, 0, 1))
		self.esp_b_t = Thread(target=lambda: esp_loop(evt_b, on_timestamp, done, esp_b_path, "b", 2, 1, 0))
		self.esp_a_t.start()
		self.esp_b_t.start()
		a_birth = await_event(evt_a, "birth")
		b_birth = await_event(evt_b, "birth")
		self.P["a"] = a_birth
		self.P["b"] = b_birth
		self.U["a"] = self.date - a_birth
		self.U["b"] = self.date - b_birth
		self.esp_a = await_event(evt_a, "ready")
		self.esp_b = await_event(evt_b, "ready")
		print("ESPs ready")

		self._configure(enable_ftm)

	def soft_reset(self, enable_ftm=False):
		self.esp_a.custom_reset(lambda: self.esp_a.esp_nofc.write(b"reset\n"))
		self.esp_b.custom_reset(lambda: self.esp_b.esp_nofc.write(b"reset\n"))
		self._configure(enable_ftm)

	# Internal use only
	def _configure(self, enable_ftm):
		print("Configuring ESPs...")
		evt_a = self.evt_a
		evt_b = self.evt_b
		esp_a = self.esp_a
		esp_b = self.esp_b

		esp_a.write(b"systeminfo\n")
		esp_b.write(b"systeminfo\n")
		self.H["a"] = await_event(evt_a, "sysid")
		self.H["b"] = await_event(evt_b, "sysid")

		if enable_ftm:
			ssid = random.randbytes(15).hex()
			print(f"Using SSID {ssid} for FTM")
			esp_a.write(f"set ssid {ssid}\n".encode())
			esp_b.write(f"set ssid {ssid}\n".encode())
			await_event(evt_a, "done")
			await_event(evt_b, "done")

		esp_a.write(b"set if WIFI_IF_AP\n" if enable_ftm else b"set if WIFI_IF_STA\n")
		esp_b.write(b"set if WIFI_IF_STA\n")
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set encoding_speed {self.E}\n".encode())
		esp_b.write(f"set encoding_speed {self.E}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set bandwidth {int(self.B)}\n".encode())
		esp_b.write(f"set bandwidth {int(self.B)}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set channel {int(self.C)}\n".encode())
		esp_b.write(f"set channel {int(self.C)}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set burst_scheduling_mode {self.S}\n".encode())
		esp_b.write(f"set burst_scheduling_mode {self.S}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set burst_period {int(self.T * 1000000)}\n".encode())
		esp_b.write(f"set burst_period {int(self.T * 1000000)}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set packet_length {self.L}\n".encode())
		esp_b.write(f"set packet_length {self.L}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(b"start_wifi\n")
		esp_b.write(b"start_wifi\n")
		await_event(evt_a, "done")
		await_event(evt_b, "done")

		if enable_ftm:
			esp_a.write(b"start_ap\n")
			await_event(evt_a, "done")
			esp_b.write(b"connect\n")
			await_event(evt_b, "done")
			print("FTM preparation done")

		print("ESP configuration done!")

	def do_msmt(self, burst_id_a=None, burst_id_b=None):
		evt_a = self.evt_a
		evt_b = self.evt_b
		esp_a = self.esp_a
		esp_b = self.esp_b

		random_burst_id_a = burst_id_a is None
		random_burst_id_b = burst_id_b is None
		while True:
			if random_burst_id_a:
				burst_id_a = random.randbytes(4)
			else:
				if type(burst_id_a) != bytes or len(burst_id_a) != 4:
					print("invalid burstid a; must be bytestring of length 4")
			if random_burst_id_b:
				burst_id_b = random.randbytes(4)
			else:
				if type(burst_id_b) != bytes or len(burst_id_b) != 4:
					print("invalid burstid b; must be bytestring of length 4")
			if burst_id_a != burst_id_b:
				break

			if not random_burst_id_a and not random_burst_id_b:
				raise Exception("Burstids cannot be the same")
			print("We are extremely lucky, or extremely unlucky, depending on your point of view")

		print(f"burstid a {burst_id_a.hex()}, b {burst_id_b.hex()}")
		esp_a.write(f"set burst_id {burst_id_a.hex()}\n".encode())
		esp_b.write(f"set burst_id {burst_id_b.hex()}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")
		esp_a.write(f"set burst_peer {burst_id_b.hex()}\n".encode())
		esp_b.write(f"set burst_peer {burst_id_a.hex()}\n".encode())
		await_event(evt_a, "done")
		await_event(evt_b, "done")

		print("Starting sync followed by measurements")
		for i in range(MAX_SYNC_TRIES):
			esp_a.write(f"sync {i}\n".encode())
			while True:
				sync_a = await_event(evt_a, "sync", timeout=1)
				# skip if we receive an older sync timestamp now
				if not sync_a or sync_a["frame_id"] == i or sync_a["direction"] != "tx":
					break
			if not sync_a:
				print("Syncing not successful - tx not found!? Retrying...")
				continue
			while True:
				sync_b = await_event(evt_b, "sync", timeout=1)
				# skip if we receive an older sync timestamp now
				if not sync_b or sync_b["frame_id"] == i or sync_b["direction"] != "rx":
					break
			if not sync_b:
				print("Syncing not successful; retrying...")
				continue

			# Sync successful!
			# Let's ignore all fluctuations in when exactly rx hook and tx hook get called in relation to the frame
			# and assume they're called at exactly the same time.
			# Unit for sync timestamps is microseconds.
			start_time_a = sync_a["timestamp"] + int(DELAY_AFTER_SYNC * 1000 * 1000)
			start_time_b = sync_b["timestamp"] + int((DELAY_AFTER_SYNC + self.T/2) * 1000 * 1000)
			break
		else:
			raise Exception("Couldn't sync, something is wrong. Maybe signal strength not high enough?")

		# Don't waste time printing anything; give burst commands as soon as possible
		esp_a.write(f"burst {start_time_a}\n".encode())
		esp_b.write(f"burst {start_time_b}\n".encode())

		print(f"Sync successful! Started a at {start_time_a / 1000 / 1000:.6f}, b at {start_time_b / 1000 / 1000:.6f}")
		await_event(evt_a, "burst_done")
		await_event(evt_b, "burst_done")

		# Flush output
		esp_a.write(b"wait_rb_empty\n")
		esp_b.write(b"wait_rb_empty\n")
		await_event(evt_a, "rbempty")
		await_event(evt_b, "rbempty")

	def do_ftm(self):
		self.esp_b.write(b"ftm\n")
		return await_event(self.evt_b, "ftmreport")

	def print_csv_header(self, out):
		out("who,what,id,ts/value")
		out(f"meta,meta,E,{self.E}")
		out(f"meta,meta,B,{self.B}")
		out(f"meta,meta,C,{self.C}")
		out(f"meta,meta,S,{self.S}")
		out(f"meta,meta,T,{self.T}")
		out(f"meta,meta,L,{self.L}")
		out(f"meta,meta,R,{self.R}")
		out(f"meta,meta,D,{self.D}")
		for n in ['a', 'b']:
			out(f"{n},meta,H,{self.H[n]}")
			out(f"{n},meta,P,{self.P.get(n, 'invalid')}")
			out(f"{n},meta,U,{self.U[n]}")

	# This will set the "done" signal passed in the constructor!
	def quit(self):
		self.done.set()
		self.esp_a_t.join()
		self.esp_b_t.join()

def main(done):
	args = make_argsparser().parse_args()

	out_file = stdout if args.output == "-" else open(args.output, "w")

	out = Queue()
	out_t = Thread(target=lambda: csv_print_thread(out_file, out, done))
	out_t.start()

	# csv_print_thread can handle these timestamps directly
	state = State.from_args(done, out.put, args)

	state.print_csv_header(out.put)
	state.do_msmt()

	state.quit()
	out_t.join()
	out_file.flush()

	print("Measurement cycle done!")

if __name__ == "__main__":
	done = Event()
	try:
		main(done)
	finally:
		done.set()







# Encoding Speed defines "possible"
# typedef enum {
#     WIFI_PHY_RATE_1M_L      = 0x00, /**< 1 Mbps with long preamble */
#     WIFI_PHY_RATE_2M_L      = 0x01, /**< 2 Mbps with long preamble */
#     WIFI_PHY_RATE_5M_L      = 0x02, /**< 5.5 Mbps with long preamble */
#     WIFI_PHY_RATE_11M_L     = 0x03, /**< 11 Mbps with long preamble */
#     WIFI_PHY_RATE_2M_S      = 0x05, /**< 2 Mbps with short preamble */
#     WIFI_PHY_RATE_5M_S      = 0x06, /**< 5.5 Mbps with short preamble */
#     WIFI_PHY_RATE_11M_S     = 0x07, /**< 11 Mbps with short preamble */
#     WIFI_PHY_RATE_48M       = 0x08, /**< 48 Mbps */
#     WIFI_PHY_RATE_24M       = 0x09, /**< 24 Mbps */
#     WIFI_PHY_RATE_12M       = 0x0A, /**< 12 Mbps */
#     WIFI_PHY_RATE_6M        = 0x0B, /**< 6 Mbps */
#     WIFI_PHY_RATE_54M       = 0x0C, /**< 54 Mbps */
#     WIFI_PHY_RATE_36M       = 0x0D, /**< 36 Mbps */
#     WIFI_PHY_RATE_18M       = 0x0E, /**< 18 Mbps */
#     WIFI_PHY_RATE_9M        = 0x0F, /**< 9 Mbps */
#     /**< rate table and guard interval information for each MCS rate*/
#     /*
#      -----------------------------------------------------------------------------------------------------------
#             MCS RATE             |          HT20           |          HT40           |          HE20           |
#      WIFI_PHY_RATE_MCS0_LGI      |     6.5 Mbps (800ns)    |    13.5 Mbps (800ns)    |     8.1 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS1_LGI      |      13 Mbps (800ns)    |      27 Mbps (800ns)    |    16.3 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS2_LGI      |    19.5 Mbps (800ns)    |    40.5 Mbps (800ns)    |    24.4 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS3_LGI      |      26 Mbps (800ns)    |      54 Mbps (800ns)    |    32.5 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS4_LGI      |      39 Mbps (800ns)    |      81 Mbps (800ns)    |    48.8 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS5_LGI      |      52 Mbps (800ns)    |     108 Mbps (800ns)    |      65 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS6_LGI      |    58.5 Mbps (800ns)    |   121.5 Mbps (800ns)    |    73.1 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS7_LGI      |      65 Mbps (800ns)    |     135 Mbps (800ns)    |    81.3 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS8_LGI      |          -----          |          -----          |    97.5 Mbps (1600ns)   |
#      WIFI_PHY_RATE_MCS9_LGI      |          -----          |          -----          |   108.3 Mbps (1600ns)   |
#      -----------------------------------------------------------------------------------------------------------
#     */
#     WIFI_PHY_RATE_MCS0_LGI  = 0x10, /**< MCS0 with long GI */
#     WIFI_PHY_RATE_MCS1_LGI  = 0x11, /**< MCS1 with long GI */
#     WIFI_PHY_RATE_MCS2_LGI  = 0x12, /**< MCS2 with long GI */
#     WIFI_PHY_RATE_MCS3_LGI  = 0x13, /**< MCS3 with long GI */
#     WIFI_PHY_RATE_MCS4_LGI  = 0x14, /**< MCS4 with long GI */
#     WIFI_PHY_RATE_MCS5_LGI  = 0x15, /**< MCS5 with long GI */
#     WIFI_PHY_RATE_MCS6_LGI  = 0x16, /**< MCS6 with long GI */
#     WIFI_PHY_RATE_MCS7_LGI  = 0x17, /**< MCS7 with long GI */
# #if CONFIG_SOC_WIFI_HE_SUPPORT || !CONFIG_SOC_WIFI_SUPPORTED
#     WIFI_PHY_RATE_MCS8_LGI,         /**< MCS8 with long GI */
#     WIFI_PHY_RATE_MCS9_LGI,         /**< MCS9 with long GI */
# #endif
#     /*
#      -----------------------------------------------------------------------------------------------------------
#             MCS RATE             |          HT20           |          HT40           |          HE20           |
#      WIFI_PHY_RATE_MCS0_SGI      |     7.2 Mbps (400ns)    |      15 Mbps (400ns)    |      8.6 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS1_SGI      |    14.4 Mbps (400ns)    |      30 Mbps (400ns)    |     17.2 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS2_SGI      |    21.7 Mbps (400ns)    |      45 Mbps (400ns)    |     25.8 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS3_SGI      |    28.9 Mbps (400ns)    |      60 Mbps (400ns)    |     34.4 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS4_SGI      |    43.3 Mbps (400ns)    |      90 Mbps (400ns)    |     51.6 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS5_SGI      |    57.8 Mbps (400ns)    |     120 Mbps (400ns)    |     68.8 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS6_SGI      |      65 Mbps (400ns)    |     135 Mbps (400ns)    |     77.4 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS7_SGI      |    72.2 Mbps (400ns)    |     150 Mbps (400ns)    |       86 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS8_SGI      |          -----          |          -----          |    103.2 Mbps (800ns)   |
#      WIFI_PHY_RATE_MCS9_SGI      |          -----          |          -----          |    114.7 Mbps (800ns)   |
#      -----------------------------------------------------------------------------------------------------------
#     */
#     WIFI_PHY_RATE_MCS0_SGI,         /**< MCS0 with short GI */
#     WIFI_PHY_RATE_MCS1_SGI,         /**< MCS1 with short GI */
#     WIFI_PHY_RATE_MCS2_SGI,         /**< MCS2 with short GI */
#     WIFI_PHY_RATE_MCS3_SGI,         /**< MCS3 with short GI */
#     WIFI_PHY_RATE_MCS4_SGI,         /**< MCS4 with short GI */
#     WIFI_PHY_RATE_MCS5_SGI,         /**< MCS5 with short GI */
#     WIFI_PHY_RATE_MCS6_SGI,         /**< MCS6 with short GI */
#     WIFI_PHY_RATE_MCS7_SGI,         /**< MCS7 with short GI */
# #if CONFIG_SOC_WIFI_HE_SUPPORT || !CONFIG_SOC_WIFI_SUPPORTED
#     WIFI_PHY_RATE_MCS8_SGI,         /**< MCS8 with short GI */
#     WIFI_PHY_RATE_MCS9_SGI,         /**< MCS9 with short GI */
# #endif
#     WIFI_PHY_RATE_LORA_250K = 0x29, /**< 250 Kbps */
#     WIFI_PHY_RATE_LORA_500K = 0x2A, /**< 500 Kbps */
#     WIFI_PHY_RATE_MAX,
# } wifi_phy_rate_t;
