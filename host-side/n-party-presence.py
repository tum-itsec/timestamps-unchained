#!/usr/bin/env python3

import host_primitives
from data_eval.common import SPEED_OF_LIGHT
from sys import argv
from threading import Thread
import statistics
import time
import datetime
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# Only take last n seconds into account
MAX_TS_AGE = 1 * 1000 * 1000 * 1000 * 1000
# unit: seconds
WINDOW_SIZE = 30
PERCENT = 0.10
CALIBRATION_PERIOD = 30

def read_until(esp, needle):
	buf = b""
	while not needle in buf:
		buf += esp.read_n(1)
	return buf[:-len(needle)]

# Filters our_msmts:
# All RX are kept. Only those TX that have an corresponding RX in peer_msmts are kept.
# Doesn't modify either argument list.
# Order of returned list will be same as in our_msmts.
def filter_tx_by_rx(our_id, our_msmts, peer_id, peer_msmts):
	filtered = []
	n_rx = 0
	n_tx_keep = 0
	n_tx_drop = 0
	for party_src, msg_id, ts in our_msmts:
		if party_src != our_id:
			# is rx: keep
			filtered.append((party_src, msg_id, ts))
			n_rx += 1
			continue
		# is tx: check for rx in peer_msmts
		if len(list(filter(lambda m: m[0] == party_src and m[1] == msg_id, peer_msmts))):
			n_tx_keep += 1
			filtered.append((party_src, msg_id, ts))
			continue
		# tx without rx - drop it
		n_tx_drop += 1
	#print(f"filter stats: rx {n_rx}, tx keep {n_tx_keep}, tx drop {n_tx_drop}")
	return filtered

def dists(a_id, a_msmts, b_id, b_msmts):
	# rx are unaffected either way,
	# so it's not important whether we use unfiltered or filtered a_msmts when filtering b_msmts
	a_msmts = filter_tx_by_rx(a_id, a_msmts, b_id, b_msmts)
	b_msmts = filter_tx_by_rx(b_id, b_msmts, a_id, a_msmts)

	# Idea: Start with any A TX. Then iterate through:
	# 1. B until corresponding RX,
	# 2. B until TX after that RX (but only if it has an RX),
	# 3. A until corresponding RX,
	# 4. A until TX after that RX (but only if it has an RX),
	# 5. B until corresponding RX.
	# If any of these can't be found, bail out and try next A TX.
	# Now we have a complete DS-TWR-L set of timestamps.
	# Not perfect, but good enough for now.
	results_by_id = {}
	next_frameid = 1
	for t1_i in range(len(a_msmts)):
		t1 = a_msmts[t1_i]
		if t1[0] != a_id:
			# was RX ts; skip
			continue
		t12_msgid = t1[1]

		# Find corresponding RX on B
		t2_candidates = list(filter(lambda m: m[1][0] == a_id and m[1][1] == t12_msgid, enumerate(b_msmts)))
		if len(t2_candidates) == 0:
			continue
		t2_i, t2 = t2_candidates[0]

		# Find following TX on B
		t3_candidates = list(filter(lambda m: m[1][0] == b_id, enumerate(b_msmts[t2_i+1:])))
		if len(t3_candidates) == 0:
			continue
		t3_i, t3 = t3_candidates[0]
		t34_msgid = t3[1]

		# Find corresponding RX on A
		t4_candidates = list(filter(lambda m: m[1][0] == b_id and m[1][1] == t34_msgid, enumerate(a_msmts)))
		if len(t4_candidates) == 0:
			continue
		t4_i, t4 = t4_candidates[0]

		# Find following TX on A
		t5_candidates = list(filter(lambda m: m[1][0] == a_id, enumerate(a_msmts[t4_i+1:])))
		if len(t5_candidates) == 0:
			continue
		t5_i, t5 = t5_candidates[0]
		t56_msgid = t5[1]

		# Find corresponding RX on B
		t6_candidates = list(filter(lambda m: m[1][0] == a_id and m[1][1] == t56_msgid, enumerate(b_msmts)))
		if len(t6_candidates) == 0:
			continue
		t6_i, t6 = t6_candidates[0]

		#print(f"{t1}, {t2}, {t3}, {t4}, {t5}, {t6}")

		# can't use t6: that's on other side
		duration = (t5[2] - t1[2]) / 1000 / 1000 / 1000 / 1000
		if duration < 0:
			print(f"duration negative!? bug!")
			continue
		if duration > 3:
			print("skipping msmt that took too long")
			continue

		skew = (t4[2] - t1[2]) / 1000 / 1000 / 1000 / 1000 / duration

		# We have a complete DS-TWR-L set! Compute it and append to results array.
		Ra = t4[2] - t1[2]
		Db = t3[2] - t2[2]
		Rb = t6[2] - t3[2]
		Da = t5[2] - t4[2]
		den = Ra + Rb + Da + Db
		if den == 0:
			print("den==0!? very weird")
			continue
		distance = (Ra*Rb - Da*Db)/den / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
		results_by_id[t12_msgid] = (distance, t1[2] / 1000 / 1000 / 1000 / 1000, duration, skew)
	results = [x[0] for x in results_by_id.values()]
	return {
		'stdev': statistics.stdev(results) if len(results) >= 2 else float('nan'),
		'mean': statistics.mean(results) if len(results) >= 1 else float('nan'),
		'num_calc': len(results),
		'min': min(results) if len(results) >= 1 else float('inf'),
		'max': max(results) if len(results) >= 1 else float('inf'),
                'count': len(results),
		'all': results_by_id
        }

matplotlib_data = {}

def esp_loop_iter(name, esp, msmts_by_party, log):
	line = read_until(esp, b"\n")
	log.write(line + b"\n")
	if not line.startswith(b"[TS] "):
		print(f"[{name}] line: {line}")
		return

	try:
		party_src, party_dst, msg_id, ts = map(int, line[len(b"[TS] "):].decode().split(" "))
	except ValueError as _:
		print(f"Unparseable line; skipping: {line}")
		return

	msmts = msmts_by_party.get(party_dst, [])
	msmts.append((party_src, msg_id, ts))
	msmts = list(sorted(filter(lambda e: ts - e[2] < MAX_TS_AGE, msmts), key=lambda e:e[2]))
	msmts_by_party[party_dst] = msmts

	# TODO maybe check for conflicting information

	for a_id, a_msmts in msmts_by_party.items():
		for b_id, b_msmts in msmts_by_party.items():
			if a_id == b_id:
				continue
			result = dists(a_id, a_msmts, b_id, b_msmts)
			if a_id == 172 and b_id == 140:
				global matplotlib_data
				matplotlib_data |= result['all']
			#print(f"[{name}] {a_id} -> {b_id}: min {result['min']}, mean {result['mean']}, count {result['count']}")

write_file_now = False

def esp_loop(uart_path, name, suffix):
	if uart_path.endswith(".log"):
		import collections
		import io
		logfile = open(uart_path, "br")
		def read_n(n):
			buf = logfile.read(n)
			if not buf:
				raise Exception("eof")
			return buf
		esp = collections.namedtuple('mockesp', ['read_n'])(read_n)
		log = io.BytesIO()
		msmts_by_party = {}

		try:
			while True:
				esp_loop_iter(name, esp, msmts_by_party, log)
		except Exception as e:
			# EOF
			print(f"{e}")
		global write_file_now
		write_file_now = True
		return

	print(f"Opening {uart_path}")
	esp = host_primitives.ESPNoFlowControl(uart_path, name=name)
	# Dict from dst party_id to array of measurements; each msmt is (src_party_id, msg_id, timestamp).
	# src_party_id is equal to dst for tx timestamps.
	msmts_by_party = {}
	with open(f"data/npp/npp-{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{suffix}.log", "bw") as log:
		while True:
			esp_loop_iter(name, esp, msmts_by_party, log)

last_keys_list = None

def main():
	if len(argv) < 3:
		print("Usage:")
		print(f"- {argv[0]} <esp_uart> <log_file_suffix> (you can call with multiple uarts, but not recommended / necessary):")
		print("\tShows live graph of npp computations, and logs everything into logfile according to log_file_suffix")
		print(f"- {argv[0]} <logfile> <ignored> (don't give multiple logfiles!):")
		print("\tShows graph of npp computations from log file, computes eval file from that, then exits")
		return

	suffix = argv[-1]
	uarts = argv[1:-1]

	# Default parameters i=i and uart=uart to prevent Python from making a closure around variables i and uart.
	threads = [Thread(target=lambda i=i, uart=uart: esp_loop(uart, f"ESP_{i}", suffix)) for i, uart in enumerate(uarts)]
	for t in threads:
		t.start()
		import time
		time.sleep(2)

	fig, ax = plt.subplots(num=uarts[0])
	#graph_raw = ax.plot([1], [1])[0]
	#graph_med = ax.plot([1], [1])[0]
	#graph_avg = ax.plot([1], [1])[0]
	#graph_min = ax.plot([1], [1])[0]
	#graph_loa = ax.plot([1], [1])[0]
	graph_lom = ax.plot([1], [1])[0]
	#graph_dur = ax.plot([1], [1])[0]
	#graph_skw = ax.plot([1], [1])[0]
	def update(frame):
		# need to read this before reading matplotlib_data
		global write_file_now
		local_write_file_now = write_file_now

		data = matplotlib_data
		if len(data) == 0:
			return
		keys_list = list(sorted(data.keys()))
		global last_keys_list
		if keys_list == last_keys_list:
			return
		last_keys_list = keys_list
		all_start = [data[k][1] for k in keys_list]

		cal = []
		meds = []
		avgs = []
		mins = []
		loas = []
		loms = []
		durs = []
		skws = []
		window_valid_is = []
		first_ts = data[keys_list[0]][1]
		for i, k in enumerate(keys_list):
			right = data[k][1]
			left = right - WINDOW_SIZE
			if left - first_ts >= CALIBRATION_PERIOD:
				# window size is fulfilled yet - we can use this datapoint
				window_valid_is.append(i)
			if right < CALIBRATION_PERIOD:
				cal.append(data[k][0])
			window = [data[k][0] for k in keys_list if data[k][1] <= right and data[k][1] >= left]
			meds.append(statistics.median(window))
			avgs.append(statistics.mean(window))
			mins.append(min(window))
			lowest_percentile = list(sorted(window))
			lowest_percentile = lowest_percentile[0:max(1,int(len(lowest_percentile)*PERCENT))]
			loas.append(statistics.mean(lowest_percentile))
			loms.append(statistics.median(lowest_percentile))
			durs.append(data[k][2] * 100)
			skws.append(data[k][3] * 10)

		if local_write_file_now:
			write_file_now = False
			# repeat lom computation here
			cal = list(sorted(cal))
			cal = cal[0:max(1,int(len(cal)*PERCENT))]
			cal = statistics.median(cal) if len(cal) > 0 else -1
			print(f"Calibration {uarts[0]}: {cal}")
			with open(f"{uarts[0]}.eval", "w") as eval:
				eval.write(",".join([str(keys_list[i]) for i in window_valid_is]) + "\n")
				eval.write(",".join([str(loms[i]) for i in window_valid_is]) + "\n")
			print("File written!")
			from sys import exit
			exit(0)

		#graph_raw.set_xdata(all_start)
		#graph_med.set_xdata(all_start)
		#graph_avg.set_xdata(all_start)
		#graph_min.set_xdata(all_start)
		#graph_loa.set_xdata(all_start)
		graph_lom.set_xdata(all_start)
		#graph_dur.set_xdata(all_start)
		#graph_skw.set_xdata(all_start)

		#graph_raw.set_ydata([data[k][0] for k in keys_list])
		#graph_med.set_ydata(meds)
		#graph_avg.set_ydata(avgs)
		#graph_min.set_ydata(mins)
		#graph_loa.set_ydata(loas)
		graph_lom.set_ydata(loms)
		#graph_dur.set_ydata(durs)
		#graph_skw.set_ydata(skws)

		plt.xlim(min(all_start), max(all_start))
		#plt.ylim(0, max([x[0] for x in data.values()]))
		plt.ylim(0, 25)
	anim = FuncAnimation(fig, update, frames=None, cache_frame_data=False)
	plt.show()

if __name__ == "__main__":
	main()
