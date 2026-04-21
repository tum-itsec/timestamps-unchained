#!/usr/bin/env python3

from sys import argv
import time
import datetime
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import re
import statistics
import math

CUTOFF = 11.983734351568238
SUMMARY = False
BOX = False
CSV_BOX = False
CSV_BAR = False

def correction(y):
	# loms, 30, 10%, calibrated 1st:
	#return 0.833 * y - 0.802

	# loms, 30, 10%, calibrated 1st+2nd
	#return 0.813 * y - 0.216

	return y

def main():
	if len(argv) < 2:
		print(f"Usage: {argv[0]} <eval_file_1> [<eval_file_2>...<eval_file_n>]")
		return

	evalfiles = argv[1:]

	fig, ax = plt.subplots()

	if CSV_BOX:
		print("x,lw,lq,med,uq,uw")
	if CSV_BAR:
		print("distance,acceptance")
	summary_xs1 = []
	summary_ys1 = []
	summary_xs2 = []
	summary_ys2 = []
	box_vals1 = []
	box_vals2 = []
	for file in evalfiles:
		with open(file) as f:
			f.readline()
			ys_uncorr = list(map(float, f.readline().split(",")))
			xs = range(len(ys_uncorr))
		ys = list(map(correction, ys_uncorr))
		match = re.match(".*/npp-.*?_(v2_)?([0-9.]+)m.log.eval", file)
		if not match:
			col = (1, 0, 0)
			d = -1
			is_v2 = False
		else:
			d = float(match.group(2))
			is_v2 = bool(match.group(1))
			if is_v2:
				dmin = 9
				dmax = 11
				col = lambda f: (0 if d != 10 else 1, f, 0)
			else:
				dmin = 5
				dmax = 15
				col = lambda f: (0 if d != 10 else 1, f, 1)
			col = col(math.sqrt((d - dmin) / (dmax - dmin)))

		ar = sum([1 if y < CUTOFF else 0 for y in ys]) / len(ys)

		if CSV_BOX:
			if not is_v2:
				csv = []
				csv.append(d)
				csv.append(min(ys))
				csv += statistics.quantiles(ys, n=4)
				csv.append(max(ys))
				print(','.join(map(str, csv)))
		elif CSV_BAR:
			if is_v2:
				csv = [d, ar]
				print(','.join(map(str, csv)))
		else:
			#print(f"{statistics.median(ys_uncorr)},{d}")
			print(f"{d}{' v2' if is_v2 else ''}: n {len(ys_uncorr)}, stdev {statistics.stdev(ys_uncorr)}")
		if SUMMARY:
			sx = summary_xs2 if is_v2 else summary_xs1
			sy = summary_ys2 if is_v2 else summary_ys1
			bv = box_vals2   if is_v2 else box_vals1
			sx.append(d)
			sy.append(ar)
			bv.append(ys)
		else:
			if is_v2:
				graph = ax.plot(xs, ys, label=file, color=col)[0]

	if SUMMARY and not BOX:
		#ax.scatter(summary_xs1, summary_ys1, s=100)
		#ax.scatter(summary_xs2, summary_ys2, s=100)
		#ax.stem(summary_xs1, summary_ys1)
		#ax.stem(summary_xs2, summary_ys2)
		#ax.bar(summary_xs1, summary_ys1, width=0.1)
		#ax.bar(summary_xs1, [1-y for y in summary_ys1], width=0.1, bottom=summary_ys1)
		ax.bar(summary_xs2, summary_ys2, width=0.1)
		ax.bar(summary_xs2, [1-y for y in summary_ys2], width=0.1, bottom=summary_ys2)
		#ax.bar(summary_xs2, summary_ys2, width=0.1)
	elif SUMMARY and BOX:
		ax.boxplot(box_vals1, positions=summary_xs1, boxprops={"color": "blue"}, widths=0.75)
		ax.boxplot(box_vals2, positions=summary_xs2, boxprops={"color": "green"}, widths=0.20)
	else:
		ax.legend()
		ax.set_yticks(range(0,100,5))
		ax.set_yticks(range(100), minor=True)
		plt.ylim(0, 20)
		ax.grid(which='minor', alpha=0.3)
	ax.grid()
	plt.show()

if __name__ == "__main__":
	main()
