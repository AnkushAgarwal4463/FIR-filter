
# FIR physical-design flow, kept separate from the existing systolic flow.
# Required environment:
# TECH_LEF, CELL_LEF, LIBERTY, NETLIST, SDC, RUN_DIR, UTIL, PLACE_DENSITY

foreach var {TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR UTIL PLACE_DENSITY} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "ERROR: required environment variable $var is not set"
        exit 1
    }
}

file mkdir $::env(RUN_DIR)

# ------------------------------------------------------------
# 1. Load technology, standard cells, and synthesized design
# ------------------------------------------------------------
puts "===== Read technology and libraries ====="
read_lef $::env(TECH_LEF)
read_lef $::env(CELL_LEF)
read_liberty $::env(LIBERTY)

puts "===== Read synthesized netlist ====="
read_verilog $::env(NETLIST)
link_design fir_filter

puts "===== Read timing constraints ====="
read_sdc $::env(SDC)

# ------------------------------------------------------------
# 2. Floorplan
# ------------------------------------------------------------
puts "===== Initialize floorplan ====="
initialize_floorplan \
    -utilization $::env(UTIL) \
    -aspect_ratio 1.0 \
    -core_space 5.0 \
    -site unithd

make_tracks
set_wire_rc -layer met2

# ------------------------------------------------------------
# 3. Place I/O pins before standard-cell placement
# ------------------------------------------------------------
puts "===== Place I/O pins ====="
# Confirm met3 and met2 exist in the loaded technology LEF.
# met3: horizontal pin layer; met2: vertical pin layer.
place_pins -hor_layers met3 -ver_layers met2

# ------------------------------------------------------------
# 4. Global placement and timing optimization
# ------------------------------------------------------------
puts "===== Global placement ====="
global_placement -density $::env(PLACE_DENSITY)

puts "===== Repair design ====="
if {[catch {repair_design} repair_msg]} {
    puts "WARNING: repair_design was skipped: $repair_msg"
}

# ------------------------------------------------------------
# 5. Detailed placement
# ------------------------------------------------------------
puts "===== Detailed placement ====="
detailed_placement

puts "===== Setup timing repair ====="
if {[catch {repair_timing -setup -setup_margin 0.2} timing_msg]} {
    puts "WARNING: repair_timing was skipped: $timing_msg"
}

# ------------------------------------------------------------
# 6. Global routing and timing reports
# ------------------------------------------------------------
puts "===== Global routing ====="
global_route \
    -congestion_report_file "$::env(RUN_DIR)/congestion.rpt"

estimate_parasitics -global_routing

catch {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
}

catch {
    report_worst_slack -max > "$::env(RUN_DIR)/wns_setup.rpt"
}

catch {
    report_tns > "$::env(RUN_DIR)/tns_setup.rpt"
}

# ------------------------------------------------------------
# 7. Detailed routing
# ------------------------------------------------------------
puts "===== Detailed routing ====="
detailed_route

# ------------------------------------------------------------
# 8. Save routed design and reports
# ------------------------------------------------------------
puts "===== Save final design ====="
write_def "$::env(RUN_DIR)/final.def"
write_db  "$::env(RUN_DIR)/final.odb"

puts "===== Generate final timing reports ====="
catch {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup.rpt"
}

catch {
    report_checks \
        -path_delay min \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_hold.rpt"
}

catch {
    report_worst_slack -max > "$::env(RUN_DIR)/wns_setup.rpt"
}

catch {
    report_worst_slack -min > "$::env(RUN_DIR)/wns_hold.rpt"
}

catch {
    report_tns > "$::env(RUN_DIR)/tns_setup.rpt"
}

catch {
    report_design_area > "$::env(RUN_DIR)/design_area.rpt"
}

puts "FIR physical-design flow completed. Results: $::env(RUN_DIR)"
