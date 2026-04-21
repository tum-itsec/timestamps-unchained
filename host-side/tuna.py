#!/usr/bin/env python3

import argparse
import optuna
from generic_data_capture_wrapper import msmt
from data_eval.aggregate import parse_file
import os
import math

# Results will be clamped to this value. Also, this is what "infinitely bad" tries will be reported as.
# Unit: meters.
# When changing this, delete and re-import entire study!
MAX_RESULT = float('inf')
# When changing this, delete and re-import entiry study!
PRE_APPLY_LOG = True
# These always fail with bandwidth 40, but (at least sometimes) work with bandwidth 20.
# TODO why?
ALL_E_20ONLY = ["WIFI_PHY_RATE_MCS0_LGI", "WIFI_PHY_RATE_MCS1_LGI", "WIFI_PHY_RATE_MCS2_LGI", "WIFI_PHY_RATE_MCS3_LGI", "WIFI_PHY_RATE_MCS4_LGI", "WIFI_PHY_RATE_MCS5_LGI", "WIFI_PHY_RATE_MCS6_LGI", "WIFI_PHY_RATE_MCS7_LGI", "WIFI_PHY_RATE_MCS0_SGI", "WIFI_PHY_RATE_MCS1_SGI", "WIFI_PHY_RATE_MCS2_SGI", "WIFI_PHY_RATE_MCS3_SGI", "WIFI_PHY_RATE_MCS4_SGI", "WIFI_PHY_RATE_MCS5_SGI", "WIFI_PHY_RATE_MCS6_SGI", "WIFI_PHY_RATE_MCS7_SGI"]
# These work with both bandwidths.
ALL_E_20AND40 = ["WIFI_PHY_RATE_1M_L", "WIFI_PHY_RATE_2M_L", "WIFI_PHY_RATE_5M_L", "WIFI_PHY_RATE_11M_L", "WIFI_PHY_RATE_2M_S", "WIFI_PHY_RATE_5M_S", "WIFI_PHY_RATE_11M_S", "WIFI_PHY_RATE_48M", "WIFI_PHY_RATE_24M", "WIFI_PHY_RATE_12M", "WIFI_PHY_RATE_6M", "WIFI_PHY_RATE_54M", "WIFI_PHY_RATE_36M", "WIFI_PHY_RATE_18M", "WIFI_PHY_RATE_9M"]

# When changing this, delete and re-import entire study!
UNINTERESTING_E = []
# These ones are always extremely bad: probably other modulation scheme.
# Not a single report about these was ever observed with stdev < 50.
UNINTERESTING_E += ["WIFI_PHY_RATE_1M_L", "WIFI_PHY_RATE_2M_L", "WIFI_PHY_RATE_5M_L", "WIFI_PHY_RATE_11M_L", "WIFI_PHY_RATE_2M_S", "WIFI_PHY_RATE_5M_S", "WIFI_PHY_RATE_11M_S"]
# These ones are weird and unreliable.
#UNINTERESTING_E += ["WIFI_PHY_RATE_MCS0_LGI", "WIFI_PHY_RATE_MCS1_LGI", "WIFI_PHY_RATE_MCS2_LGI", "WIFI_PHY_RATE_MCS3_LGI", "WIFI_PHY_RATE_MCS4_LGI", "WIFI_PHY_RATE_MCS5_LGI", "WIFI_PHY_RATE_MCS6_LGI", "WIFI_PHY_RATE_MCS7_LGI", "WIFI_PHY_RATE_MCS0_SGI","WIFI_PHY_RATE_MCS1_SGI", "WIFI_PHY_RATE_MCS2_SGI", "WIFI_PHY_RATE_MCS3_SGI", "WIFI_PHY_RATE_MCS4_SGI", "WIFI_PHY_RATE_MCS5_SGI", "WIFI_PHY_RATE_MCS6_SGI", "WIFI_PHY_RATE_MCS7_SGI"]

ALL_E = ALL_E_20ONLY + ALL_E_20AND40
INTERESTING_E = [x for x in ALL_E if x not in UNINTERESTING_E]

def objective(trial, args):
	# When changing parameters, change import_as_trial() too!
	encoding_speed = trial.suggest_categorical("encoding_speed", INTERESTING_E)
	bandwidth = trial.suggest_categorical("bandwidth", [20, 40])
	wait_time = trial.suggest_float("wait_time", 0.0001, 1.0, log=True)
	packet_length = trial.suggest_int("packet_length", 24, 1024) # TODO log=True ?

	if bandwidth == 40 and encoding_speed not in ALL_E_20AND40:
		print("Marking non-working B / E combo as infinitely bad")
		return MAX_RESULT

	msmt_result_filepath = msmt(esp_a_path=args.esp_a_path, esp_b_path=args.esp_b_path, data_dir=args.output, R=args.room, D=args.distance, C=args.channel, E=encoding_speed, B=bandwidth, S='busywait', T=wait_time, L=packet_length)
	if msmt_result_filepath is None:
		return float('NaN')

	result = parse_file(msmt_result_filepath)

	if result is None:
		return float('NaN')

	trial.set_user_attr("msmt_id", result_id(result))
	return extract_raw_result(result)

def import_as_trial(filename):
	result = parse_file(filename)
	if result is None:
		return None
	if result["E"] in UNINTERESTING_E:
		return None
	if result["R"] == "unknownroom" or float(result["D"]) < 0:
		#print(f"Refusing to add measurement for room {result['R']}, distance {result['D']}")
		return None
	return optuna.trial.create_trial(
		# When changing parameters, change objective() too!
		params={
			"encoding_speed": result["E"],
			"bandwidth": int(result["B"]),
			"wait_time": float(result["T"]),
			"packet_length": int(result["L"]),
		},
		distributions={
			"encoding_speed": optuna.distributions.CategoricalDistribution(INTERESTING_E),
			"bandwidth": optuna.distributions.CategoricalDistribution([20, 40]),
			"wait_time": optuna.distributions.FloatDistribution(0.0001, 1.0, log=True),
			"packet_length": optuna.distributions.IntDistribution(24, 1024),
		},
		value=extract_raw_result(result),
		user_attrs={"msmt_id": result_id(result)},
	)

def extract_raw_result(result):
	result_raw = float(result['stdev'])
	if PRE_APPLY_LOG:
		result_raw = math.log(result_raw)
	return result_raw if result_raw < MAX_RESULT else MAX_RESULT

def result_id(result):
	return " | ".join([f"{k}: {result[k]}" for k in ["E", "B", "C", "S", "T", "L", "R", "D", "a_H", "b_H", "a_P", "b_P", "a_U", "b_U"]])


def main(*args, **kwargs):
	argp = argparse.ArgumentParser()
	argp.add_argument("-o", "--output", type=str, default="data", help="Directory path for output CSV files (default: data)")
	argp.add_argument("-d", "--database", type=str, default="tuna.db", help="SQLite database file to store results (default: tuna.db)")
	argp.add_argument('-R', '--room', type=str, default='unknownroom', help='Room string like for example "gang". (default: unknownroom)')
	argp.add_argument('-D', '--distance', type=float, default=-1000.0, help='Distance. "Ground truth". Unit: meters. (default: -1000.0)')
	argp.add_argument('-C', '--channel', type=int, default=3, help='Channel (choose 2 least busy ones. (default: 3)')
	argp.add_argument('-i', '--importdir', type=str, help='Instead of running experiments, import all existing experiments from the given folder. An "experiment" is defined to be a .csv file.')
	argp.add_argument("esp_a_path", help="Path to UART for ESP A")
	argp.add_argument("esp_b_path", help="Path to UART for ESP B")
	args = argp.parse_args(*args, **kwargs)

	study = optuna.create_study(
		study_name="stdev",
		load_if_exists=True,
		direction="minimize",
		sampler=optuna.samplers.RandomSampler(), storage=f"sqlite:///{args.database}")

	if args.importdir is None:
		if args.room == "unknownroom" or args.distance < 0:
			print("WARNING: Room and distance not set. Running anyway...")
			from time import sleep
			sleep(5)
		study.optimize(lambda trial: objective(trial, args))
	else:
		success = 0
		existing = 0
		quality = 0
		names = list(os.listdir(args.importdir))
		names.sort()
		for name in names:
			if not name.endswith('.csv'):
				continue
			trial = import_as_trial(os.path.join(args.importdir, name))
			if trial is None:
				quality += 1
				continue
			if trial.user_attrs["msmt_id"] in [t.user_attrs.get("msmt_id", "") for t in study.trials]:
				existing += 1
				continue
			study.add_trial(trial)
			success += 1
		print()
		print(f"Importing done: successfully imported {success}, skipped because duplicate {existing}, skipped because too low quality: {quality}")


if __name__ == "__main__":
	main()
