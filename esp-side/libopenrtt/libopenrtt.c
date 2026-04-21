#include "libopenrtt.h"
#include "esp_rom_sys.h" // for esp_rom_delay_us

// Symbols we... "borrow" from the library

struct reversed_recv_frame_info {
	char padding0[11];
	unsigned char raw_t2_2;
	unsigned int raw_t2_1;
	char padding1[12];
	unsigned int raw_t2_3;
	char padding2[4];
	unsigned int raw_t3_1;
	unsigned char raw_t3_2;
};
extern struct {
	char padding0[4];
	struct reversed_recv_frame_info *recv_info;
} *wDevCtrl;

extern void **pp_wdev_funcs;
extern size_t wDev_ProcessRxSucData(size_t, size_t, size_t);
extern size_t lmacProcessTxComplete(size_t, size_t, size_t);

// stuff we need for faked_lmacProcessTxComplete
extern int hal_mac_get_txq_state(size_t, size_t, size_t);
extern int __ctzsi2(unsigned int);
extern void hal_mac_get_txq_pmd(int, int32_t*);
extern int our_instances_ptr;
extern int32_t our_tx_eb;

// === Timestamps

uint64_t read_t1() {
	uint32_t offset_is_0x0 = 0;
	uint32_t raw_t1_2 = *(size_t *)(offset_is_0x0 + 0x60034338);
	uint32_t raw_t1_1 = *(size_t *)(offset_is_0x0 + 0x6003433c);

	uint32_t v10 = raw_t1_2;
	int32_t v11 = (0x50 * (uint64_t)v10) >> 32;
	uint32_t v12 = (raw_t1_1 & 0x7F) + 0x50 * v10;
	_Bool v13 = v12 < 0x50 * v10;
	int64_t v14 = 8LL * v12;
	int32_t v15 = (8 * (v13 + v11)) | ((v14) >> 32);
	uint32_t t1_1 = v14 - 0x1400;
	uint32_t t1_2 = ((uint32_t)v14 >= 0x1400) + v15 - 1;

	uint32_t lower, upper;
	uint64_t upper_1, upper_2, lower_2;

	lower_2 = t1_1 + 0x1b3;
	upper_1 = 0x61a * lower_2;
	upper_2 = (0x61a * t1_2) + (upper_1 >> 0x20);

	lower = ((t1_2 << 0x1f) | (lower_2 >> 1)) + upper_1;
	upper = (t1_2 >> 1) + upper_2 + (lower < (uint32_t)upper_1);

	uint64_t timestamp = (uint64_t)upper << 0x20 | lower;
	return timestamp;
}

uint64_t read_t2_nosleep() {
	unsigned int raw_t2_1 = wDevCtrl->recv_info->raw_t2_1;
	unsigned int raw_t2_2 = wDevCtrl->recv_info->raw_t2_2;
	unsigned int raw_t2_3 = wDevCtrl->recv_info->raw_t2_3;

	raw_t2_3 >>= 0x14;
	raw_t2_3 &= 0x7ff;
	if (0x3ff < raw_t2_3) {
		raw_t2_3 = 0x800 - raw_t2_3;
	}
	unsigned int raw_t2_1_mul = raw_t2_1 * 0x280;
	unsigned int raw_t2_1_minus = raw_t2_1_mul - 0x3400;
	raw_t2_2 &= 0x7f;
	raw_t2_2 *= 8;
	raw_t2_2 += raw_t2_1_minus;
	unsigned int t2_lower = raw_t2_3 + raw_t2_2;
	unsigned int t2_upper = (int)((unsigned long long)raw_t2_1 * 0x280 >> 0x20);
	t2_upper += raw_t2_3 + raw_t2_2 < raw_t2_2;
	t2_upper += raw_t2_1_minus < raw_t2_1_mul;
	t2_upper += raw_t2_2 < raw_t2_1_minus;
	t2_upper -= 1;

	unsigned long long timestamp =
		(t2_lower | (unsigned long long)t2_upper << 32) * (1562 * 2 + 1) / 2;
	// last_timestamps_t2[next_timestamp] =
	// (t2_lower | (unsigned long long)t2_upper << 32) * (1562 * 2 + 1) / 2;

	return timestamp;
}

uint64_t read_t2() {
	// wDev_record_ftm_data_local does this too. (Technically, it calls
	// ets_delay_us from the ROM.) Without this, we just read garbage.
	esp_rom_delay_us(0x32);

	return read_t2_nosleep();
}

// === Callbacks

volatile rx_cb_t current_rx_cb;
volatile tx_cb_t current_tx_cb;

size_t faked_wDev_ProcessRxSucData(size_t arg0, size_t arg1, size_t arg2) {
	wifi_promiscuous_pkt_t *pkt = (void *)wDevCtrl->recv_info;
	// Argument why it's not restricting to always read t2, including 50us sleep:
	// If ts is not interesting to user, official library's callback provides all info anyway.
	current_rx_cb(read_t2(), pkt);
	return wDev_ProcessRxSucData(arg0, arg1, arg2);
}

void set_rx_cb(rx_cb_t rx_cb) {
	// avoid theoretical race leading to nullpointer dereference:
	// 1. faked_wDev_ProcessRxSucData gets called
	// 2. we set current_rx_cb to NULL
	// 3. faked_wDev_ProcessRxSucData tries calling current_rx_cb
	pp_wdev_funcs[0x1f4 / 4] = wDev_ProcessRxSucData;
	current_rx_cb = rx_cb;
	pp_wdev_funcs[0x1f4 / 4] = rx_cb == NULL ? wDev_ProcessRxSucData : faked_wDev_ProcessRxSucData;
}
void unset_rx_cb(void) {
	set_rx_cb(NULL);
}

size_t faked_lmacProcessTxComplete(size_t arg0, size_t arg1, size_t arg2) {
	#define unknown_case(state) \
		do { \
			/* TODO some form of error handling / reporting to user would be nice */ \
			/* current_tx_cb(NULL, -1, state); */ \
			return lmacProcessTxComplete(arg0, arg1, arg2); \
		} while(0)
	// lmacProcessTxComplete
	int a0 = hal_mac_get_txq_state(2, arg1, arg2);
	// does while do the first iter?
	if(!a0)
		unknown_case(-1);
	int32_t a0_1 = __ctzsi2(a0);
	int32_t s8_1 = 1 << a0_1;
	// does while only do one iter?
	if((a0 & ~s8_1) != 0)
		unknown_case(-2);
	if(!(4 >= a0_1 && *(char *)(our_instances_ptr + a0_1 * 0x28 + 0x12) == 1))
		unknown_case(-3);

	int s0_2 = a0_1 & 0xff;
	int32_t var_34 = 0;
	hal_mac_get_txq_pmd(s0_2, &var_34);
	int32_t a1_4 = var_34;
	int32_t a5_8 = a1_4 >> 0xc & 0xf;
	int32_t lmacEFES_arg0;
	//int32_t lmacEFES_arg2;
	char emulate_lEFES = 0;
	switch(a5_8) {
	case 0:
		int32_t lPTS_arg0 = s0_2;
		//char lPTS_arg1 = a1_4 >> 0x10;
		// lmacProcessTxSuccess;
		int *piVar3 = (int*) (lPTS_arg0 * 0x28 + our_instances_ptr);
		if (*(char *)((int)piVar3 + 0x1d) < 3)
			unknown_case(-4);
		//TODO some writes to memory happen here; especially relative to piVar3.
		// Ignoring these for now.
		lmacEFES_arg0 = (int32_t) piVar3;
		//lmacEFES_arg2 = 0;
		emulate_lEFES = 1;
		break;
	// case 1 never observed in practice
	case 2:
		//int32_t lPCT_arg0 = s0_2;
		//int32_t lPCT_arg1 = 0;
		// lmacProcessCtsTimeout
		//if (lPCT_arg0 == 0xa)
		//	unknown_case(-5);
		//	// lPCT_arg1 = *(uint *)(*(int *)(our_tx_eb + 0x2c) + 4) >> 4 & 0xf;
		//if (1 < (char)(*(char *)(lPCT_arg0 * 0x28 + our_instances_ptr + 0x12) - 1U))
		//	unknown_case(-6);
		//int32_t lPSRF_arg0 = 0;
		//int32_t lPSRF_arg1 = 1;
		//int32_t lPSRF_arg2 = lPCT_arg1;
		//TODO
		unknown_case(-7);
	// case 3 goes to error case
	// case 4 never observed in practice
	case 5:
		//int32_t lPAT_arg0 = s0_2;
		//int32_t lPAT_arg1 = 0;
		// lmacProcessAckTimeout
		//TODO
		unknown_case(-8);
	default:
		unknown_case(-9);
	}

	uint8_t *data;
	uint16_t *data_len;
	if(emulate_lEFES) {
		// lmacEndFrameExchangeSequence
		int iVar4;
		int iVar5;
		iVar4 = * (int*) lmacEFES_arg0;
		//char cVar6 = *(char *)((int)lmacEFES_arg0 + 0x12);
		// if(lmacEFES_arg2 == 0) //TODO find out where this comes from (spoiler: depends on which tailcall of lmacProcessTxComplete switch cases is taken)

		// ppProcTxDone
		iVar5 = *(int *)(*(int *)(iVar4 + 4) + 4);
		if (**(int **)(iVar4 + 0x2c) << 0xd < 0) {
			iVar5 = iVar5 + 8;
		}
		data = (uint8_t *) iVar5;
		data_len = (uint16_t *) (iVar4 + 0x16);
	} else {
		data = NULL;
		data_len = NULL;
		unknown_case(-10);
	}

	current_tx_cb(read_t1(), data, *data_len);
	return lmacProcessTxComplete(arg0, arg1, arg2);
	#undef unknown_case
}

void set_tx_cb(tx_cb_t tx_cb) {
	// avoid same theoretical race leading to nullpointer dereference as in set_rx_cb
	pp_wdev_funcs[0x178 / 4] = lmacProcessTxComplete;
	current_tx_cb = tx_cb;
	pp_wdev_funcs[0x178 / 4] = tx_cb == NULL ? lmacProcessTxComplete : faked_lmacProcessTxComplete;
}
void unset_tx_cb(void) {
	set_tx_cb(NULL);
}
