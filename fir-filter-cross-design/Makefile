
SHELL := /bin/bash

BUILD := build

FIR_RUN_DIR ?= fir_project/runs/fir_8tap_w8_util40_clk10
NETLIST_JSON ?= fir_project/synth/fir_filter_netlist.json

DESIGN_ID ?= fir_filter_8tap
RUN_ID ?= fir_8tap_w8_util40_clk10

TAPS ?= 8
DATA_WIDTH ?= 8
COEFF_WIDTH ?= 8
ACC_WIDTH ?= 24
UTIL ?= 40
PLACE_DENSITY ?= 0.40
CLK_PERIOD ?= 10.0

.PHONY: all sim synth features report openroad clean

all: sim

sim:
	mkdir -p $(BUILD)
	iverilog -g2012 -s tb_fir_filter -o $(BUILD)/tb_fir_filter.vvp rtl/fir_filter.v tb/tb_fir_filter.v
	vvp $(BUILD)/tb_fir_filter.vvp

synth:
	@echo "For mapped Sky130 synthesis, edit config/sky130_example.env then run scripts/run_fir_flow.sh."
	@echo "For a technology-independent Yosys check, run:"
	@echo "yosys -p 'read_verilog -sv rtl/fir_filter.v; hierarchy -check -top fir_filter; proc; opt; check'"

features:
	python3 scripts/extract_fir_features.py \
		--netlist-json "$(NETLIST_JSON)" \
		--run-dir "$(FIR_RUN_DIR)" \
		--outdir "$(FIR_RUN_DIR)" \
		--design-id "$(DESIGN_ID)" \
		--run-id "$(RUN_ID)" \
		--taps "$(TAPS)" \
		--data-width "$(DATA_WIDTH)" \
		--coeff-width "$(COEFF_WIDTH)" \
		--acc-width "$(ACC_WIDTH)" \
		--utilization "$(UTIL)" \
		--place-density "$(PLACE_DENSITY)" \
		--clock-period "$(CLK_PERIOD)"

report: features
	@echo ""
	@echo "===== Report files ====="
	@for f in \
		"$(FIR_RUN_DIR)/run_report.md" \
		"$(FIR_RUN_DIR)/features.csv" \
		"$(FIR_RUN_DIR)/physical_targets.csv" \
		"$(FIR_RUN_DIR)/fir_features.csv"; do \
		if [[ -s "$$f" ]]; then \
			echo "FOUND: $$f"; \
		else \
			echo "MISSING OR EMPTY: $$f"; \
		fi; \
	done

openroad:
	./scripts/run_fir_flow.sh

clean:
	rm -rf $(BUILD) fir_project/synth/* fir_project/runs/*
