SHELL := /bin/bash
BUILD := build
FIR_RUN_DIR ?= fir_project/runs/fir_8tap_w8_util40_clk10

.PHONY: all sim synth features openroad clean
all: sim

sim:
	mkdir -p $(BUILD)
	iverilog -g2012 -s tb_fir_filter -o $(BUILD)/tb_fir_filter.vvp rtl/fir_filter.v tb/tb_fir_filter.v
	vvp $(BUILD)/tb_fir_filter.vvp

synth:
	@echo "For mapped Sky130 synthesis, edit config/sky130_example.env then run scripts/run_fir_flow.sh."
	@echo "For a technology-independent Yosys check, run: yosys -p 'read_verilog -sv rtl/fir_filter.v; hierarchy -check -top fir_filter; proc; opt; check'"

features:
	python3 scripts/extract_fir_features.py --netlist-json fir_project/synth/fir_filter_netlist.json --run-dir $(FIR_RUN_DIR) --outdir $(FIR_RUN_DIR)

openroad:
	./scripts/run_fir_flow.sh

clean:
	rm -rf build fir_project/synth/* fir_project/runs/*
