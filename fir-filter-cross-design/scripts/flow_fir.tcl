
# ============================================================
# FIR physical-design flow with step-level diagnostics
# Kept separate from the existing systolic-array flow.
#
# Required environment:
# TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR UTIL PLACE_DENSITY
# ============================================================

# ------------------------------------------------------------
# 0. Validate environment
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

set ::diag_log "$::env(RUN_DIR)/flow_diagnostics.log"
set ::diag_fd [open $::diag_log "a"]
fconfigure $::diag_fd -buffering line

proc log_msg {message} {
    puts $message
    puts $::diag_fd $message
    flush $::diag_fd
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
        log_msg "Diagnostic log: $::diag_log"

        close $::diag_fd
        return -options $options $result
    }

    log_msg "SUCCESS: $label"
    log_msg "Elapsed: [expr {[clock seconds] - $start_time}] seconds"
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
        return 0
    }

    log_msg "SUCCESS: $label"
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

log_msg ""
log_msg "############################################################"
log_msg "FIR PHYSICAL-DESIGN FLOW"
log_msg "Started: [clock format [clock seconds]]"
log_msg "RUN_DIR: $::env(RUN_DIR)"
log_msg "UTIL: $::env(UTIL)"
log_msg "PLACE_DENSITY: $::env(PLACE_DENSITY)"
log_msg "NETLIST: $::env(NETLIST)"
log_msg "SDC: $::env(SDC)"
log_msg "############################################################"

# ------------------------------------------------------------
# 1. Validate input files
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
# 2. Read technology, libraries, netlist and constraints
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
# 3. Floorplan and routing setup
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
# 4. Place I/O pins
# ------------------------------------------------------------
run_step "Place I/O pins" {
    place_pins -hor_layers met3 -ver_layers met2
}

# ------------------------------------------------------------
# 5. Global placement
# ------------------------------------------------------------
run_step "Global placement" {
    global_placement -density $::env(PLACE_DENSITY)
}

# Save a checkpoint when supported by this OpenROAD build.
optional_step "Save post-global-placement database" {
    write_db "$::env(RUN_DIR)/after_global_placement.odb"
}

# ------------------------------------------------------------
# 6. Design repair and detailed placement
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
# 7. Timing repair
# ------------------------------------------------------------
optional_step "Setup timing repair" {
    repair_timing -setup -setup_margin 0.2
}

# ------------------------------------------------------------
# 8. Global routing
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
# 9. Global-route timing reports
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
# 10. Detailed routing
# ------------------------------------------------------------
run_step "Detailed routing" {
    detailed_route
}

optional_step "Save post-detailed-routing database" {
    write_db "$::env(RUN_DIR)/after_detailed_route.odb"
}

# ------------------------------------------------------------
# 11. Save final design
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
# 12. Final timing and area reports
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
# 13. Completion summary
# ------------------------------------------------------------
log_msg ""
log_msg "############################################################"
log_msg "FIR PHYSICAL-DESIGN FLOW COMPLETED"
log_msg "Finished: [clock format [clock seconds]]"
log_msg "Results directory: $::env(RUN_DIR)"
log_msg "Diagnostic log: $::diag_log"
log_msg "############################################################"

close $::diag_fd
