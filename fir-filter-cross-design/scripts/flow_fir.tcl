
# FIR physical-design flow for OpenROAD / Sky130HD.
#
# Required environment:
# TECH_LEF, CELL_LEF, LIBERTY, NETLIST, SDC, RUN_DIR,
# UTIL (percent), PLACE_DENSITY (0..1).
# Optional: RUN_TAG.
#
# Congestion Boolean status is recorded separately from numerical
# congestion metrics. Do not interpret has_congestion as overflow.

foreach var {TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR UTIL PLACE_DENSITY} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "ERROR: required environment variable $var is not set"
        exit 1
    }
}

if {![string is double -strict $::env(UTIL)] ||
    $::env(UTIL) <= 0 || $::env(UTIL) >= 100} {
    puts stderr "ERROR: UTIL must be a number greater than 0 and less than 100"
    exit 1
}

if {![string is double -strict $::env(PLACE_DENSITY)] ||
    $::env(PLACE_DENSITY) <= 0 || $::env(PLACE_DENSITY) > 1} {
    puts stderr "ERROR: PLACE_DENSITY must be greater than 0 and at most 1"
    exit 1
}

foreach path_var {TECH_LEF CELL_LEF LIBERTY NETLIST SDC} {
    if {![file exists $::env($path_var)]} {
        puts stderr "ERROR: file specified by $path_var does not exist: $::env($path_var)"
        exit 1
    }
}

file mkdir $::env(RUN_DIR)

if {[info exists ::env(RUN_TAG)] && $::env(RUN_TAG) ne ""} {
    set ::fir_run_id $::env(RUN_TAG)
} else {
    set ::fir_run_id [file tail $::env(RUN_DIR)]
}

set ::fir_last_completed_stage "initialization"
set ::fir_current_stage "initialization"
set ::fir_status_csv "$::env(RUN_DIR)/fir_features_status.csv"
set ::fir_diagnostics "$::env(RUN_DIR)/flow_diagnostics.log"

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
        fir_status "FAILED" $stage
        return -options $options $result
    }

    set ::fir_last_completed_stage $stage
    fir_status "IN_PROGRESS" $::fir_last_completed_stage
    fir_log "STAGE_COMPLETED: $stage"
    return $result
}

# Reset status and diagnostics for this run.
set fd [open $::fir_status_csv w]
puts $fd "\"run_id\",\"status\",\"last_completed_stage\",\"unix_timestamp\""
close $fd

set fd [open $::fir_diagnostics w]
puts $fd "FIR physical-design diagnostics for $::fir_run_id"
close $fd

fir_status "STARTED" "initialization"

# ------------------------------------------------------------
# Technology and design loading
# ------------------------------------------------------------

fir_step "read_technology_lef" [list read_lef $::env(TECH_LEF)]
fir_step "read_cell_lef" [list read_lef $::env(CELL_LEF)]
fir_step "read_liberty" [list read_liberty $::env(LIBERTY)]
fir_step "read_netlist" [list read_verilog $::env(NETLIST)]
fir_step "link_design" [list link_design fir_filter]
fir_step "read_sdc" [list read_sdc $::env(SDC)]

# ------------------------------------------------------------
# Floorplan and routing-layer setup
# ------------------------------------------------------------

fir_step "initialize_floorplan" [list initialize_floorplan \
    -utilization $::env(UTIL) \
    -aspect_ratio 1.0 \
    -core_space 5.0 \
    -site unithd]

fir_step "make_tracks" [list make_tracks]
fir_step "set_wire_rc" [list set_wire_rc -layer met2]

# ------------------------------------------------------------
# I/O placement and standard-cell placement
# ------------------------------------------------------------

fir_step "place_io_pins" [list place_pins \
    -hor_layers met3 \
    -ver_layers met2]

fir_step "global_placement" [list global_placement \
    -density $::env(PLACE_DENSITY)]

# Optional optimization steps.
if {[catch {repair_design} result]} {
    fir_log "WARNING: repair_design failed or unavailable: $result"
} else {
    fir_log "OPTIONAL_STEP_COMPLETED: repair_design"
}

fir_step "detailed_placement" [list detailed_placement]

if {[catch {repair_timing -setup -setup_margin 0.2} result]} {
    fir_log "WARNING: repair_timing failed or unavailable: $result"
} else {
    fir_log "OPTIONAL_STEP_COMPLETED: repair_timing"
}

# ------------------------------------------------------------
# Global routing and congestion status
# ------------------------------------------------------------

set congestion_report "$::env(RUN_DIR)/congestion.rpt"
set congestion_status_report "$::env(RUN_DIR)/congestion_overflow.rpt"

fir_step "global_routing" [list global_route \
    -congestion_report_file $congestion_report \
    -verbose]

# This is a Boolean status, not a numerical overflow measurement.
if {[catch {has_congestion} congestion_flag]} {
    fir_log "GLOBAL_ROUTE_HAS_CONGESTION: unavailable"
    fir_log "WARNING: has_congestion query failed: $congestion_flag"

    set fd [open $congestion_status_report w]
    puts $fd "GLOBAL_ROUTE_STATUS: unavailable"
    close $fd
} else {
    set congestion_flag [expr {$congestion_flag ? 1 : 0}]

    set fd [open $congestion_status_report w]

    if {$congestion_flag} {
        puts $fd "GLOBAL_ROUTE_HAS_CONGESTION: true"
        fir_log "GLOBAL_ROUTE_HAS_CONGESTION: true"

        if {[catch {
            report_congestion >> $congestion_status_report
        } report_error]} {
            fir_log "WARNING: report_congestion failed: $report_error"
        }
    } else {
        puts $fd "GLOBAL_ROUTE_HAS_CONGESTION: false"
        puts $fd "No remaining global-routing congestion reported."
        fir_log "GLOBAL_ROUTE_HAS_CONGESTION: false"
    }

    close $fd
}

# Global-route timing diagnostics.
if {[catch {
    report_checks -path_delay max -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
} result]} {
    fir_log "WARNING: global-route setup timing report unavailable: $result"
}

if {[catch {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup_global_route.rpt"
} result]} {
    fir_log "WARNING: global-route WNS report unavailable: $result"
}

if {[catch {
    report_tns > "$::env(RUN_DIR)/tns_setup_global_route.rpt"
} result]} {
    fir_log "WARNING: global-route TNS report unavailable: $result"
}

fir_step "estimate_global_route_parasitics" \
    [list estimate_parasitics -global_routing]

# ------------------------------------------------------------
# Detailed routing
# ------------------------------------------------------------

fir_step "detailed_routing" [list detailed_route]

puts "SUCCESS: Detailed routing"
fir_log "SUCCESS: Detailed routing"

# ------------------------------------------------------------
# Final routed artifacts
# ------------------------------------------------------------

fir_step "write_final_def" \
    [list write_def "$::env(RUN_DIR)/final.def"]

fir_step "write_final_odb" \
    [list write_db "$::env(RUN_DIR)/final.odb"]

# ------------------------------------------------------------
# Final timing and area reports
# ------------------------------------------------------------

set report_commands {
    {setup_timing {report_checks -path_delay max -format full_clock_expanded} timing_setup.rpt}
    {hold_timing {report_checks -path_delay min -format full_clock_expanded} timing_hold.rpt}
    {setup_wns {report_worst_slack -max} wns_setup.rpt}
    {hold_wns {report_worst_slack -min} wns_hold.rpt}
    {setup_tns {report_tns} tns_setup.rpt}
    {design_area {report_design_area} design_area.rpt}
}

foreach report_entry $report_commands {
    lassign $report_entry report_name report_command report_file

    set report_path "$::env(RUN_DIR)/$report_file"

    if {[catch {
        uplevel #0 "$report_command > {$report_path}"
    } result]} {
        fir_log "WARNING: $report_name report unavailable: $result"
    }
}

# ------------------------------------------------------------
# Validate final outputs
# ------------------------------------------------------------

foreach artifact {final.def final.odb} {
    set artifact_path "$::env(RUN_DIR)/$artifact"

    if {![file exists $artifact_path] || [file size $artifact_path] == 0} {
        fir_log "ERROR: $artifact is missing or empty"
        fir_status "FAILED" $::fir_last_completed_stage
        error "$artifact is missing or empty"
    }
}

set ::fir_last_completed_stage "flow_completed"
fir_status "COMPLETED" $::fir_last_completed_stage
fir_log "FLOW_COMPLETED: $::env(RUN_DIR)"

puts "FIR physical-design flow completed. Results: $::env(RUN_DIR)"
