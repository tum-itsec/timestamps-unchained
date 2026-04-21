#!/usr/bin/env python3

import sys
import statistics
import csv
import os
# Why, Python, why!?
# https://stackoverflow.com/questions/16981921/relative-imports-in-python-3
try:
	from .common import *
except ImportError as _:
	from common import *

MIN_VALID_DATA = 30

def parse_file(filepath):
	measured_tss = []
	meta = {}

	with open(filepath) as csvfile:
		# print(f"Parsing {filepath}")
		reader = csv.reader(csvfile, delimiter=',', quotechar='#')
		for row in reader:
			if row[0] == 'meta':
				meta[row[2]] = row[3]
				continue
			elif row[1] == 'meta':
				meta[row[0] + '_' + row[2]] = row[3]
				continue
			else:
				try:
					measured_tss.append(measured_ts(row[0], row[1], int(row[2]), int(row[3])))
				except ValueError as _:
					# malformed line - ignore
					continue

	result = aggregate_measurements(measured_tss)
	return result | meta | {"filename": filepath} if result is not None else None

def aggregate_measurements(measured_tss):
	max_id = max([t.frameid for t in measured_tss], default=0)

	a_source_signals = {}
	b_source_signals = {}

	for t in measured_tss:
		if False:
			pass
		elif t.reporter == 'a' and t.direction == 's':
			a_source_signals.setdefault(t.frameid, {})['tx'] = t.timestamp
		elif t.reporter == 'a' and t.direction == 'r':
			b_source_signals.setdefault(t.frameid, {})['rx'] = t.timestamp
		elif t.reporter == 'b' and t.direction == 's':
			b_source_signals.setdefault(t.frameid, {})['tx'] = t.timestamp
		elif t.reporter == 'b' and t.direction == 'r':
			a_source_signals.setdefault(t.frameid, {})['rx'] = t.timestamp
		else:
			# Not a valid measured timestamp
			continue

	assert all(x % 2 == 0 for x in list(a_source_signals.keys()))
	assert all(x % 2 == 1 for x in list(b_source_signals.keys()))

	all_signals = a_source_signals | b_source_signals
	# print(all_signals)

	# DSTWR-L
	dstwr_l_results = []
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
			# print(f"Ra: {Ra}, Da: {Da}, Rb: {Rb}, Db: {Db}, Da-Db: {Da - Db}, Ra - Rb: {Ra - Rb}")
			distance = (Ra*Rb - Da*Db)/(Ra + Rb + Da + Db) / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
			dstwr_l_results.append(distance)
		except KeyError:
			#print(f"Skipping incomplete DSTWR-L signal set {i}, {i+1}, {i+2}")
			pass

	# # DSTWR-R
	# dstwr_r_results = []
	# for i in range(1, max_id, 3):
	# 	try:
	# 		t1 = all_signals[i]['tx']
	# 		t2 = all_signals[i]['rx']
	# 		t3 = all_signals[i+1]['tx']
	# 		t4 = all_signals[i+1]['rx']
	# 		t5 = all_signals[i+2]['tx']
	# 		t6 = all_signals[i+2]['rx']
	# 		Rb = t4 - t1
	# 		Da = t3 - t2
	# 		Ra = t6 - t3
	# 		Db = t5 - t4
	# 		distance = (Ra*Rb - Da*Db)/(Ra + Rb + Da + Db) / 1000 / 1000 / 1000 / 1000 * SPEED_OF_LIGHT
	# 		dstwr_r_results.append(distance)
	# 	except KeyError:
	# 		#print(f"Skipping incomplete DSTWR-R signal set {i}, {i+1}, {i+2}")
	# 		pass


	if len(dstwr_l_results) < MIN_VALID_DATA:
		if len(dstwr_l_results) > 0:
			print(f"Too little datapoints ({len(dstwr_l_results)}), discarding")
		return None


	# print(f"dstwr_l_stdev {statistics.stdev(dstwr_l_results)}")
	return {
		'stdev': statistics.stdev(dstwr_l_results),
		'mean': statistics.mean(dstwr_l_results),
		'num_calc': len(dstwr_l_results),
		'min': min(dstwr_l_results),
		'max': max(dstwr_l_results),
	}

def main():
	if len(sys.argv) != 3:
		print(f"Usage: {sys.argv[0]} <data directory path> <result file path>")
		sys.exit(1)

	results = []

	for file in os.listdir(sys.argv[1]):
		filename = os.fsdecode(file)
		if filename.endswith(".csv"):
			result = parse_file(os.path.join(sys.argv[1], filename))
			if result:
				results.append(result)
		else:
			pass

	results_sorted = sorted(results, key=lambda x: x['stdev'])
	with open(sys.argv[2], 'w', newline='') as csvfile:
		writer = csv.DictWriter(csvfile, fieldnames=results[0].keys())
		writer.writeheader()
		writer.writerows(results_sorted)

if __name__ == "__main__":
	main()
