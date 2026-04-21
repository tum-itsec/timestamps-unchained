#!/usr/bin/env -S python3 -u

from generic_data_capture import make_argsparser, State
from data_eval.aggregate import aggregate_measurements
from queue import Queue
from threading import Event, Thread
from datetime import datetime

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def msmt_loop(done, args, new_results):
	out = Queue()
	state = State.from_args(done, out.put, args, include_physical=False, enable_ftm=args.ftm)
	while not done.is_set():
		# Do FTM measurement

		if args.ftm:
			ftm_time = datetime.now()
			ftm_summary = state.do_ftm()

		# Do TWR measurement

		try:
			twr_time = datetime.now()
			state.do_msmt()
		except Exception as e:
			if type(e) == KeyboardInterrupt:
				raise e
			print("Error during measurement - ignoring")
			continue

		tss = []
		while not out.empty():
			tss.append(out.get(block=False))

		twr_summary = aggregate_measurements(tss)
		if not twr_summary:
			continue
		#print(twr_summary)

		# Extract data from summaries and pass to UI thread

		result = {}

		if args.ftm:
			result["ftm_time"] = ftm_time
			result["ftm_dist"] = ftm_summary["dist"]

		result["twr_time"] = twr_time
		result["mean"] = twr_summary['mean']
		result["stdev"] = twr_summary['stdev']
		result["min"] = twr_summary['min']
		result["max"] = twr_summary['max']

		new_results.put(result)

def main(done):
	argsparser = make_argsparser(include_output=False, include_physical=False)
	argsparser.add_argument("-f", "--ftm", type=bool, default=False, help="Do FTM too (default: False)")
	args = argsparser.parse_args()

	new_results = Queue()
	msmt_thread = Thread(target=lambda: msmt_loop(done, args, new_results))
	msmt_thread.start()

	twr_times = []
	means = []
	stdevs = []
	mins = []
	maxs = []

	if args.ftm:
		ftm_times = []
		ftm_dists = []

	plt.ion()
	fig, ax = plt.subplots()
	ax.set_xlabel("Time")
	ax.set_ylabel("Value")
	ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

	while fig.get_label() in plt.get_figlabels():
		if new_results.empty():
			plt.pause(0.1)
			continue

		result = new_results.get(block=False)

		if args.ftm:
			ftm_times.append(result["ftm_time"])
			ftm_dists.append(result["ftm_dist"])

		twr_times.append(result["twr_time"])
		means.append(result["mean"])
		stdevs.append(result["stdev"])
		mins.append(result["min"])
		maxs.append(result["max"])

		# Redraw diagram

		ax.clear()
		ax.set_xlabel("Time")
		ax.set_ylabel("Measured distance")
		ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

		ax.errorbar(
			twr_times,
			means,
			yerr=stdevs,
			fmt='o',
			capsize=4
		)

		ax.plot(twr_times, mins, 'x', label="min")
		ax.plot(twr_times, maxs, 'x', label="max")
		if args.ftm:
			ax.plot(ftm_times, ftm_dists, 'x', label="ftm")
		ax.set_ylim(0, 30)

		fig.autofmt_xdate()

if __name__ == "__main__":
	done = Event()
	try:
		main(done)
	finally:
		plt.close('all')
		done.set()
	import time
	time.sleep(1)
	# don't keep old threads running
	# TODO more sane exit strategy.
	# By default doesn't work because ESP threads are quicker to close than msmt thread,
	# so msmt thread gets stuck wherever it was.
	import os
	os._exit(0)
