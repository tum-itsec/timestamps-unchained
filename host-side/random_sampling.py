#!/usr/bin/env python3

import random
import argparse
from generic_data_capture_wrapper import msmt

ALL_E = ["WIFI_PHY_RATE_1M_L", "WIFI_PHY_RATE_2M_L", "WIFI_PHY_RATE_5M_L", "WIFI_PHY_RATE_11M_L", "WIFI_PHY_RATE_2M_S", "WIFI_PHY_RATE_5M_S", "WIFI_PHY_RATE_11M_S", "WIFI_PHY_RATE_48M", "WIFI_PHY_RATE_24M", "WIFI_PHY_RATE_12M", "WIFI_PHY_RATE_6M", "WIFI_PHY_RATE_54M", "WIFI_PHY_RATE_36M", "WIFI_PHY_RATE_18M", "WIFI_PHY_RATE_9M", "WIFI_PHY_RATE_MCS0_LGI", "WIFI_PHY_RATE_MCS1_LGI", "WIFI_PHY_RATE_MCS2_LGI", "WIFI_PHY_RATE_MCS3_LGI", "WIFI_PHY_RATE_MCS4_LGI", "WIFI_PHY_RATE_MCS5_LGI", "WIFI_PHY_RATE_MCS6_LGI", "WIFI_PHY_RATE_MCS7_LGI", "WIFI_PHY_RATE_MCS0_SGI", "WIFI_PHY_RATE_MCS1_SGI", "WIFI_PHY_RATE_MCS2_SGI", "WIFI_PHY_RATE_MCS3_SGI", "WIFI_PHY_RATE_MCS4_SGI", "WIFI_PHY_RATE_MCS5_SGI", "WIFI_PHY_RATE_MCS6_SGI", "WIFI_PHY_RATE_MCS7_SGI"]
ALL_B = [20, 40]
ALL_S = ["busywait"]
ALL_T = [0.005, 0.008, 0.01, 0.02, 0.03, 0.05, 0.08, 0.1, 0.2, 0.3, 0.5, 1]
# TODO 2048 doesn't work because it takes too much stack space
ALL_L = [24, 32, 256, 1024]

def random_msmts_forever(esp_a_path, esp_b_path, data_dir, R, D, C):
	while True:
		msmt(esp_a_path, esp_b_path, data_dir, R, D, C,
			random.choice(ALL_E), random.choice(ALL_B), random.choice(ALL_S), random.choice(ALL_T), random.choice(ALL_L))

def main(*args, **kwargs):
	argp = argparse.ArgumentParser()
	argp.add_argument("-o", "--output", type=str, default="data", help="Directory path for output CSV files (default: data)")
	argp.add_argument('-R', '--room', type=str, default='unknownroom', help='Room string like for example "gang". (default: unknownroom)')
	argp.add_argument('-D', '--distance', type=float, default=-1000.0, help='Distance. "Ground truth". Unit: meters. (default: -1000.0)')
	argp.add_argument('-C', '--channel', type=int, default=3, help='Channel (choose 2 least busy ones. (default: 3)')
	argp.add_argument("esp_a_path", help="Path to UART for ESP A")
	argp.add_argument("esp_b_path", help="Path to UART for ESP B")
	args = argp.parse_args(*args, **kwargs)

	random_msmts_forever(args.esp_a_path, args.esp_b_path, args.output, args.room, args.distance, args.channel)

if __name__ == '__main__':
	main()
