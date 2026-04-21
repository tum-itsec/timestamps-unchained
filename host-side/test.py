import host_primitives
import time
import json
from sys import argv

esp = host_primitives.ESP(argv[1], do_reset=True)
esp.wait_until_ready()

esp.write(b"start_wifi\n")
esp.write(b"set burst_period 1337\n")
esp.write(b"set burst_id 13371337\n")
esp.write(b"set packet_length 24\n")
esp.write(b"set burst_scheduling_mode asap\n")
esp.write(b"burst 1337\n")
print("waiting for stuff")
last_ts = None
while True:
	line = esp.readline()
	if not line.startswith(b"ESP_MSG: "):
		print(f"raw msg {line}")
		continue
	line = line[len(b"ESP_MSG: "):]
	try:
		j = json.loads(line)
	except:
		j = {"type": "unparseable"}
	if j["type"] == "timestamp":
		ts = j["timestamp"]
		if last_ts:
			print(f"{(ts - last_ts) // 1000 // 1000:6d}")
		last_ts = ts
	else:
		print(f"unparseable json / unknown type: {line}")
