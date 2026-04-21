#!/usr/bin/env python3

import sys
import matplotlib.pyplot as plt
import numpy as np
import time
from time import sleep
import csv
from collections import namedtuple

SPEED_OF_LIGHT = 299792458
DIAGRAM_LENGTH = 30


def compute_features(e):
	return{
		"Da_minus_Db": e.Da - e.Db,
		"Ra_minus_Rb": e.Ra - e.Rb,
	}


def main():
	if len(sys.argv) != 2:
		print(f"Usage: {sys.argv[0]} <data file path>")
		sys.exit(1)


	a_source_signals = {}
	b_source_signals = {}
	max_id = 0

	with open(sys.argv[1]) as csvfile:
		reader = csv.reader(csvfile, delimiter=',', quotechar='#')
		for row in reader:
			if row[0] == 'a' and row[1] == 's':
				a_source_signals.setdefault(int(row[2]), {})['tx'] = int(row[3])
			elif row[0] == 'a' and row[1] == 'r':
				b_source_signals.setdefault(int(row[2]), {})['rx'] = int(row[3])
			elif row[0] == 'b' and row[1] == 's':
				b_source_signals.setdefault(int(row[2]), {})['tx'] = int(row[3])
			elif row[0] == 'b' and row[1] == 'r':
				a_source_signals.setdefault(int(row[2]), {})['rx'] = int(row[3])
			else:
				# Not a line containing valid data
				continue
			max_id = max(max_id, int(row[2]))

	# Check if all a_source signals are even and all b_source signals are odd ids
	assert all(x % 2 == 0 for x in list(a_source_signals.keys()))
	assert all(x % 2 == 1 for x in list(b_source_signals.keys()))

	# Tag a and b signals according to their source before merging
	for e in a_source_signals.values():
		e['source'] = 'a'
	for e in b_source_signals.values():
		e['source'] = 'b'
	# Merge both dicts into one big dict
	all_signals = a_source_signals | b_source_signals
	for i in range(2, max_id):
		try:
			print(f"{i} - {i-2}: {all_signals[i]['tx'] - all_signals[i-2]['tx']}")
			# print(f"{i}: {all_signals[i]['tx']}")
		except KeyError:
			pass

	# FTM-L
	ftm_l_results = {} 
	for i in range(0, max_id, 2):
		try:
			t1 = all_signals[i]['tx']
			t2 = all_signals[i]['rx']
			t3 = all_signals[i+1]['tx']
			t4 = all_signals[i+1]['rx']
			Ra = t4 - t1
			Db = t3 - t2
			distance = (Ra - Db) / 2 / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
			ftm_l_results[i] = distance
		except KeyError:
			print(f"Skipping incomplete FTM-L signal set {i}, {i+1}")

	# FTM-R
	ftm_r_results = {} 
	for i in range(1, max_id, 2):
		try:
			t1 = all_signals[i]['tx']
			t2 = all_signals[i]['rx']
			t3 = all_signals[i+1]['tx']
			t4 = all_signals[i+1]['rx']
			Ra = t4 - t1
			Db = t3 - t2
			distance = (Ra - Db) / 2 / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
			ftm_r_results[i] = distance
		except KeyError:
			print(f"Skipping incomplete FTM-R signal set {i}, {i+1}")

	# DSTWR-L
	dstwr_l_results = {} 
	dstwr_l_datapoints = {} 
	for i in range(0, max_id, 3):
		try:
			t1 = all_signals[i]['tx']
			t2 = all_signals[i]['rx']
			t3 = all_signals[i+1]['tx']
			t4 = all_signals[i+1]['rx']
			t5 = all_signals[i+2]['tx']
			t6 = all_signals[i+2]['rx']
			Ra = t4 - t1
			Db = t3 - t2
			Rb = t6 - t3
			Da = t5 - t4
			print(f"Ra: {Ra}, Da: {Da}, Rb: {Rb}, Db: {Db}, Da-Db: {Da - Db}, Ra - Rb: {Ra - Rb}")
			distance = (Ra*Rb - Da*Db)/(Ra + Rb + Da + Db) / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
			dstwr_l_results[i] = distance
			dstwr_l_datapoints[i] = {'ra': Ra, 'rb': Rb, 'da': Da, 'db': Db} 
		except KeyError:
			print(f"Skipping incomplete DSTWR-L signal set {i}, {i+1}, {i+2}")

	# DSTWR-R
	dstwr_r_results = {} 
	for i in range(1, max_id, 3):
		try:
			t1 = all_signals[i]['tx']
			t2 = all_signals[i]['rx']
			t3 = all_signals[i+1]['tx']
			t4 = all_signals[i+1]['rx']
			t5 = all_signals[i+2]['tx']
			t6 = all_signals[i+2]['rx']
			Rb = t4 - t1
			Da = t3 - t2
			Ra = t6 - t3
			Db = t5 - t4
			distance = (Ra*Rb - Da*Db)/(Ra + Rb + Da + Db) / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
			dstwr_r_results[i] = distance
		except KeyError:
			print(f"Skipping incomplete DSTWR-R signal set {i}, {i+1}, {i+2}")


	## Data prep 

	# Old way of placing stuff on grid
	# x = np.arange(max_id)

	# Use always the clock of A to position/space plot on grid
	x = np.array([(all_signals[i].get('tx', -1) if all_signals[i]['source'] == 'a' else all_signals[i].get('rx', -1)) / 1_000_000_000_000 for i in range(0, max_id)])
	# Pad all datasets to the same length
	# max_len = max(len(lst) for lst in datasets)
	# x = np.arange(max_len)
	# for d in datasets:
	#	 d += [None] * (max_len - len(d))

	# Put np.nan inbetween where there is no data and as padding on the end
	ftm_l_y = np.array([ftm_l_results.get(i, np.nan) for i in range(0, max_id)])
	ftm_r_y = np.array([ftm_r_results.get(i, np.nan) for i in range(0, max_id)])
	dstwr_l_y = np.array([dstwr_l_results.get(i, np.nan) for i in range(0, max_id)])
	dstwr_r_y = np.array([dstwr_r_results.get(i, np.nan) for i in range(0, max_id)])

	# claculate means
	ftm_l_avg = np.nanmean(ftm_l_y)
	ftm_r_avg = np.nanmean(ftm_r_y)
	dstwr_l_avg = np.nanmean(dstwr_l_y)
	dstwr_r_avg = np.nanmean(dstwr_r_y)
	ftm_l_std = np.nanstd(ftm_l_y)
	ftm_r_std = np.nanstd(ftm_r_y)
	dstwr_l_std = np.nanstd(dstwr_l_y)
	dstwr_r_std = np.nanstd(dstwr_r_y)
	print(f"dstwr_l_min: {np.nanmin(dstwr_l_y)}")


	# Data analysis with DSTWR-L
	dstwr_l_std = np.nanstd(dstwr_l_y, ddof=1)
	dstwr_l_zscores = (dstwr_l_y - dstwr_l_avg) / dstwr_l_std
	
	mask = np.abs(dstwr_l_zscores) > 1.0
	bad_ones = np.where(mask, dstwr_l_y, np.nan) # Plotable
	# for i in range(0, dstwr_l_datapoints):
	#	 try:
	#		 good_datapoints.append()
	#	 except KeyError:
	#		 pass


	# # Test features

	# print(bad_datapoints)
	# print(good_datapoints)



	# Plotting

	plt.figure(figsize=(10, 5))
	plt.scatter(x, ftm_l_y, label=f"FTM-L (avg = {ftm_l_avg:.3f}m, std = {ftm_l_std:.3f}m)", color="blue", marker="o")
	plt.scatter(x, ftm_r_y, label=f"FTM-R (avg = {ftm_r_avg:.3f}m, std = {ftm_r_std:.3f}m)", color="green", marker="o")
	plt.scatter(x, dstwr_l_y, label=f"DSTWR-L (avg = {dstwr_l_avg:.3f}m, std = {dstwr_l_std:.3f}m)", color="red", marker="o")
	plt.scatter(x, dstwr_r_y, label=f"DSTWR-R (avg = {dstwr_r_avg:.3f}m, std = {dstwr_r_std:.3f}m)", color="orange", marker="o")


	plt.scatter(x, bad_ones, label=f"BAD ONES)", color="black", marker="x")

	# Add average lines
	plt.axhline(y=ftm_l_avg, color="blue", linestyle="--", alpha=0.5)
	plt.axhline(y=ftm_r_avg, color="green", linestyle="--", alpha=0.5)
	plt.axhline(y=dstwr_l_avg, color="red", linestyle="--", alpha=0.5)
	plt.axhline(y=dstwr_r_avg, color="orange", linestyle="--", alpha=0.5)

	# Add labels and styling
	plt.title("Measurement Results")
	plt.xlabel("Measurement Time (first ts of A) [s]")
	plt.ylabel("Distance [m]")
	plt.legend()
	plt.grid(True, linestyle="--", alpha=0.6)
	plt.tight_layout()
	plt.show()



	# # Clean up incomplete measurements
	# for l in [b_source_signals, a_source_signals]:
	#	 for k in list(l.keys()):
	#		 if 'rx' not in l[k] or 'tx' not in l[k]:
	#			 print(f"Removing incomplete entry {k}: {l[k]}")
	#			 del l[k]


	# a_outgoing_signals_ordered = sorted(list(a_source_signals.values()), key=lambda x: x['tx']) # Ordered by As transmission time
	# a_incoming_signals_ordered = sorted(list(b_source_signals.values()), key=lambda x: x['rx']) # Ordered by As reception time
	

if __name__ == "__main__":
	main()
