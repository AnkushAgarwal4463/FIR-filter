
# FIR physical-design flow for OpenROAD / Sky130HD.
#
# Required environment:
# TECH_LEF, CELL_LEF, LIBERTY, NETLIST, SDC, RUN_DIR,
# UTIL (percent), PLACE_DENSITY (0..1)
#
# Optional environment:
# RUN_TAG
# ENABLE_REPAIR_TIMING (0 = disabled, 1 = enabled; default = 1)

# ------------------------------------------------------------
# 1. Validate required environment variables
# ------------------------------------------------------------

foreach var {
    TECH_LEF CELL_LEF LIBERTY NETLIST SDC
    RUN_DIR UTIL PLACE_DENSITY
} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "ERROR: required environment variable $var is not set"
        exit 1
    }
}

# Validate timing-repair switch.
set enable_repair_timing 1

if {[info exists ::env(ENABLE_REPAIR_TIMING)]} {
    set enable_repair_timing $::env(ENABLE_REPAIR_TIMING)
}

if {$enable_repair_timing ni {0 1}} {
    puts stderr "ERROR: ENABLE_REPAIR_TIMING must be 0 or 1"
    exit 1
}

# ------------------------------------------------------------
# 2. Initialize run information
# ------------------------------------------------------------

file mkdir $::env(RUN_DIR)

if {[info exists ::env(RUN_TAG)] && $::env(RUN_TAG) ne ""} {
    set ::fir_run_id $::env(RUN_TAG)
} else {
    set ::fir_run_id [file tail $::env(RUN_DIR)]
}

set ::fir_last_completed_stage "initialization"
set ::fir_status_csv "$::env(RUN_DIR)/fir_features_status.csv"
set ::fir_diagnostics "$::env(RUN_DIR)/flow_diagnostics.log"

# ------------------------------------------------------------
# 3. Logging and status helper procedures
# ------------------------------------------------------------

proc fir_csv_escape {value} {
    set value [string map [list "\r" " " "\n" " "] $value]
    set value [string map [list "\"" "\"\""] $value]
    return "\"$value\""
}

proc fir_status {status stage} {
    set fd [open $::fir_status_csv a]
    puts $fd "[fir_csv_escape $::fir_run_id],[fir_csv_escape $status],[fir_csv_escape $stage],[clock seconds]"
    close $fd
}

proc fir_log {message} {
    puts $message

    set fd [open $::fir_diagnostics a]
    puts $fd $message
    close $fd
}

proc fir_step {stage command_list} {
    set ::fir_current_stage $stage

    fir_log "STAGE: $stage"
    fir_status "IN_PROGRESS" $::fir_last_completed_stage

    if {[catch {uplevel #0 $command_list} result options]} {
        fir_log "ERROR in stage '$stage': $result"
        fir_status "FAILED" $::fir_last_completed_stage

        return -options $options $result
    }

    set ::fir_last_completed_stage $stage

    fir_status "IN_PROGRESS" $::fir_last_completed_stage
    fir_log "STAGE_COMPLETED: $stage"

    return $result
}

# ------------------------------------------------------------
# 4. Reset run-specific status and diagnostic files
# ------------------------------------------------------------

set fd [open $::fir_status_csv w]
puts $fd "\"run_id\",\"status\",\"last_completed_stage\",\"unix_timestamp\""
close $fd

set fd [open $::fir_diagnostics w]
puts $fd "FIR physical-design diagnostics for $::fir_run_id"
close $fd

fir_status "STARTED" "initialization"

fir_log "RUN_ID: $::fir_run_id"
fir_log "ENABLE_REPAIR_TIMING: $enable_repair_timing"

# ------------------------------------------------------------
# 5. Read technology and design
# ------------------------------------------------------------

fir_step "read_technology_lef" [list read_lef $::env(TECH_LEF)]

fir_step "read_cell_lef" [list read_lef $::env(CELL_LEF)]

fir_step "read_liberty" [list read_liberty $::env(LIBERTY)]

fir_step "read_netlist" [list read_verilog $::env(NETLIST)]

fir_step "link_design" [list link_design fir_filter]

fir_step "read_sdc" [list read_sdc $::env(SDC)]

# ------------------------------------------------------------
# 6. Floorplan and routing-layer setup
# ------------------------------------------------------------

fir_step "initialize_floorplan" [list initialize_floorplan \
    -utilization $::env(UTIL) \
    -aspect_ratio 1.0 \
    -core_space 5.0 \
    -site unithd]

fir_step "make_tracks" [list make_tracks]

fir_step "set_wire_rc" [list set_wire_rc -layer met2]

# ------------------------------------------------------------
# 7. Top-level I/O pin placement
# ------------------------------------------------------------

fir_step "place_io_pins" [list place_pins \
    -hor_layers met3 \
    -ver_layers met2]

# ------------------------------------------------------------
# 8. Global placement
# ------------------------------------------------------------

fir_step "global_placement" [list global_placement \
    -density $::env(PLACE_DENSITY)]

# ------------------------------------------------------------
# 9. Design repair
# ------------------------------------------------------------

if {[catch {repair_design} result]} {
    fir_log "WARNING: repair_design was skipped/failed: $result"
} else {
    fir_log "OPTIONAL_STEP_COMPLETED: repair_design"
}

# ------------------------------------------------------------
# 10. Detailed placement
# ------------------------------------------------------------

fir_step "detailed_placement" [list detailed_placement]

# ------------------------------------------------------------
# 11. Optional timing repair
# ------------------------------------------------------------

if {$enable_repair_timing == 1} {

    fir_log "REPAIR_TIMING_ENABLED: true"

    if {[catch {
        repair_timing -setup -setup_margin 0.2
    } result]} {

        fir_log "WARNING: repair_timing was skipped/failed: $result"

    } else {

        fir_log "OPTIONAL_STEP_COMPLETED: repair_timing"
    }

} else {

    fir_log "REPAIR_TIMING_ENABLED: false"
    fir_log "OPTIONAL_STEP_SKIPPED: repair_timing"
}

# ------------------------------------------------------------
# 12. Global routing
# ------------------------------------------------------------

set congestion_report \
    "$::env(RUN_DIR)/congestion.rpt"

set congestion_overflow_report \
    "$::env(RUN_DIR)/congestion_overflow.rpt"

fir_step "global_routing" [list global_route \
    -congestion_report_file $congestion_report \
    -verbose]

# ------------------------------------------------------------
# 13. Congestion measurement
# ------------------------------------------------------------

if {[catch {has_congestion} congestion_flag]} {

    set congestion_status "unavailable"

    fir_log "GLOBAL_ROUTE_HAS_CONGESTION: unavailable"

    fir_log "WARNING: Could not query global-routing congestion: $congestion_flag"

} else {

    set congestion_flag [expr {$congestion_flag ? 1 : 0}]

    if {$congestion_flag} {
        set congestion_status "true"
    } else {
        set congestion_status "false"
    }

    fir_log "GLOBAL_ROUTE_HAS_CONGESTION: $congestion_status"
}

# Generate detailed congestion report.

set report_generated 0

if {[catch {report_congestion} report_result]} {

    fir_log "WARNING: report_congestion failed: $report_result"

} else {

    set report_generated 1

    fir_log "CONGESTION_REPORT_GENERATED"
}

# Save congestion status.

set fd [open $congestion_overflow_report w]

puts $fd "GLOBAL_ROUTE_HAS_CONGESTION: $congestion_status"

puts $fd "CONGESTION_REPORT_GENERATED: $report_generated"

close $fd

# ------------------------------------------------------------
# 14. Global-route parasitic estimation and timing reports
# ------------------------------------------------------------

fir_step "estimate_global_route_parasitics" \
    [list estimate_parasitics -global_routing]

catch {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
}

catch {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup_global_route.rpt"
}

catch {
    report_tns \
        > "$::env(RUN_DIR)/tns_setup_global_route.rpt"
}

# ------------------------------------------------------------
# 15. Save pre-detailed-routing checkpoints
# ------------------------------------------------------------

# These checkpoints remain available even if detailed routing fails.

fir_step "write_pre_drt_odb" \
    [list write_db "$::env(RUN_DIR)/pre_drt.odb"]

fir_step "write_pre_drt_def" \
    [list write_def "$::env(RUN_DIR)/pre_drt.def"]

fir_log "PRE_DRT_CHECKPOINTS_SAVED"

# ------------------------------------------------------------
# 16. Detailed routing
# ------------------------------------------------------------

fir_step "detailed_routing" [list detailed_route]

puts "SUCCESS: Detailed routing"

fir_log "SUCCESS: Detailed routing"

# ------------------------------------------------------------
# 17. Save final routed artifacts
# ------------------------------------------------------------

fir_step "write_final_def" \
    [list write_def "$::env(RUN_DIR)/final.def"]

fir_step "write_final_odb" \
    [list write_db "$::env(RUN_DIR)/final.odb"]

# ------------------------------------------------------------
# 18. Final timing and area reports
# ------------------------------------------------------------

if {[catch {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup.rpt"
} result]} {
    fir_log "WARNING: setup timing report unavailable: $result"
}

if {[catch {
    report_checks \
        -path_delay min \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_hold.rpt"
} result]} {
    fir_log "WARNING: hold timing report unavailable: $result"
}

if {[catch {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup.rpt"
} result]} {
    fir_log "WARNING: setup WNS report unavailable: $result"
}

if {[catch {
    report_worst_slack -min \
        > "$::env(RUN_DIR)/wns_hold.rpt"
} result]} {
    fir_log "WARNING: hold WNS report unavailable: $result"
}

if {[catch {
    report_tns \
        > "$::env(RUN_DIR)/tns_setup.rpt"
} result]} {
    fir_log "WARNING: TNS report unavailable: $result"
}

if {[catch {
    report_design_area \
        > "$::env(RUN_DIR)/design_area.rpt"
} result]} {
    fir_log "WARNING: design-area report unavailable: $result"
}

# ------------------------------------------------------------
# 19. Validate final routed artifacts
# ------------------------------------------------------------

if {
    ![file exists "$::env(RUN_DIR)/final.def"] ||
    [file size "$::env(RUN_DIR)/final.def"] == 0
} {

    fir_log "ERROR: final.def is missing or empty"

    fir_status "FAILED" $::fir_last_completed_stage

    error "final.def is missing or empty"
}

if {
    ![file exists "$::env(RUN_DIR)/final.odb"] ||
    [file size "$::env(RUN_DIR)/final.odb"] == 0
} {

    fir_log "ERROR: final.odb is missing or empty"

    fir_status "FAILED" $::fir_last_completed_stage

    error "final.odb is missing or empty"
}

# ------------------------------------------------------------
# 20. Mark flow as completed
# ------------------------------------------------------------

set ::fir_last_completed_stage "flow_completed"

fir_status "COMPLETED" $::fir_last_completed_stage

fir_log "FLOW_COMPLETED: $::env(RUN_DIR)"

puts "FIR physical-design flow completed. Results: $::env(RUN_DIR)"
