
# ============================================================
# FIR physical-design flow with step-level diagnostics
# Kept separate from the existing systolic-array flow.
#
# Required environment:
# TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR UTIL PLACE_DENSITY
#
# Optional configuration:
# TAPS DATA_WIDTH COEFF_WIDTH ACC_WIDTH CLK_PERIOD_NS or CLK_PERIOD
# ============================================================

# ------------------------------------------------------------
# 0. Validate environment and initialize run directory
# ------------------------------------------------------------
foreach var {
    TECH_LEF CELL_LEF LIBERTY NETLIST SDC
    RUN_DIR UTIL PLACE_DENSITY
} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "FATAL: required environment variable '$var' is not set"
        exit 1
    }
}

file mkdir $::env(RUN_DIR)

# ------------------------------------------------------------
# 1. CSV helpers and persistent stage tracking
# ------------------------------------------------------------
set ::feature_csv "$::env(RUN_DIR)/fir_features.csv"
set ::status_csv "$::env(RUN_DIR)/fir_features_status.csv"
set ::feature_status "IN_PROGRESS"
set ::last_completed_stage "initialization"

set ::taps ""
set ::data_width ""
set ::coeff_width ""
set ::acc_width ""
set ::clk_period ""

foreach {env_name variable_name} {
    TAPS taps
    DATA_WIDTH data_width
    COEFF_WIDTH coeff_width
    ACC_WIDTH acc_width
} {
    if {[info exists ::env($env_name)]} {
        set ::$variable_name $::env($env_name)
    }
}

# Accept either clock-period variable name.
if {[info exists ::env(CLK_PERIOD_NS)] && $::env(CLK_PERIOD_NS) ne ""} {
    set ::clk_period $::env(CLK_PERIOD_NS)
} elseif {[info exists ::env(CLK_PERIOD)] && $::env(CLK_PERIOD) ne ""} {
    set ::clk_period $::env(CLK_PERIOD)
}

proc csv_escape {value} {
    set value [string map [list "\"" "\"\""] $value]
    return "\"$value\""
}

proc csv_write_row {fd values} {
    set escaped {}
    foreach value $values {
        lappend escaped [csv_escape $value]
    }
    puts $fd [join $escaped ","]
}

proc write_feature_snapshot {} {
    set header {
        run_id taps data_width coeff_width acc_width
        utilization place_density clk_period_ns
        flow_status last_completed_stage
    }

    set values [list \
        [file tail $::env(RUN_DIR)] \
        $::taps \
        $::data_width \
        $::coeff_width \
        $::acc_width \
        $::env(UTIL) \
        $::env(PLACE_DENSITY) \
        $::clk_period \
        $::feature_status \
        $::last_completed_stage \
    ]

    # Rewrite the snapshot so it always contains exactly one header
    # row and one data row, even when status changes during the flow.
    set fd [open $::feature_csv "w"]
    fconfigure $fd -buffering line
    csv_write_row $fd $header
    csv_write_row $fd $values
    close $fd
}

proc append_status_history {} {
    set needs_header 1

    if {[file exists $::status_csv] && [file size $::status_csv] > 0} {
        set needs_header 0
    }

    set fd [open $::status_csv "a"]
    fconfigure $fd -buffering line

    if {$needs_header} {
        csv_write_row $fd {
            run_id status last_completed_stage unix_timestamp
        }
    }

    csv_write_row $fd [list \
        [file tail $::env(RUN_DIR)] \
        $::feature_status \
        $::last_completed_stage \
        [clock seconds] \
    ]

    close $fd
}

proc update_feature_status {status stage} {
    set ::feature_status $status
    set ::last_completed_stage $stage

    write_feature_snapshot
    append_status_history
}

# Create initial CSV files before any physical-design steps run.
update_feature_status "IN_PROGRESS" "initialization"

# ------------------------------------------------------------
# 2. Diagnostic logging
# ------------------------------------------------------------
set ::diag_log "$::env(RUN_DIR)/flow_diagnostics.log"
set ::diag_fd [open $::diag_log "a"]
fconfigure $::diag_fd -buffering line

proc log_msg {message} {
    puts $message
    if {[info exists ::diag_fd] && $::diag_fd ne ""} {
        puts $::diag_fd $message
        flush $::diag_fd
    }
}

proc close_diag_log {} {
    if {[info exists ::diag_fd] && $::diag_fd ne ""} {
        close $::diag_fd
        set ::diag_fd ""
    }
}

proc run_step {label script} {
    log_msg ""
    log_msg "============================================================"
    log_msg "START: $label"
    log_msg "============================================================"

    set start_time [clock seconds]

    if {[catch {uplevel 1 $script} result options]} {
        log_msg "FAILED: $label"
        log_msg "ERROR: $result"

        if {[dict exists $options -errorcode]} {
            log_msg "ERROR CODE: [dict get $options -errorcode]"
        }

        if {[dict exists $options -errorinfo]} {
            log_msg "TCL STACK TRACE:"
            log_msg [dict get $options -errorinfo]
        }

        log_msg "Elapsed before failure: [expr {[clock seconds] - $start_time}] seconds"

        # Preserve the last successfully completed stage.
        update_feature_status "FAILED" $::last_completed_stage

        log_msg "Diagnostic log: $::diag_log"
        close_diag_log

        return -options $options $result
    }

    log_msg "SUCCESS: $label"
    log_msg "Elapsed: [expr {[clock seconds] - $start_time}] seconds"

    update_feature_status "IN_PROGRESS" $label
    return $result
}

proc optional_step {label script} {
    log_msg "OPTIONAL: $label"

    if {[catch {uplevel 1 $script} result options]} {
        log_msg "WARNING: $label failed"
        log_msg "Reason: $result"

        if {[dict exists $options -errorinfo]} {
            log_msg [dict get $options -errorinfo]
        }

        # Optional failure does not erase the last successful stage.
        # The warning remains in flow_diagnostics.log.
        return 0
    }

    log_msg "SUCCESS: $label"
    update_feature_status "IN_PROGRESS" $label
    return 1
}

proc require_file {path description} {
    if {![file exists $path]} {
        error "$description was not created: $path"
    }

    if {[file size $path] == 0} {
        error "$description exists but is empty: $path"
    }

    log_msg "ARTIFACT OK: $description ($path, [file size $path] bytes)"
}

# ------------------------------------------------------------
# 3. Startup information
# ------------------------------------------------------------
log_msg ""
log_msg "############################################################"
log_msg "FIR PHYSICAL-DESIGN FLOW"
log_msg "Started (Unix timestamp): [clock seconds]"
log_msg "RUN_DIR: $::env(RUN_DIR)"
log_msg "UTIL: $::env(UTIL)"
log_msg "PLACE_DENSITY: $::env(PLACE_DENSITY)"
log_msg "CLK_PERIOD_NS: $::clk_period"
log_msg "NETLIST: $::env(NETLIST)"
log_msg "SDC: $::env(SDC)"
log_msg "Feature CSV: $::feature_csv"
log_msg "Status history: $::status_csv"
log_msg "############################################################"

# ------------------------------------------------------------
# 4. Validate input files
# ------------------------------------------------------------
run_step "Validate input files" {
    foreach var {TECH_LEF CELL_LEF LIBERTY NETLIST SDC} {
        set path $::env($var)

        if {![file exists $path]} {
            error "$var file does not exist: $path"
        }

        if {[file size $path] == 0} {
            error "$var file is empty: $path"
        }

        log_msg "$var OK: $path"
    }
}

# ------------------------------------------------------------
# 5. Read technology, libraries, netlist and constraints
# ------------------------------------------------------------
run_step "Read technology LEF" {
    read_lef $::env(TECH_LEF)
}

run_step "Read standard-cell LEF" {
    read_lef $::env(CELL_LEF)
}

run_step "Read Liberty library" {
    read_liberty $::env(LIBERTY)
}

run_step "Read synthesized netlist" {
    read_verilog $::env(NETLIST)
}

run_step "Link design" {
    link_design fir_filter
}

run_step "Read SDC constraints" {
    read_sdc $::env(SDC)
}

# ------------------------------------------------------------
# 6. Floorplan and routing setup
# ------------------------------------------------------------
run_step "Initialize floorplan" {
    initialize_floorplan \
        -utilization $::env(UTIL) \
        -aspect_ratio 1.0 \
        -core_space 5.0 \
        -site unithd
}

run_step "Create routing tracks" {
    make_tracks
}

run_step "Configure wire RC" {
    set_wire_rc -layer met2
}

# ------------------------------------------------------------
# 7. Place I/O pins
# ------------------------------------------------------------
run_step "Place I/O pins" {
    place_pins -hor_layers met3 -ver_layers met2
}

# ------------------------------------------------------------
# 8. Global placement
# ------------------------------------------------------------
run_step "Global placement" {
    global_placement -density $::env(PLACE_DENSITY)
}

optional_step "Save post-global-placement database" {
    write_db "$::env(RUN_DIR)/after_global_placement.odb"
}

# ------------------------------------------------------------
# 9. Design repair and detailed placement
# ------------------------------------------------------------
run_step "Repair design" {
    repair_design
}

run_step "Detailed placement" {
    detailed_placement
}

optional_step "Save post-placement database" {
    write_db "$::env(RUN_DIR)/after_placement.odb"
}

# ------------------------------------------------------------
# 10. Setup timing repair
# ------------------------------------------------------------
optional_step "Setup timing repair" {
    repair_timing -setup -setup_margin 0.2
}

# ------------------------------------------------------------
# 11. Global routing
# ------------------------------------------------------------
run_step "Global routing" {
    global_route \
        -congestion_report_file "$::env(RUN_DIR)/congestion.rpt"
}

optional_step "Save post-global-routing database" {
    write_db "$::env(RUN_DIR)/after_global_route.odb"
}

optional_step "Estimate global-route parasitics" {
    estimate_parasitics -global_routing
}

# ------------------------------------------------------------
# 12. Global-route timing reports
# ------------------------------------------------------------
optional_step "Global-route setup timing report" {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
}

optional_step "Global-route WNS report" {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup_global_route.rpt"
}

optional_step "Global-route TNS report" {
    report_tns \
        > "$::env(RUN_DIR)/tns_setup_global_route.rpt"
}

# ------------------------------------------------------------
# 13. Detailed routing
# ------------------------------------------------------------
run_step "Detailed routing" {
    detailed_route
}

optional_step "Save post-detailed-routing database" {
    write_db "$::env(RUN_DIR)/after_detailed_route.odb"
}

# ------------------------------------------------------------
# 14. Save and validate final design
# ------------------------------------------------------------
run_step "Write final DEF" {
    write_def "$::env(RUN_DIR)/final.def"
}

run_step "Write final OpenROAD database" {
    write_db "$::env(RUN_DIR)/final.odb"
}

run_step "Validate final design artifacts" {
    require_file "$::env(RUN_DIR)/final.def" "Final DEF"
    require_file "$::env(RUN_DIR)/final.odb" "Final ODB"
}

# ------------------------------------------------------------
# 15. Final timing and area reports
# ------------------------------------------------------------
optional_step "Final setup timing report" {
    report_checks \
        -path_delay max \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup.rpt"
}

optional_step "Final hold timing report" {
    report_checks \
        -path_delay min \
        -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_hold.rpt"
}

optional_step "Final setup WNS report" {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup.rpt"
}

optional_step "Final hold WNS report" {
    report_worst_slack -min \
        > "$::env(RUN_DIR)/wns_hold.rpt"
}

optional_step "Final TNS report" {
    report_tns \
        > "$::env(RUN_DIR)/tns_setup.rpt"
}

optional_step "Final design area report" {
    report_design_area \
        > "$::env(RUN_DIR)/design_area.rpt"
}

# ------------------------------------------------------------
# 16. Completion summary
# ------------------------------------------------------------
log_msg ""
log_msg "############################################################"
log_msg "FIR PHYSICAL-DESIGN FLOW COMPLETED"
log_msg "Finished (Unix timestamp): [clock seconds]"
log_msg "Results directory: $::env(RUN_DIR)"
log_msg "Diagnostic log: $::diag_log"
log_msg "############################################################"

update_feature_status "COMPLETED" "flow_completed"
close_diag_log
