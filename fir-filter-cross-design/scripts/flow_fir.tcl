# FIR physical-design flow, kept separate from the existing systolic flow.
# Required environment: TECH_LEF, CELL_LEF, LIBERTY, NETLIST, SDC, RUN_DIR.
foreach var {TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR UTIL PLACE_DENSITY} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "ERROR: required environment variable $var is not set"
        exit 1
    }
}
file mkdir $::env(RUN_DIR)

read_lef $::env(TECH_LEF)
read_lef $::env(CELL_LEF)
read_liberty $::env(LIBERTY)
read_verilog $::env(NETLIST)
link_design fir_filter
read_sdc $::env(SDC)

# Keep these settings aligned with the systolic-array experiment when comparing.
initialize_floorplan -utilization $::env(UTIL) -aspect_ratio 1.0 -core_space 5.0 -site unithd
make_tracks
set_wire_rc -layer met2

# Placement and timing repair.
global_placement -density $::env(PLACE_DENSITY)
# Optional timing-driven optimization available in current OpenROAD builds.
catch {repair_design}
detailed_placement
catch {repair_timing -setup -setup_margin 0.2}

# Global and detailed routing.
global_route -congestion_report_file "$::env(RUN_DIR)/congestion.rpt"
estimate_parasitics -global_routing
report_checks -path_delay max -format full_clock_expanded > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
catch {report_worst_slack -max > "$::env(RUN_DIR)/wns_setup.rpt"}
catch {report_tns > "$::env(RUN_DIR)/tns_setup.rpt"}
detailed_route

# Save routed results and reports. Reports may differ slightly by OpenROAD build.
write_def "$::env(RUN_DIR)/final.def"
write_db "$::env(RUN_DIR)/final.odb"
catch {report_checks -path_delay max -format full_clock_expanded > "$::env(RUN_DIR)/timing_setup.rpt"}
catch {report_checks -path_delay min -format full_clock_expanded > "$::env(RUN_DIR)/timing_hold.rpt"}
catch {report_worst_slack -max > "$::env(RUN_DIR)/wns_setup.rpt"}
catch {report_worst_slack -min > "$::env(RUN_DIR)/wns_hold.rpt"}
catch {report_tns > "$::env(RUN_DIR)/tns_setup.rpt"}
catch {report_design_area > "$::env(RUN_DIR)/design_area.rpt"}
puts "FIR physical-design flow completed. Results: $::env(RUN_DIR)"
