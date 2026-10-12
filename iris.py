#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bhavya Bansal / BCDC (Bansal Chip Design Corporation)
"""
============================================================================
  I.R.I.S. -- Integrated RTL-to-Implementation System          (Mark I)
  by BCDC -- Bansal Chip Design Corporation
============================================================================
  A tkinter GUI (and headless CLI) that produces and runs the full script
  set for an RTL -> GDSII tape-out flow:
      Verilator lint + Icarus sim + Yosys synthesis + OpenROAD PnR +
      KLayout GDS merge + KLayout DRC

  Toolchain modes (Runtime tab):
    bcdc    -- everything comes from the pinned toolchain installed by
               install_bcdc.sh (~/.bcdc/env.sh). No nix-shell per stage.
    legacy  -- old behaviour: source OSS CAD Suite for yosys, run openroad
               inside the OpenLane2 nix-shell.

  Generates:
    01_synth.ys                Yosys synthesis script
    dff_map.v / latch_map.v    Yosys techmap helpers
    02_design.sdc              Timing constraints
    03_floorplan.tcl           OpenROAD floorplan
    04_padframe.tcl            OpenROAD padframe (auto-pitch)
    05_place.tcl               OpenROAD placement
    06_cts.tcl                 OpenROAD CTS
    07_route.tcl               OpenROAD detailed routing
    08_reports.tcl             OpenROAD post-route reports (area/timing/power)
    09_gds_merge.py            KLayout DEF->GDS extractor
    10_view.tcl                OpenROAD GUI loader (latest stage DB)
    run_flow.sh                Bash orchestrator (same commands as the GUI)

  CLI:
    python3 PROGRAM.py                                   # GUI
    python3 PROGRAM.py cfg.json --headless [--stages synth,floorplan,...]
    python3 PROGRAM.py cfg.json --generate-only --out DIR
    python3 PROGRAM.py --list-stages
============================================================================
"""

import argparse
import copy
import json
import os
import sys
import shlex
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime

PRODUCT   = "IRIS"
PRODUCT_L = "Integrated RTL-to-Implementation System"
MARK      = "Mark I"
COMPANY   = "BCDC - Bansal Chip Design Corporation"
GOLD, NAVY, CYAN = "#c9a24a", "#0a1a2f", "#00d4ff"

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAVE_TK = True
except Exception:          # headless machines without python3-tk
    HAVE_TK = False


# =============================================================================
#                              PDK PROFILES
# =============================================================================
# Each profile overrides fields under DEFAULTS['pdk'] plus 'routing.bottom_layer'
# /'top_layer'. Users select from the GUI's "Profile" dropdown to auto-populate
# cell names, metal layers and paths.
#
# Only OPEN PDKs ship here. Proprietary / NDA PDK profiles go in a private file
# that is never committed:   ~/.bcdc/pdk_profiles.local.json
#   {"profiles": {"My Foundry 180nm": { ...same keys as below... }}}
# (or point IRIS_PDK_PROFILES at another JSON file).
PDK_PROFILES = {
    "SkyWater 130nm (5 metal)": {
        "pdk_root": "$HOME/pdks/sky130A",
        "lef_files": [
            "libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef",
            "libs.ref/sky130_fd_sc_hd/lef/sky130_fd_sc_hd.lef",
        ],
        "lib_file":  "libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
        "gds_files": ["libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds"],
        "site_name":     "unithd",
        "clock_buffer":  "sky130_fd_sc_hd__clkbuf_1",
        "buffer_cell":   "sky130_fd_sc_hd__buf_1",
        # globs OK (Yosys >= 0.40 and OpenROAD get_lib_cells)
        "dont_use_cells": ["*lpflow*", "*probe*"],
        "filler_cells":   ["sky130_fd_sc_hd__fill_1",
                            "sky130_fd_sc_hd__fill_2",
                            "sky130_fd_sc_hd__fill_4",
                            "sky130_fd_sc_hd__fill_8"],
        "tie_hi_cell": "sky130_fd_sc_hd__conb_1",
        "tie_lo_cell": "sky130_fd_sc_hd__conb_1",
        "cells": {
            "dff_reset_low":  "sky130_fd_sc_hd__dfrtp_1",
            "dff_reset_high": "sky130_fd_sc_hd__dfstp_1",
            "latch":          "sky130_fd_sc_hd__dlrtp_1",
        },
        "pads": {"input": "", "output": "", "vdd": "", "vss": "",
                  "corner": "", "corner_size_um": 0.0},
        "metal_layers": [   # pitches + offsets as used by OpenROAD-flow-scripts
            {"name": "li1",  "x_pitch": 0.46, "y_pitch": 0.34, "x_offset": 0.23, "y_offset": 0.17},
            {"name": "met1", "x_pitch": 0.34, "y_pitch": 0.34, "x_offset": 0.17, "y_offset": 0.17},
            {"name": "met2", "x_pitch": 0.46, "y_pitch": 0.46, "x_offset": 0.23, "y_offset": 0.23},
            {"name": "met3", "x_pitch": 0.68, "y_pitch": 0.68, "x_offset": 0.34, "y_offset": 0.34},
            {"name": "met4", "x_pitch": 0.92, "y_pitch": 0.92, "x_offset": 0.46, "y_offset": 0.46},
            {"name": "met5", "x_pitch": 3.4,  "y_pitch": 3.4,  "x_offset": 1.7,  "y_offset": 1.7},
        ],
        "routing_bottom": "met1", "routing_top": "met5",
        "ff_mapping": "dfflibmap",
        "tie_hi_pin": "HI", "tie_lo_pin": "LO",
        "io_hor_layer": "met3", "io_ver_layer": "met2",
        "tapcell": "sky130_fd_sc_hd__tapvpwrvgnd_1", "tap_distance": 14,
        "layer_map_file": "libs.tech/klayout/tech/sky130A.map",
        "pdn": {
            "enable": True,
            "power_pins":  "^(VDD|VPWR|VPB)$",
            "ground_pins": "^(VSS|VGND|VNB)$",
            "rail_layer": "met1", "rail_width": 0.48,
            "straps": [
                {"layer": "met4", "width": 1.6, "pitch": 27.14, "offset": 13.57},
                {"layer": "met5", "width": 1.6, "pitch": 27.2,  "offset": 13.6},
            ],
            "connect": [["met1", "met4"], ["met4", "met5"]],
        },
    },

    "GF 180nm MCU (5 metal)": {
        "pdk_root": "$HOME/pdks/gf180mcuC",
        "lef_files": [
            "libs.ref/gf180mcu_fd_sc_mcu7t5v0/techlef/gf180mcu_5LM_1TM_9K_9t_tech.lef",
            "libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef",
        ],
        "lib_file":  "libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.lib",
        "gds_files": ["libs.ref/gf180mcu_fd_sc_mcu7t5v0/gds/gf180mcu_fd_sc_mcu7t5v0.gds"],
        "site_name":     "GF018hv5v_mcu_sc7",
        "clock_buffer":  "gf180mcu_fd_sc_mcu7t5v0__clkbuf_1",
        "buffer_cell":   "gf180mcu_fd_sc_mcu7t5v0__buf_1",
        "dont_use_cells": [],
        "filler_cells":   ["gf180mcu_fd_sc_mcu7t5v0__fill_1",
                            "gf180mcu_fd_sc_mcu7t5v0__fill_2",
                            "gf180mcu_fd_sc_mcu7t5v0__fill_4",
                            "gf180mcu_fd_sc_mcu7t5v0__fill_8"],
        "tie_hi_cell": "gf180mcu_fd_sc_mcu7t5v0__tiehi",
        "tie_lo_cell": "gf180mcu_fd_sc_mcu7t5v0__tielo",
        "cells": {
            "dff_reset_low":  "gf180mcu_fd_sc_mcu7t5v0__dffrq_1",
            "dff_reset_high": "gf180mcu_fd_sc_mcu7t5v0__dffsq_1",
            "latch":          "gf180mcu_fd_sc_mcu7t5v0__latq_1",
        },
        "pads": {"input": "", "output": "", "vdd": "", "vss": "",
                  "corner": "", "corner_size_um": 0.0},
        "metal_layers": [
            {"name": "Metal1", "x_pitch": 0.28, "y_pitch": 0.28},
            {"name": "Metal2", "x_pitch": 0.28, "y_pitch": 0.28},
            {"name": "Metal3", "x_pitch": 0.28, "y_pitch": 0.28},
            {"name": "Metal4", "x_pitch": 0.28, "y_pitch": 0.28},
            {"name": "Metal5", "x_pitch": 0.44, "y_pitch": 0.44},
        ],
        "routing_bottom": "Metal1", "routing_top": "Metal5",
        "ff_mapping": "dfflibmap",
        "tie_hi_pin": "Z", "tie_lo_pin": "ZN",
        "io_hor_layer": "Metal3", "io_ver_layer": "Metal2",
        "tapcell": "", "tap_distance": 0,
        "layer_map_file": "",
        "pdn": {"enable": False},
    },

    "Custom (fill manually)": {
        "pdk_root": "", "lef_files": [], "lib_file": "", "gds_files": [],
        "site_name": "", "clock_buffer": "", "buffer_cell": "",
        "dont_use_cells": [], "filler_cells": [],
        "tie_hi_cell": "", "tie_lo_cell": "",
        "cells": {"dff_reset_low": "", "dff_reset_high": "", "latch": ""},
        "pads":  {"input": "", "output": "", "vdd": "", "vss": "",
                   "corner": "", "corner_size_um": 0.0},
        "metal_layers": [
            {"name": "metal1", "x_pitch": 1.0, "y_pitch": 1.0},
            {"name": "metal2", "x_pitch": 1.0, "y_pitch": 1.0},
        ],
        "routing_bottom": "metal1", "routing_top": "metal2",
        "ff_mapping": "dfflibmap",
        "tie_hi_pin": "", "tie_lo_pin": "",
        "io_hor_layer": "", "io_ver_layer": "",
        "tapcell": "", "tap_distance": 0,
        "layer_map_file": "",
        "pdn": {"enable": False},
    },
}

# =============================================================================
#                              DEFAULT CONFIG
# =============================================================================
DEFAULTS = {
    "project": {
        "top_module": "my_top",
        "rtl_dir": "",
        "rtl_files": [],
        "work_dir": "",
        "output_dir": "",
    },
    # pdk is filled from the default profile right after this dict
    "pdk": {},
    "verify": {
        "lint_enable": True,
        "verilator_flags": "-Wall -Wno-fatal",
        "sim_enable": True,
        "testbench_files": [],
        "tb_top": "",
        "iverilog_flags": "-g2012",
        # Sim stage fails if sim.log matches this (grep -E). Blank = never.
        "sim_fail_regex": "FAIL|ERROR|MISMATCH",
    },
    "synthesis": {
        "abc_D": 100000,
        "target_delay_ps": 100000,
        "flatten": True,
        "share_wreduce": True,
    },
    "sdc": {
        "clock_port": "clk",
        "clock_period_ns": 20.0,
        "clock_uncertainty_ns": 0.25,
        "hold_uncertainty_ns": 0.1,
        "io_delay_ns": 4.0,
        "reset_port": "rst_n",
        # True: reset is asynchronous -> false-path it.  False (sync reset,
        # e.g. picorv32 resetn): it is timed like any other input.
        "reset_is_async": True,
        # "ALL" = every input except the clock / every output
        "sync_input_ports": ["ALL"],
        "output_ports": ["ALL"],
        "output_load_pf": 0.0334,
        "max_fanout": 16,
        "max_transition_ns": 1.5,
    },
    "floorplan": {
        "die_size_um": 600.0,
        "core_margin_um": 20.0,
        "aspect_ratio": 1.0,
    },
    "padframe": {
        "enable": False,
        "corner_cell": "",
        "corner_size_um": 0.0,
        # per-side pad counts (sum = total pads)
        "pads_south": 4,
        "pads_east":  4,
        "pads_north": 4,
        "pads_west":  4,
        # default pad width used for pitch/spacing preview (um)
        "default_pad_width_um": 80.0,
        # default cell types for auto-generated pad list (from your IO library)
        "default_signal_pad": "",
        "default_output_pad": "",
        "default_vdd_pad":    "",
        "default_vss_pad":    "",
        # If True, auto-insert 1 VDD + 1 VSS pair on every side
        "auto_power_pairs":   True,
        # rows: [order, master_cell, instance_name, port_name, is_power]
        "pad_list": [],
    },
    "placement": {
        "density": 0.18,
        "init_density_penalty": 0.20,
        "init_wirelength_coef": 0.02,
        "min_phi_coef": 0.95,
        "max_phi_coef": 1.05,
        "overflow": 0.05,
        "pad_left": 4,
        "pad_right": 4,
        "routability_driven": True,
        "detailed_max_displacement": 2000,
    },
    "cts": {
        "wire_unit": 20,
        "distance_between_buffers": 100,
        "sink_clustering_size": 30,
        "sink_clustering_max_diameter": 60,
        "num_static_layers": 1,
    },
    "routing": {
        "bottom_layer": "met1",
        "top_layer": "met5",
        "iterations": 30,
        "verbose": 1,
        "clean_patches": True,
        "save_guide_updates": True,
    },
    "reports": {
        "activity": 0.1,
        "duty": 0.5,
    },
    "signoff": {
        # KLayout DRC runset (.lydrc / .drc). Blank = DRC stage skipped.
        # Invoked as: klayout -b -r DECK -rd input=GDS -rd topcell=TOP -rd report=RPT
        "drc_deck": "",
        "drc_extra_args": "",
    },
    # Hand-edited scripts saved from the Output/Preview tab.
    # {filename: full text}. Generate ALL writes these instead of regenerating.
    "script_overrides": {},
    "runtime": {
        # "bcdc"   = pinned toolchain from install_bcdc.sh (recommended)
        # "legacy" = OSS CAD Suite + OpenLane2 nix-shell (old behaviour)
        "toolchain_mode": "bcdc",
        "bcdc_env": "$HOME/.bcdc/env.sh",
        "openroad_bin": "openroad",
        "yosys_bin": "yosys",
        "klayout_bin": "klayout",
        "verilator_bin": "verilator",
        "iverilog_bin": "iverilog",
        "threads": 12,
        # ---- legacy-mode environment activation ----
        "yosys_env_activate":    "source $HOME/oss-cad-suite/environment",
        "openroad_use_nix":      True,
        "openlane_nix_dir":      "$HOME/openlane2",
        "klayout_env_activate":  "",
    },
}

DEFAULT_PROFILE = "SkyWater 130nm (5 metal)"
PROFILE_PDK_KEYS = ("pdk_root", "lef_files", "lib_file", "gds_files", "site_name",
                    "clock_buffer", "buffer_cell", "dont_use_cells", "filler_cells",
                    "tie_hi_cell", "tie_lo_cell", "cells", "cell_pins", "pads",
                    "metal_layers", "ff_mapping", "tie_hi_pin", "tie_lo_pin",
                    "io_hor_layer", "io_ver_layer", "tapcell", "tap_distance",
                    "layer_map_file", "pdn")
DEFAULT_CELL_PINS = {"clk": "CLK", "d": "D", "q": "Q", "reset_n": "RESET_B",
                     "set_n": "SET_B", "latch_en": "GATE", "latch_reset_n": "RESET_B"}


def _load_local_profiles():
    """Merge private PDK profiles (NDA kits) from local JSON files, if present."""
    paths = [os.path.join(os.environ.get("BCDC_HOME", os.path.expanduser("~/.bcdc")),
                          "pdk_profiles.local.json")]
    if os.environ.get("IRIS_PDK_PROFILES"):
        paths.insert(0, os.environ["IRIS_PDK_PROFILES"])
    for path in paths:
        try:
            with open(os.path.expanduser(path)) as f:
                data = json.load(f)
            PDK_PROFILES.update(data.get("profiles", {}))
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[IRIS] could not read {path}: {e}", file=sys.stderr)


def _pdk_from_profile(name):
    prof = PDK_PROFILES[name]
    pdk = {"active_profile": name, "cell_pins": dict(DEFAULT_CELL_PINS)}
    for k in PROFILE_PDK_KEYS:
        if k in prof:
            pdk[k] = copy.deepcopy(prof[k])
    return pdk


_load_local_profiles()
DEFAULTS["pdk"] = _pdk_from_profile(DEFAULT_PROFILE)

# Ordered flow stages: (key used on the CLI, label shown in the GUI)
STAGES = [
    ("lint",      "1. Lint (Verilator)"),
    ("sim",       "2. RTL Sim (Icarus)"),
    ("synth",     "3. Synthesis (Yosys)"),
    ("floorplan", "4. Floorplan + Padframe"),
    ("place",     "5. Placement"),
    ("cts",       "6. CTS"),
    ("route",     "7. Routing"),
    ("reports",   "8. Reports (post-route)"),
    ("gds",       "9. GDS extraction"),
    ("drc",       "10. DRC (KLayout)"),
]
STAGE_LABEL = dict(STAGES)
STAGE_KEYS = [k for k, _ in STAGES]


def xpath(s):
    """Expand $VARS and ~ in a user-supplied path (configs may use $HOME)."""
    return os.path.expanduser(os.path.expandvars(s)) if s else s


def deep_merge(base, override):
    """Return base updated recursively with override (so configs saved by an
       older PROGRAM.py still get every new key with its default)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config_file(path):
    with open(path) as f:
        data = f.read()
    if not data.strip():
        raise ValueError("File is empty")
    cfg = json.loads(data)
    if not isinstance(cfg, dict):
        raise ValueError("JSON root must be an object")
    return deep_merge(DEFAULTS, cfg)


def write_scripts(cfg, out):
    """Write every generated file + flow_config.json into `out`. Returns paths."""
    gen = ScriptGenerator(cfg)
    files = gen.all_files()
    files.update({k: v for k, v in (cfg.get("script_overrides") or {}).items()
                  if k in files})          # hand edits win over regeneration
    Path(out).mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in files.items():
        p = Path(out) / name
        p.write_text(content)
        if name.endswith(".sh"):
            try: p.chmod(0o755)
            except Exception: pass
        written.append(p)
    cfgp = Path(out) / "flow_config.json"
    cfgp.write_text(json.dumps(cfg, indent=2))
    written.append(cfgp)
    return written


# =============================================================================
#                            SCRIPT GENERATORS
# =============================================================================
class ScriptGenerator:
    """Generates every TCL / verilog helper / bash script from the config dict."""

    def __init__(self, cfg):
        # Work on a copy: generators must never mutate the GUI's config, and
        # $HOME / ~ in PDK + RTL paths must be expanded because Tcl, Yosys and
        # KLayout-python do NOT expand shell variables ($HOME in Tcl is an
        # undefined Tcl variable).
        self.cfg = copy.deepcopy(cfg)
        ex = lambda s: os.path.expanduser(os.path.expandvars(s)) if s else s
        self.cfg["pdk"]["pdk_root"] = ex(self.cfg["pdk"].get("pdk_root", ""))
        self.cfg["project"]["rtl_files"] = [ex(f) for f in self.cfg["project"]["rtl_files"]]
        self.cfg.setdefault("verify", copy.deepcopy(DEFAULTS["verify"]))
        self.cfg.setdefault("signoff", copy.deepcopy(DEFAULTS["signoff"]))
        self.cfg["verify"]["testbench_files"] = [
            ex(f) for f in self.cfg["verify"].get("testbench_files", [])]
        self.top = self.cfg["project"]["top_module"]
        # PDK files may be relative to pdk_root OR absolute (picked with Browse)
        pdk = self.cfg["pdk"]
        self.lib_path  = self._pdk(pdk.get("lib_file", ""))
        self.lef_paths = [self._pdk(x) for x in pdk.get("lef_files", [])]
        self.gds_paths = [self._pdk(x) for x in pdk.get("gds_files", [])]
        self.map_path  = self._pdk(pdk.get("layer_map_file", "")) if pdk.get("layer_map_file") else ""

    def _pdk(self, p):
        """Absolute path for a PDK file: absolute paths are kept, relative ones
           are joined to pdk_root."""
        p = xpath(p) if p else p
        if not p or os.path.isabs(p):
            return p
        return f"{self.cfg['pdk']['pdk_root']}/{p}"

    # -------- Yosys synthesis --------
    def synth_ys(self):
        pdk = self.cfg["pdk"]
        syn = self.cfg["synthesis"]
        top = self.top
        lib = self.lib_path
        rtl_files = self.cfg["project"]["rtl_files"]
        rtl_read = "\n".join([f'read_verilog "{f}"' for f in rtl_files]) \
            or "# TODO: add rtl paths in GUI 'Project' tab"

        dont_use = " ".join(f"-dont_use {c}" for c in pdk["dont_use_cells"])
        if pdk.get("ff_mapping", "techmap") == "dfflibmap":
            ff_block = f"""# ---- Map flops/latches straight from the liberty (pin names read from .lib) ----
dfflibmap -liberty {lib} {dont_use}
opt -fast"""
        else:
            ff_block = """# ---- Convert all FF/latch variants to types our techmap supports ----
#      Includes latches ($_DLATCH_*) since designs like PRESENT-80 use
#      combinational always@(*) blocks with incomplete assignments
#      (Yosys infers those as latches).
dfflegalize -cell $_DFF_P_ 01 -cell $_DFF_N_ 01 \\
            -cell $_DFF_PP0_ 01 -cell $_DFF_PN0_ 01 \\
            -cell $_DFF_PP1_ 01 -cell $_DFF_PN1_ 01 \\
            -cell $_DLATCH_P_ 01 -cell $_DLATCH_N_ 01

# ---- Map registers via manual techmap (pin names from pdk.cell_pins) ----
techmap -map dff_map.v
techmap -map latch_map.v
simplemap
opt -fast"""
        if pdk.get("tie_hi_cell") and pdk.get("tie_hi_pin") and \
           pdk.get("tie_lo_cell") and pdk.get("tie_lo_pin"):
            tie = (f"hilomap -hicell {pdk['tie_hi_cell']} {pdk['tie_hi_pin']} "
                   f"-locell {pdk['tie_lo_cell']} {pdk['tie_lo_pin']}")
        else:
            tie = "# (no tie cells/pins set on the PDK tab: constants stay as 1'b0/1'b1)"
        flatten = "flatten" if syn["flatten"] else "# flatten"
        share = "share -aggressive\n  wreduce" if syn["share_wreduce"] else "# share/wreduce"

        return f"""## =====================================================================
## Yosys synthesis  --  {top}
## Generated by IRIS (BCDC) on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================

# ---- Read PDK liberty ----
read_liberty -lib {lib}

# ---- Read RTL ----
{rtl_read}

# ---- Hierarchy ----
hierarchy -top {top}
{flatten}

# ---- Generic synthesis ----
proc; opt; fsm; opt; memory; opt
techmap; opt

# ---- Area/logic optimization ----
{share}
opt -fast

{ff_block}

# ---- ABC technology mapping ----
abc -liberty {lib} -D {syn['abc_D']} {dont_use}

# ---- Final cleanup + tie cells for constants ----
opt_clean
setundef -zero
{tie}
splitnets
opt_clean -purge

# ---- Reports ----
stat -liberty {lib}
check

# ---- Write netlist ----
write_verilog -noattr {top}_synth.v
write_json    {top}_synth.json
"""

    def dff_map_v(self):
        """Manual DFF techmap (ff_mapping = "techmap"), for libraries where
           Yosys dfflibmap can't be used. Cell names come from pdk.cells and pin
           names from pdk.cell_pins.  DFF_R = async active-low reset flop,
           DFF_S = async active-low set flop (same name if one cell has both)."""
        pdk = self.cfg["pdk"]
        pins = {**DEFAULT_CELL_PINS, **(pdk.get("cell_pins") or {})}
        CK, DP, QP = pins["clk"], pins["d"], pins["q"]
        RN, SN = pins["reset_n"], pins["set_n"]
        DFF_R = pdk.get("cells", {}).get("dff_reset_low")  or "DFF_RESET_CELL"
        DFF_S = pdk.get("cells", {}).get("dff_reset_high") or DFF_R
        return f"""// Auto-generated DFF techmap for PDK profile: {pdk.get('active_profile','?')}
// Maps Yosys generic DFF cells to library cells:
//   async-reset (active-low {RN}) -> {DFF_R}
//   async-set   (active-low {SN}) -> {DFF_S}

// ============ Async positive-edge DFF (no reset) ============
module \\$_DFF_P_ (input C, D, output Q);
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(D), .{QP}(Q));
endmodule
module \\$_DFF_N_ (input C, D, output Q);
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(~C), .{RN}(1'b1), .{DP}(D), .{QP}(Q));
endmodule

// ============ Async reset, positive edge, active-high reset ============
module \\$_DFF_PP0_ (input C, R, D, output Q);
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(~R), .{DP}(D), .{QP}(Q));
endmodule
module \\$_DFF_PN0_ (input C, R, D, output Q);
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(R),  .{DP}(D), .{QP}(Q));
endmodule
module \\$_DFF_PP1_ (input C, R, D, output Q);
    {DFF_S} _TECHMAP_REPLACE_ (.{CK}(C), .{SN}(~R), .{DP}(D), .{QP}(Q));
endmodule
module \\$_DFF_PN1_ (input C, R, D, output Q);
    {DFF_S} _TECHMAP_REPLACE_ (.{CK}(C), .{SN}(R),  .{DP}(D), .{QP}(Q));
endmodule

// ============ Sync reset, positive edge (SDFF) ============
// Build sync reset as an input mux:  D_next = R ? RST_VAL : D
module \\$_SDFF_PP0_ (input C, R, D, output Q);
    wire d_next = R ? 1'b0 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_PN0_ (input C, R, D, output Q);
    wire d_next = ~R ? 1'b0 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_PP1_ (input C, R, D, output Q);
    wire d_next = R ? 1'b1 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_PN1_ (input C, R, D, output Q);
    wire d_next = ~R ? 1'b1 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule

// ============ Sync reset, negative edge (SDFF_N*) ============
module \\$_SDFF_NP0_ (input C, R, D, output Q);
    wire d_next = R ? 1'b0 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(~C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_NN0_ (input C, R, D, output Q);
    wire d_next = ~R ? 1'b0 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(~C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_NP1_ (input C, R, D, output Q);
    wire d_next = R ? 1'b1 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(~C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_SDFF_NN1_ (input C, R, D, output Q);
    wire d_next = ~R ? 1'b1 : D;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(~C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule

// ============ DFF with clock enable (DFFE) ============
module \\$_DFFE_PP_ (input C, E, D, output Q);
    wire d_next = E ? D : Q;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
module \\$_DFFE_PN_ (input C, E, D, output Q);
    wire d_next = ~E ? D : Q;
    {DFF_R} _TECHMAP_REPLACE_ (.{CK}(C), .{RN}(1'b1), .{DP}(d_next), .{QP}(Q));
endmodule
"""

    def latch_map_v(self):
        """Map Yosys latches to the PDK latch cell (pdk.cells.latch); the reset
           pin, if the cell has one, is tied inactive for pure latch behaviour."""
        pdk = self.cfg["pdk"]
        pins = {**DEFAULT_CELL_PINS, **(pdk.get("cell_pins") or {})}
        EN, DP, QP, LRN = pins["latch_en"], pins["d"], pins["q"], pins["latch_reset_n"]
        L = pdk.get("cells", {}).get("latch") or "LATCH_CELL"
        return f"""// Auto-generated latch techmap for PDK profile: {pdk.get('active_profile','?')}
// Latch cell: {L}  (pins: {DP}, {EN}, {LRN}, {QP})
module \\$_DLATCH_P_ (input E, D, output Q);
    {L} _TECHMAP_REPLACE_ (.{EN}(E),  .{LRN}(1'b1), .{DP}(D), .{QP}(Q));
endmodule
module \\$_DLATCH_N_ (input E, D, output Q);
    {L} _TECHMAP_REPLACE_ (.{EN}(~E), .{LRN}(1'b1), .{DP}(D), .{QP}(Q));
endmodule
"""

    # -------- SDC --------
    def sdc(self):
        s = self.cfg["sdc"]
        pdk = self.cfg["pdk"]
        top = self.top
        clk = s["clock_port"]
        # "ALL" = every input except the clock / every output
        if [x.upper() for x in s["sync_input_ports"]] == ["ALL"]:
            in_sel = (f"[lsearch -inline -all -not -exact [all_inputs] "
                      f"[get_ports {clk}]]")
        else:
            in_sel = "[get_ports {" + " ".join(s["sync_input_ports"]) + "}]"
        if [x.upper() for x in s["output_ports"]] == ["ALL"]:
            out_sel = "[all_outputs]"
        else:
            out_sel = "[get_ports {" + " ".join(s["output_ports"]) + "}]"
        if s.get("reset_is_async", True) and s.get("reset_port"):
            rst = (f"# ---- Async reset ----\n"
                   f"set_false_path -from [get_ports {s['reset_port']}]")
        else:
            rst = (f"# ---- Synchronous reset '{s.get('reset_port','')}': timed as a normal input "
                   f"(include it in the input list) ----")

        return f"""## =====================================================================
## SDC constraints  --  {top}
## Target clock: {1000.0/s['clock_period_ns']:.1f} MHz ({s['clock_period_ns']} ns period)
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================

set PERIOD  {s['clock_period_ns']}
set UNCERT  {s['clock_uncertainty_ns']}
set IO_DLY  {s['io_delay_ns']}

# ---- Primary clock ----
create_clock -name clk -period $PERIOD [get_ports {s['clock_port']}]

# ---- Uncertainty ----
set_clock_uncertainty -setup $UNCERT [get_clocks clk]
set_clock_uncertainty -hold  {s['hold_uncertainty_ns']}   [get_clocks clk]

{rst}

# ---- Input delays ----
set IN_PORTS  {in_sel}
set OUT_PORTS {out_sel}
set_input_delay  -clock clk -max $IO_DLY $IN_PORTS
set_input_delay  -clock clk -min 0.0     $IN_PORTS

# ---- Output delays ----
set_output_delay -clock clk -max $IO_DLY $OUT_PORTS
set_output_delay -clock clk -min 0.0     $OUT_PORTS

# ---- Output load ----
#   Skip set_driving_cell -- pin names differ per PDK and default drive is fine.
#   If you need a specific driver, uncomment and pick a cell that exists in
#   your library. Check the output pin with:
#     awk '/cell\\({pdk['buffer_cell']}\\)/,/}}/' $LIB_FILE | grep 'pin('
# set_driving_cell -lib_cell {pdk['buffer_cell']} -pin Q [get_ports {{ ... }}]
set_load {s['output_load_pf']} $OUT_PORTS

# ---- Design-wide budgets ----
set_max_fanout      {s['max_fanout']} [current_design]
set_max_transition  {s['max_transition_ns']} [current_design]
"""

    # -------- Floorplan --------
    def floorplan_tcl(self):
        f   = self.cfg["floorplan"]
        pdk = self.cfg["pdk"]
        top = self.top
        rt  = self.cfg["runtime"]
        die = f["die_size_um"]
        m   = f["core_margin_um"]
        core_urx = die - m
        core_ury = die - m
        lef_lines = "\n".join(f"read_lef {lf}" for lf in self.lef_paths)

        # Build make_tracks lines for every metal layer defined in the PDK
        metals = pdk.get("metal_layers", [])
        track_lines = "\n".join(
            f"make_tracks {L['name']} "
            f"-x_offset {L.get('x_offset', 0)} -x_pitch {L['x_pitch']} "
            f"-y_offset {L.get('y_offset', 0)} -y_pitch {L['y_pitch']}"
            for L in metals
        )
        io_h = pdk.get("io_hor_layer") or self.cfg["routing"]["bottom_layer"]
        io_v = pdk.get("io_ver_layer") or self.cfg["routing"]["top_layer"]
        if pdk.get("tapcell"):
            tap = (f"tapcell -distance {pdk.get('tap_distance', 14)} "
                   f"-tapcell_master {pdk['tapcell']}")
        else:
            tap = "# (no tap cell set for this PDK)"
        pdn_tcl = self._pdn_tcl()

        # Padframe source only if any pad cell defined AND enabled
        pads    = pdk.get("pads", {})
        pf_cfg  = self.cfg.get("padframe", {})
        has_pads = pf_cfg.get("enable", True) and bool(pads.get("corner")) \
                   and any(pads.get(k) for k in ("input", "output", "vdd", "vss"))
        pad_src = "source ./04_padframe.tcl" if has_pads else \
                  "# (no padframe: PDK has no pad cells or padframe disabled)"

        bottom = self.cfg["routing"]["bottom_layer"]
        top_l  = self.cfg["routing"]["top_layer"]

        return f"""## =====================================================================
## Floorplan  --  {top}   Die: {die/1000:.3f} mm x {die/1000:.3f} mm
## Metal layers: {[L['name'] for L in metals]}
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================
set_thread_count {rt['threads']}

set PDK_ROOT   {pdk['pdk_root']}
set LIB_FILE   {self.lib_path}
set NETLIST    ../synth/{top}_synth.v
set SDC        ../{top}.sdc
set OUT        [pwd]

# ---- Load PDK ----
{lef_lines}
read_liberty $LIB_FILE

# ---- Read netlist + link ----
read_verilog $NETLIST
link_design  {top}

# ---- SDC intentionally NOT read here (floorplan doesn't need timing) ----
# read_sdc $SDC   ;# deferred to CTS/route/reports stages

# ---- Wire RC (pre-placement estimate) ----
set_wire_rc -clock  -layer {top_l}
set_wire_rc -signal -layer {bottom}

# ---- Initialize floorplan ----
initialize_floorplan -site {pdk['site_name']} \\
    -die_area  "0 0 {die:.1f} {die:.1f}" \\
    -core_area "{m:.1f} {m:.1f} {core_urx:.1f} {core_ury:.1f}"

# ---- Routing tracks (one per metal layer in the PDK) ----
{track_lines}

# ---- Padframe ----
{pad_src}

# ---- Place top-level IO pins on die edge so ports have physical location ----
#   Needed when pads are placed as separate instances (not RTL-instantiated).
#   Without this, GRT errors: "Pin X does not have geometries in a valid routing layer".
#   (-random/-random_seed are obsolete in OpenROAD 26Qx: passing them makes
#    place_pins skip placement entirely -> GPL-0326 "port is not placed")
if {{[catch {{place_pins -hor_layers {io_h} -ver_layers {io_v} -min_distance 1}} err]}} {{
  puts "  place_pins: $err"
}}
# ---- Verify every top-level port got a pin shape (fail here, not in placement) ----
set n_ports 0; set n_unplaced 0
foreach bt [[ord::get_db_block] getBTerms] {{
  incr n_ports
  set ok 0
  foreach bp [$bt getBPins] {{ if {{[llength [$bp getBoxes]] > 0}} {{ set ok 1 }} }}
  if {{!$ok}} {{ incr n_unplaced; if {{$n_unplaced <= 10}} {{ puts "  unplaced port: [$bt getName]" }} }}
}}
if {{$n_unplaced > 0}} {{
  puts "\\[IRIS\\] ERROR: $n_unplaced of $n_ports top-level ports have no pin location"
  exit 1
}}
puts "  IO pins: all $n_ports ports placed on {io_h}/{io_v}"

# ---- Well taps ----
{tap}

# ---- Power distribution network ----
{pdn_tcl}

puts "== Floorplan done =="
write_db  1_floorplan.odb
write_def 1_floorplan.def
"""

    def _lib_reload(self):
        """An .odb holds the physical design but NOT the liberty timing models:
           every stage after floorplan must re-read the .lib (else repair_design
           and CTS have no buffers/timing to work with) and the SDC."""
        pdk = self.cfg["pdk"]
        du = " ".join(pdk.get("dont_use_cells", []))
        lines = [f"read_liberty {self.lib_path}",
                 f'catch {{read_sdc "../{self.top}.sdc"}}']
        if du:
            lines.append(f"catch {{set_dont_use [get_lib_cells {{{du}}}]}}")
        return "\n".join(lines)

    def _pdn_tcl(self):
        """pdngen script from pdk.pdn (followpin rails + straps). Disabled ->
           comment only (no power grid: fine for experiments, NOT for tapeout)."""
        pdn = self.cfg["pdk"].get("pdn") or {}
        if not pdn.get("enable"):
            return ("# PDN disabled for this PDK profile (pdk.pdn.enable = false).\n"
                    "# WARNING: without a power grid the std-cell rails are not\n"
                    "# connected to anything -- OK for flow experiments only.")
        L = [f"add_global_connection -net VDD -inst_pattern {{.*}} "
             f"-pin_pattern {{{pdn.get('power_pins', '^(VDD|VPWR)$')}}} -power",
             f"add_global_connection -net VSS -inst_pattern {{.*}} "
             f"-pin_pattern {{{pdn.get('ground_pins', '^(VSS|VGND)$')}}} -ground",
             "global_connect",
             "set_voltage_domain -name CORE -power VDD -ground VSS",
             "define_pdn_grid -name grid -voltage_domains CORE",
             f"add_pdn_stripe -grid grid -layer {pdn['rail_layer']} "
             f"-width {pdn['rail_width']} -followpins"]
        for s in pdn.get("straps", []):
            L.append(f"add_pdn_stripe -grid grid -layer {s['layer']} -width {s['width']} "
                     f"-pitch {s['pitch']} -offset {s['offset']}")
        for a, b in pdn.get("connect", []):
            L.append(f"add_pdn_connect -grid grid -layers {{{a} {b}}}")
        L.append("pdngen")
        return "\n".join(L)

    # -------- Padframe (auto-pitch, evenly distributed, per-side counts) --------
    def padframe_tcl(self):
        p = self.cfg["padframe"]
        pdk_pads = self.cfg["pdk"].get("pads", {})

        # Skip padframe generation entirely if the PDK has no pad cells
        if not pdk_pads.get("corner") or not pdk_pads.get("vdd"):
            return (f"# Padframe skipped -- PDK profile '"
                    f"{self.cfg['pdk'].get('active_profile','?')}' has no pad cells "
                    f"(digital-only PDK). To use, define pad cells on the PDK tab.\n")

        # Override corner cell + size from PDK profile if set
        p["corner_cell"]    = pdk_pads.get("corner", p.get("corner_cell"))
        p["corner_size_um"] = pdk_pads.get("corner_size_um", p.get("corner_size_um"))
        pads = sorted(p["pad_list"], key=lambda x: x[0])

        ns = p.get("pads_south", 4)
        ne = p.get("pads_east",  4)
        nn = p.get("pads_north", 4)
        nw = p.get("pads_west",  4)
        total = ns + ne + nn + nw

        # Slice pad list by cumulative per-side counts (padded with placeholders if short)
        while len(pads) < total:
            i = len(pads) + 1
            pads.append([i, p.get("default_signal_pad", "") or "PAD_IN",
                         f"u_pad_{i}", f"pad_port_{i}", False])
        idx = 0
        south = pads[idx:idx+ns]; idx += ns
        east  = pads[idx:idx+ne]; idx += ne
        north = pads[idx:idx+nn]; idx += nn
        west  = pads[idx:idx+nw]; idx += nw

        def emit_side(name, side, orient, axis):
            """Emit place_pad commands for one side. Uses the proven pattern
               from a proven padframe flow: first pad centered in its
               slot (half-pitch offset), then step by pitch. Positional
               instance-name (not -inst_name flag)."""
            lines = [f"    puts \"== IO_{name.upper()} =={{}}\""]
            lines.append(f"    set loc [expr {{$corner_size + $pitch_{name} / 2.0}}]")
            for order, master, inst, port, is_pwr in side:
                if is_pwr:
                    lines.append(f'    # POWER pad')
                lines.append(
                    f'    set rc [catch {{place_pad -master {master} '
                    f'-row IO_{name.upper()} -location $loc {inst}}} err]'
                )
                lines.append(f'    if {{$rc}} {{')
                lines.append(f'        puts "  \\[FAIL\\] {inst} ({master}) @ $loc: $err"')
                lines.append(f'    }} else {{')
                lines.append(f'        puts "  \\[OK\\]   {inst} ({master}) @ $loc"')
                lines.append(f'    }}')
                lines.append(f'    set loc [expr {{$loc + $pitch_{name}}}]')
            return "\n".join(lines)

        # guard against /0 when a side has 0 pads
        def div(n): return f"{max(n, 1)}.0"

        return f"""## =====================================================================
## Padframe  --  variable per-side counts, auto-computed pitch
##   South: {ns}   East: {ne}   North: {nn}   West: {nw}   Total: {total}
## Corner cell: {p['corner_cell']}  ({p['corner_size_um']} um square)
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================

set corner_size {p['corner_size_um']}
set block  [ord::get_db_block]
set bbox   [$block getDieArea]
set die_w  [expr {{[$bbox dx] / 1000.0}}]
set die_h  [expr {{[$bbox dy] / 1000.0}}]

## Per-side pitch (pads centered inside their slot):
##   pitch = (die_edge - 2 * corner_size) / N_pads
set pitch_south [expr {{($die_w - 2.0 * $corner_size) / {div(ns)}}}]
set pitch_east  [expr {{($die_h - 2.0 * $corner_size) / {div(ne)}}}]
set pitch_north [expr {{($die_w - 2.0 * $corner_size) / {div(nn)}}}]
set pitch_west  [expr {{($die_h - 2.0 * $corner_size) / {div(nw)}}}]

puts "  die = ${{die_w}} x ${{die_h}} um"
puts "  pitch: S=$pitch_south  E=$pitch_east  N=$pitch_north  W=$pitch_west"

# ---- IO rows around the die (site names vary per PDK) ----
# Typical IO libraries use 'IO' and 'corner' sites.
# If your PDK uses different names, edit here or catch skips the error.
catch {{
  make_io_sites -horizontal_site IO -vertical_site IO \\
                -corner_site corner -offset 0
}} err
if {{[info exists err] && $err ne ""}} {{
  puts "  make_io_sites: $err (continuing with default IO rows)"
}}

# ---- Pads first (evenly spaced per side, auto-pitch) ----
{emit_side("south", south, "", "w")}

{emit_side("east", east, "", "h")}

{emit_side("north", north, "", "w")}

{emit_side("west", west, "", "h")}

# ---- Corners after pads (as in the proven working script) ----
catch {{place_corners {p['corner_cell']}}} err
puts "== Corners placed ($err) =="

# ---- Remove unused IO rows so placement isn't confused ----
catch {{remove_io_rows}}

puts "== Padframe done: {total} pads + 4 corners =="
"""

    # -------- Placement --------
    def place_tcl(self):
        p = self.cfg["placement"]
        rt = self.cfg["runtime"]

        return f"""## =====================================================================
## Placement  --  density {p['density']}, {p['pad_left']}/{p['pad_right']} pad
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================
set_thread_count {rt['threads']}

read_db 1_floorplan.odb
{self._lib_reload()}

# ---- Global placement ----
global_placement \\
    -density {p['density']} \\
    -init_density_penalty {p['init_density_penalty']} \\
    -init_wirelength_coef {p['init_wirelength_coef']} \\
    -min_phi_coef {p['min_phi_coef']} \\
    -max_phi_coef {p['max_phi_coef']} \\
    -overflow {p['overflow']} \\
    -pad_left {p['pad_left']} -pad_right {p['pad_right']} \\
    {"-routability_driven" if p['routability_driven'] else ""}

# ---- Post-GP repair (skip if library lacks buffer cells) ----
set_wire_rc -clock  -layer {self.cfg['routing']['top_layer']}
set_wire_rc -signal -layer {self.cfg['routing']['bottom_layer']}
estimate_parasitics -placement
catch {{repair_design -max_wire_length 5000}} err
if {{[info exists err] && $err ne ""}} {{
    puts "  repair_design skipped: $err"
}}

# ---- Detailed placement ----
detailed_placement -max_displacement {p['detailed_max_displacement']}
optimize_mirroring
catch {{ check_placement -verbose }}

puts "== Placement done =="
write_db  2_placed.odb
write_def 2_placed.def
"""

    # -------- CTS --------
    def cts_tcl(self):
        c = self.cfg["cts"]
        pdk = self.cfg["pdk"]
        rt = self.cfg["runtime"]

        return f"""## =====================================================================
## Clock Tree Synthesis  --  root buffer {pdk['clock_buffer']}
##   Wrapped in catch: PDKs with very minimal buffer libraries can cause
##   TritonCTS to segfault on fanoutLimit lookup. On failure we fall back to
##   ideal-clock (router will handle the clock net as a normal signal).
## =====================================================================
set_thread_count {rt['threads']}

read_db 2_placed.odb
{self._lib_reload()}

# ---- IMMEDIATELY write a fallback 3_cts.odb (= placed.odb) so if CTS
#      segfaults below, routing still has a valid DB to consume.
write_db  3_cts.odb
write_def 3_cts.def

set_wire_rc -clock  -layer {self.cfg['routing']['top_layer']}
set_wire_rc -signal -layer {self.cfg['routing']['bottom_layer']}

# (liberty + SDC were loaded above -- CTS needs both for buffer characterisation)

# ---- CTS (in catch -- may segfault on minimal libraries) ----
set cts_rc [catch {{
    clock_tree_synthesis \\
        -root_buf {pdk['clock_buffer']} \\
        -buf_list {pdk['clock_buffer']} \\
        -distance_between_buffers {c['distance_between_buffers']} \\
        -sink_clustering_enable \\
        -sink_clustering_size {c['sink_clustering_size']} \\
        -sink_clustering_max_diameter {c['sink_clustering_max_diameter']} \\
        -num_static_layers {c['num_static_layers']}
}} cts_err]

if {{$cts_rc != 0}} {{
    puts "\\n[[[[ CTS failed: $cts_err ]]]]"
    puts "  Falling back to ideal clock (router will route clock as signal)"
}} else {{
    puts "  CTS succeeded"
    # ---- Post-CTS optimization ----
    estimate_parasitics -placement
    catch {{set_propagated_clock [all_clocks]}}
    catch {{repair_clock_inverters}}
    catch {{repair_clock_nets -max_wire_length 5000}}
    # ---- Hold fixing with the real (propagated) clock tree ----
    estimate_parasitics -placement
    if {{[catch {{repair_timing -hold -hold_margin 0.05}} rt_err]}} {{
        puts "  repair_timing -hold skipped: $rt_err"
    }}
    catch {{detailed_placement}}
    catch {{check_placement -verbose}}
}}

puts "== CTS stage done =="
write_db  3_cts.odb
write_def 3_cts.def
"""

    # -------- Routing --------
    def route_tcl(self):
        r = self.cfg["routing"]
        rt = self.cfg["runtime"]
        pdk = self.cfg["pdk"]

        filler = " ".join(pdk["filler_cells"])
        clean = "-clean_patches" if r["clean_patches"] else ""
        save  = "-save_guide_updates" if r["save_guide_updates"] else ""

        return f"""## =====================================================================
## Detailed Routing  --  {r['iterations']} iterations, {r['bottom_layer']}..{r['top_layer']}
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================
set_thread_count {rt['threads']}

read_db 3_cts.odb
{self._lib_reload()}
set OUT [pwd]

# ---- Filler cells ----
filler_placement {{{filler}}}

# ---- Global route ----
set_routing_layers -signal {r['bottom_layer']}-{r['top_layer']} \\
                   -clock  {r['bottom_layer']}-{r['top_layer']}
global_route -congestion_iterations 100 -verbose
catch {{write_guides $OUT/route.guide}}

# ---- Pin access ----
#   (layer range comes from set_routing_layers above; the per-command
#    -bottom/-top_routing_layer options are deprecated errors in OpenROAD 26Qx)
catch {{pin_access}}

# ---- Detailed route ----
detailed_route \\
    -output_drc $OUT/route_drc.rpt \\
    -verbose {r['verbose']} \\
    -droute_end_iter {r['iterations']} \\
    {save} {clean}

# ---- Reports ----
catch {{report_wire_length -net * -detailed_route -file $OUT/wire_length.rpt}}

puts "== Routing done =="
write_db  4_routed.odb
write_def 4_routed.def
"""

    # -------- Reports (post-route) --------
    def reports_tcl(self):
        rp = self.cfg["reports"]
        sdc = self.cfg["sdc"]
        pdk = self.cfg["pdk"]
        top = self.top
        lef_lines = "\n    ".join(f"read_lef {lf}" for lf in self.lef_paths)

        return f"""## =====================================================================
## Reports  --  area / timing / power on the LATEST layout DB
##   Uses 4_routed.odb if present (else 3_cts / 2_placed), so numbers
##   reflect real placement + CTS. Falls back to the synthesized netlist
##   (pre-layout) only when no layout DB exists.
##   Parasitics: placement-based estimate. For true post-route RC you need
##   an OpenRCX rules file for your PDK (extract_parasitics -ext_model_file).
## Generated on {datetime.now():%Y-%m-%d %H:%M}
## =====================================================================

set PDK_ROOT {pdk['pdk_root']}
set LIB_FILE {self.lib_path}

set src ""
foreach f {{4_routed.odb 3_cts.odb 2_placed.odb}} {{
    if {{[file exists $f]}} {{ set src $f; break }}
}}

if {{$src ne ""}} {{
    read_db $src
    read_liberty $LIB_FILE
    puts "\\n==== Reports on layout DB: $src ===="
}} else {{
    {lef_lines}
    read_liberty $LIB_FILE
    read_verilog ../synth/{top}_synth.v
    link_design  {top}
    puts "\\n==== No layout DB found -- PRE-LAYOUT reports on synthesized netlist ===="
}}
read_sdc ../{top}.sdc

set_wire_rc -clock  -layer {self.cfg['routing']['top_layer']}
set_wire_rc -signal -layer {self.cfg['routing']['bottom_layer']}
if {{$src eq "4_routed.odb" || $src eq "3_cts.odb"}} {{
    catch {{set_propagated_clock [all_clocks]}}
}}
catch {{estimate_parasitics -placement}}

puts "\\n==== AREA ===="
report_design_area

puts "\\n==== CELL USAGE ===="
report_cell_usage

puts "\\n==== TIMING (worst setup + hold) ===="
report_checks -path_delay min_max -format full_clock_expanded \\
              -fields {{slew capacitance input_pins fanout}} -digits 3

puts "\\n==== SLACK ===="
puts "setup:"; report_worst_slack -max; report_tns
puts "hold:";  report_worst_slack -min

puts "\\n==== DESIGN RULES (slew/cap/fanout) ===="
catch {{report_check_types -max_slew -max_capacitance -max_fanout -violators}}

puts "\\n==== CLOCK PROPERTIES ===="
report_clock_properties
catch {{report_clock_skew}}

puts "\\n==== POWER ===="
set_power_activity -input -activity {rp['activity']} -duty {rp['duty']}
# (clock activity is derived from the clock definition automatically)
report_power -digits 4

exit
"""

    # -------- KLayout GDS merge --------
    def gds_merge_py(self):
        pdk = self.cfg["pdk"]
        pads_cfg = pdk.get("pads", {})
        pad_cells = sorted({v for k, v in pads_cfg.items()
                            if k != "corner_size_um" and isinstance(v, str) and v}
                           | {r[1] for r in self.cfg.get("padframe", {}).get("pad_list", [])
                              if len(r) > 1 and r[1]})
        top = self.top
        gds = list(self.gds_paths)
        lef = list(self.lef_paths)
        return f'''"""
KLayout DEF -> GDS extractor
Usage:  klayout -zz -rm 09_gds_merge.py
Generated on {datetime.now():%Y-%m-%d %H:%M}
"""
import pya, os, glob

TOP = "{top}"
GDS = [os.path.expandvars(p) for p in {gds!r}]
LEF = [os.path.expandvars(p) for p in {lef!r}]
DEF_FILE = "4_routed.def"
OUT_GDS  = "{top}_final.gds"

layout = pya.Layout()

# ---- Load every GDS in the PDK ----
print(f"[merge] Loading {{len(GDS)}} GDS files...")
for g in GDS:
    if os.path.exists(g):
        print(f"  reading {{g}}")
        layout.read(g)
    else:
        print(f"  MISSING: {{g}}")

# Also load any extra .gds found under the PDK root
pdk_root = os.path.dirname(os.path.dirname(GDS[0])) if GDS else ""
if pdk_root:
    extra = [p for p in glob.glob(f"{{pdk_root}}/**/*.gds", recursive=True) if p not in GDS]
    for g in extra:
        print(f"  reading (extra) {{g}}")
        layout.read(g)

# ---- Read routed DEF with LEF context ----
opts = pya.LoadLayoutOptions()
opts.lefdef_config.lef_files = LEF
opts.lefdef_config.produce_lef_macros    = True     # LEF outline fallback
opts.lefdef_config.macro_resolution_mode = 1        # prefer real GDS
MAP = os.path.expandvars("{self.map_path}")
if MAP and os.path.exists(MAP):
    opts.lefdef_config.map_file = MAP                # real GDS layer numbers
    print(f"[merge] layer map: {{MAP}}")
elif MAP:
    print(f"[merge] WARNING: layer map not found: {{MAP}} -- GDS layers will be generic")
print(f"[merge] Reading DEF: {{DEF_FILE}}")
layout.read(DEF_FILE, opts)

# ---- Report empty cells (for debugging missing GDS) ----
empty = [c.name for c in layout.each_cell()
         if c.bbox().empty() and c.child_instances() == 0]
if empty:
    print(f"[merge] WARNING: {{len(empty)}} cells have no geometry:")
    for n in empty: print(f"    {{n}}")

# ---- Count pads ----
top_cell = layout.cell(TOP)
if top_cell:
    pad_cells = {pad_cells!r}
    n = sum(1 for i in top_cell.each_inst() if i.cell.name in pad_cells)
    print(f"[merge] Pad+corner instances in {{TOP}}: {{n}}")

# ---- Write final GDS ----
layout.write(OUT_GDS)
sz = os.path.getsize(OUT_GDS)
print(f"[merge] WROTE {{OUT_GDS}}  ({{sz:,}} bytes)")
'''

    # -------- OpenROAD GUI loader --------
    def view_tcl(self):
        pdk = self.cfg["pdk"]
        return f"""## IRIS: open the most advanced layout DB in the OpenROAD GUI
## Usage (from work/pnr):  openroad -gui 10_view.tcl
set loaded ""
foreach f {{4_routed.odb 3_cts.odb 2_placed.odb 1_floorplan.odb}} {{
    if {{[file exists $f]}} {{ read_db $f; set loaded $f; break }}
}}
if {{$loaded eq ""}} {{
    puts "IRIS: no .odb found in [pwd] -- run the flow first"
}} else {{
    puts "IRIS: loaded $loaded"
    catch {{read_liberty {self.lib_path}}}
    catch {{read_sdc ../{self.top}.sdc}}
}}
"""

    # -------- Toolchain / environment --------
    def mode(self):
        return self.cfg["runtime"].get("toolchain_mode", "bcdc")

    def _env_prefix(self):
        """Shell snippet prepended to every stage command.
           bcdc mode  -> source ~/.bcdc/env.sh (pinned toolchain on PATH)
           legacy     -> nothing (per-tool activation below)"""
        if self.mode() != "bcdc":
            return ""
        env = self.cfg["runtime"].get("bcdc_env") or "$HOME/.bcdc/env.sh"
        return (f'if [ -f "{env}" ]; then . "{env}"; else '
                f'echo "[bcdc] {env} not found -- run install_bcdc.sh, or set '
                f'Runtime > toolchain_mode = legacy"; false; fi && ')

    def _tool_prefix(self, tool):
        """Bash command that invokes a tool (with env activation in legacy mode)."""
        rt = self.cfg["runtime"]
        binary = rt.get(f"{tool}_bin", tool)
        if self.mode() == "bcdc":
            return binary
        if tool == "yosys":
            act = rt.get("yosys_env_activate", "").strip()
            return (f"{act} && " if act else "") + binary
        if tool == "klayout":
            act = rt.get("klayout_env_activate", "").strip()
            return (f"{act} && " if act else "") + binary
        return binary

    # -------- Per-stage commands (GUI, headless and run_flow.sh share these) --------
    def stage_commands(self, script_dir, work_dir):
        """Return {stage_key: bash_command}. Paths are double-quoted so spaces
           (and $VARS when generating run_flow.sh) work."""
        rt  = self.cfg["runtime"]
        v   = self.cfg["verify"]
        so  = self.cfg["signoff"]
        top = self.top
        pre = self._env_prefix()

        def norm(p): return str(p).replace("\\", "/")
        SD = norm(script_dir)
        WD = norm(work_dir)
        syn_dir = f"{WD}/synth"
        pnr_dir = f"{WD}/pnr"
        gds_dir = f"{WD}/gds"

        yosys     = self._tool_prefix("yosys")
        klayout   = self._tool_prefix("klayout")
        verilator = self._tool_prefix("verilator")
        iverilog  = self._tool_prefix("iverilog")
        rtl = " ".join(f'"{norm(f)}"' for f in self.cfg["project"]["rtl_files"])
        tbs = " ".join(f'"{norm(f)}"' for f in v.get("testbench_files", []))

        use_nix = self.mode() == "legacy" and rt.get("openroad_use_nix", False)
        nix_dir = rt.get("openlane_nix_dir", "$HOME/openlane2")

        def openroad(tcl, logf, work_cwd):
            or_bin = rt["openroad_bin"]
            if use_nix:
                # nix-shell changes CWD -> cd back to work_cwd inside the shell
                inner = f'cd \\"{work_cwd}\\" && {or_bin} -no_init -exit \\"{SD}/{tcl}\\"'
                return (f'cd "{nix_dir}" && nix-shell --run '
                        f'"{inner}" 2>&1 | tee "{work_cwd}/{logf}"')
            return (pre + f'cd "{work_cwd}" && {or_bin} -no_init -exit '
                          f'"{SD}/{tcl}" 2>&1 | tee "{logf}"')

        # ---- lint ----
        if v.get("lint_enable", True) and rtl:
            lint = (pre + f'mkdir -p "{WD}/lint" && cd "{WD}/lint" && '
                    f'{verilator} --lint-only {v.get("verilator_flags", "")} '
                    f'--top-module {top} {rtl} 2>&1 | tee lint.log')
        else:
            lint = 'echo "[lint] skipped (disabled or no RTL files)"'

        # ---- RTL simulation ----
        if v.get("sim_enable", True) and tbs and rtl:
            tbtop = f"-s {v['tb_top']}" if v.get("tb_top") else ""
            sim = (pre + f'mkdir -p "{WD}/sim" && cd "{WD}/sim" && '
                   f'{iverilog} {v.get("iverilog_flags", "")} {tbtop} -o sim.vvp '
                   f'{rtl} {tbs} 2>&1 | tee sim_compile.log && '
                   f'vvp -n sim.vvp 2>&1 | tee sim.log')
            rx = (v.get("sim_fail_regex") or "").strip()
            if rx:
                sim += (f' && if grep -nE {shlex.quote(rx)} sim.log; then '
                        f'echo "[sim] failure pattern matched in sim.log"; false; fi')
        else:
            sim = 'echo "[sim] skipped (disabled, or no testbench files on the Verify tab)"'

        # ---- DRC ----
        deck = (so.get("drc_deck") or "").strip()
        if deck:
            drc = (pre + f'mkdir -p "{WD}/signoff" && cd "{WD}/signoff" && '
                   f'{klayout} -b -r "{deck}" -rd input="{gds_dir}/{top}_final.gds" '
                   f'-rd topcell={top} -rd report="{WD}/signoff/drc.lyrdb" '
                   f'{so.get("drc_extra_args", "")} 2>&1 | tee drc.log && '
                   f'n=$(grep -c "<item>" drc.lyrdb 2>/dev/null || true); '
                   f'echo "[drc] ${{n:-0}} marker(s) -> {WD}/signoff/drc.lyrdb '
                   f'(view: klayout {gds_dir}/{top}_final.gds -m drc.lyrdb)"')
        else:
            drc = 'echo "[drc] skipped (no KLayout DRC deck set on the Verify + Signoff tab)"'

        return {
            "lint": lint,
            "sim":  sim,
            "synth":
                pre + f'mkdir -p "{syn_dir}" && cd "{syn_dir}" && '
                f'cp "{SD}/dff_map.v" "{SD}/latch_map.v" . && '
                f'{yosys} -l synth.log "{SD}/01_synth.ys" && '
                f'cp "{SD}/02_design.sdc" "{WD}/{top}.sdc"',
            "floorplan":
                f'mkdir -p "{pnr_dir}" && '
                f'cp "{SD}/04_padframe.tcl" "{pnr_dir}/" && '
                + openroad('03_floorplan.tcl', 'floorplan.log', pnr_dir),
            "place":   openroad('05_place.tcl',   'place.log',   pnr_dir),
            "cts":     openroad('06_cts.tcl',     'cts.log',     pnr_dir),
            "route":   openroad('07_route.tcl',   'route.log',   pnr_dir),
            "reports": openroad('08_reports.tcl', 'reports.log', pnr_dir),
            "gds":
                pre + f'mkdir -p "{gds_dir}" && cd "{pnr_dir}" && '
                f'{klayout} -zz -rm "{SD}/09_gds_merge.py" 2>&1 | tee gds.log && '
                f'cp "{top}_final.gds" "{gds_dir}/"',
            "drc": drc,
        }

    def stage_outputs(self, work_dir):
        """Expected artefact per stage. If a stage exits non-zero but this file
           was (re)written during the stage, the flow continues (e.g. CTS segfault
           after the fallback write_db)."""
        WD = str(work_dir).replace("\\", "/")
        top = self.top
        return {
            "synth":     f"{WD}/synth/{top}_synth.v",
            "floorplan": f"{WD}/pnr/1_floorplan.odb",
            "place":     f"{WD}/pnr/2_placed.odb",
            "cts":       f"{WD}/pnr/3_cts.odb",
            "route":     f"{WD}/pnr/4_routed.odb",
            "reports":   f"{WD}/pnr/reports.log",
            "gds":       f"{WD}/gds/{top}_final.gds",
        }

    def view_commands(self, script_dir, work_dir):
        """Detached viewer commands (KLayout GDS, OpenROAD GUI, GTKWave)."""
        pre = self._env_prefix()
        SD = str(script_dir).replace("\\", "/")
        WD = str(work_dir).replace("\\", "/")
        top = self.top
        kl = self._tool_prefix("klayout")
        return {
            "gds": pre + f'{kl} "{WD}/gds/{top}_final.gds"',
            "openroad": pre + f'cd "{WD}/pnr" && {self.cfg["runtime"]["openroad_bin"]} '
                              f'-gui "{SD}/10_view.tcl"',
            "wave": pre + f'cd "{WD}/sim" && f=$(ls -t *.vcd *.fst 2>/dev/null | head -1) && '
                          f'[ -n "$f" ] && gtkwave "$f"',
        }

    # -------- Master run script --------
    def run_flow_sh(self):
        top = self.top
        work = self.cfg["project"]["work_dir"] or "$SCRIPT_DIR/work"
        cmds = self.stage_commands("$SCRIPT_DIR", "$WORK")
        outs = self.stage_outputs("$WORK")
        mode = self.mode()
        if mode == "bcdc":
            tool_note = f"BCDC toolchain ({self.cfg['runtime'].get('bcdc_env')})"
        else:
            rt = self.cfg["runtime"]
            tool_note = (f"legacy: yosys via '{rt.get('yosys_env_activate') or 'PATH'}', "
                         f"openroad via "
                         f"{'nix-shell in ' + rt.get('openlane_nix_dir','') if rt.get('openroad_use_nix') else 'PATH'}")

        funcs = "\n".join(f"stage_{k}() (\n    {cmds[k]}\n)\n" for k in STAGE_KEYS)
        out_map = "\n".join(f'    [{k}]="{v}"' for k, v in outs.items())
        return f"""#!/usr/bin/env bash
## =====================================================================
## RTL -> GDSII master flow  --  {top}
## Generated by IRIS (BCDC) on {datetime.now():%Y-%m-%d %H:%M}
## Toolchain: {tool_note}
##
## Usage:
##   ./run_flow.sh                     # all stages
##   ./run_flow.sh synth floorplan     # selected stages, in the order given
##   BCDC_WORK=/tmp/w ./run_flow.sh    # override work dir
## Stages: {" ".join(STAGE_KEYS)}
##
## Outputs:  $WORK/{{lint,sim,synth,pnr,gds,signoff}}/
## =====================================================================
set -uo pipefail

TOP={top}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="${{BCDC_WORK:-{work}}}"
ALL_STAGES=({" ".join(STAGE_KEYS)})

log()  {{ echo -e "\\n\\033[1;32m[flow]\\033[0m $1"; }}
warn() {{ echo -e "\\033[1;33m[flow]\\033[0m $1"; }}
err()  {{ echo -e "\\033[1;31m[flow]\\033[0m $1"; }}

# ---------------------------------------------------------------- stages ----
{funcs}
# Expected output per stage (continue if re-written despite non-zero exit)
declare -A OUT=(
{out_map}
)

run_stage() {{
    local s="$1" stamp
    declare -F "stage_$s" >/dev/null || {{ err "unknown stage '$s' (valid: ${{ALL_STAGES[*]}})"; return 1; }}
    stamp="$(mktemp)"
    log "stage: $s"
    if "stage_$s"; then rm -f "$stamp"; return 0; fi
    local o="${{OUT[$s]:-}}"
    if [ -n "$o" ] && [ "$o" -nt "$stamp" ]; then
        warn "stage '$s' exited non-zero but $o was written -- continuing"
        rm -f "$stamp"; return 0
    fi
    rm -f "$stamp"
    err "stage '$s' failed -- halting"
    return 1
}}

mkdir -p "$WORK"
stages=("$@"); [ ${{#stages[@]}} -eq 0 ] && stages=("${{ALL_STAGES[@]}}")
for s in "${{stages[@]}}"; do run_stage "$s" || exit 1; done
log "IRIS: flow complete. Final GDS: $WORK/gds/${{TOP}}_final.gds"
"""

    # -------- File map --------
    def all_files(self):
        return {
            "01_synth.ys":      self.synth_ys(),
            "dff_map.v":        self.dff_map_v(),
            "latch_map.v":      self.latch_map_v(),
            "02_design.sdc":    self.sdc(),
            "03_floorplan.tcl": self.floorplan_tcl(),
            "04_padframe.tcl":  self.padframe_tcl(),
            "05_place.tcl":     self.place_tcl(),
            "06_cts.tcl":       self.cts_tcl(),
            "07_route.tcl":     self.route_tcl(),
            "08_reports.tcl":   self.reports_tcl(),
            "09_gds_merge.py":  self.gds_merge_py(),
            "10_view.tcl":      self.view_tcl(),
            "run_flow.sh":      self.run_flow_sh(),
        }


# =============================================================================
#                              HEADLESS RUNNER
# =============================================================================
def _touch_stamp(work_dir):
    """Timestamp file on the SAME filesystem as the outputs (avoids clock skew,
       e.g. WSL /mnt/c). Returns its mtime."""
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    s = Path(work_dir) / ".bcdc_stage_stamp"
    s.write_text(datetime.now().isoformat())
    return s.stat().st_mtime


def _written_since(path, stamp_mtime):
    """True if `path` exists and was modified after the stage started."""
    return bool(path) and os.path.exists(path) and os.path.getmtime(path) > stamp_mtime


def run_headless(cfg, script_dir, work_dir, stages, regen=True, stream=sys.stdout):
    """Run stages sequentially without the GUI. Returns process exit code."""
    if regen:
        for p in write_scripts(cfg, script_dir):
            print(f"[gen] {p}", file=stream)
    gen  = ScriptGenerator(cfg)
    cmds = gen.stage_commands(script_dir, work_dir)
    outs = gen.stage_outputs(work_dir)
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    for key in stages:
        if key not in cmds:
            print(f"[flow] unknown stage '{key}'. Valid: {', '.join(STAGE_KEYS)}", file=stream)
            return 2
        print(f"\n\033[1;36m[flow] >>> {STAGE_LABEL[key]}\033[0m\n$ {cmds[key]}\n",
              file=stream, flush=True)
        stamp = _touch_stamp(work_dir)
        rc = subprocess.call(["/bin/bash", "-c", "set -o pipefail; " + cmds[key]])
        if rc == 0:
            continue
        out = outs.get(key)
        if _written_since(out, stamp):
            print(f"[flow] {key} exited {rc} but {out} was written -- continuing", file=stream)
            continue
        print(f"[flow] {key} FAILED (exit {rc}) -- halting", file=stream)
        return rc
    top = cfg["project"]["top_module"]
    print(f"\n[IRIS] flow complete. GDS: {work_dir}/gds/{top}_final.gds", file=stream)
    return 0


# =============================================================================
#                                   GUI
# =============================================================================
class FlowGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{PRODUCT} {MARK}  ·  {PRODUCT_L}   |   by {COMPANY}")
        self.root.geometry("1180x820")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.vars = {}                                # tk variables by dotted path
        self._build_ui()

    # ---- helper: bound tk variable per config leaf ----
    def _bind(self, parent, section, key, row, col=0, width=32, label=None):
        label = label or key.replace("_", " ")
        val = self.cfg[section][key]
        vtype = type(val)
        var = (tk.BooleanVar if vtype is bool else
               tk.DoubleVar  if vtype is float else
               tk.IntVar     if vtype is int else
               tk.StringVar)()
        var.set(val)
        self.vars[f"{section}.{key}"] = (var, vtype, section, key)
        ttk.Label(parent, text=label + ":").grid(row=row, column=col*2, sticky="e",
                                                  padx=4, pady=2)
        if vtype is bool:
            ttk.Checkbutton(parent, variable=var).grid(row=row, column=col*2+1,
                                                       sticky="w", padx=4, pady=2)
        else:
            ttk.Entry(parent, textvariable=var, width=width).grid(
                row=row, column=col*2+1, sticky="w", padx=4, pady=2)

    # ---- helper: file/dir picker bound to a StringVar ----
    def _picker(self, parent, section, key, row, is_dir=False, label=None):
        val = self.cfg[section][key]
        var = tk.StringVar(value=val)
        self.vars[f"{section}.{key}"] = (var, str, section, key)
        ttk.Label(parent, text=(label or key.replace("_", " ")) + ":").grid(
            row=row, column=0, sticky="e", padx=4, pady=2)
        e = ttk.Entry(parent, textvariable=var, width=60)
        e.grid(row=row, column=1, sticky="we", padx=4, pady=2)
        def pick():
            p = filedialog.askdirectory() if is_dir else filedialog.askopenfilename()
            if p: var.set(p)
        ttk.Button(parent, text="Browse", command=pick).grid(
            row=row, column=2, padx=4, pady=2)

    # ---- top-level UI ----
    def _build_ui(self):
        # ---- BCDC banner ----
        banner = tk.Frame(self.root, bg=NAVY)
        banner.pack(fill="x")
        self._banner_logo = _logo_image(LOGO_BANNER_B64)
        if self._banner_logo:
            chip = tk.Frame(banner, bg="#ffffff", padx=4, pady=2)
            chip.pack(side="left", padx=(10, 2), pady=5)
            tk.Label(chip, image=self._banner_logo, bg="#ffffff", bd=0).pack()
            try:
                self.root.iconphoto(True, self._banner_logo)
            except Exception:
                pass
        tk.Label(banner, text="I.R.I.S.", bg=NAVY, fg=GOLD,
                 font=("Consolas", 20, "bold")).pack(side="left", padx=(14, 8), pady=6)
        tk.Label(banner, text=PRODUCT_L.upper(),
                 bg=NAVY, fg="#ffffff",
                 font=("Helvetica", 12, "bold")).pack(side="left", pady=6)
        tk.Label(banner, text=f"  |  {MARK}  ·  by BCDC",
                 bg=NAVY, fg="#8ecfff",
                 font=("Helvetica", 11, "italic")).pack(side="left", pady=6)
        tk.Label(banner, text="Verilator · Icarus · Yosys · OpenROAD · KLayout",
                 bg="#0a1a2f", fg="#7a97b8",
                 font=("Helvetica", 10)).pack(side="right", padx=14, pady=6)

        # ---- top bar ----
        top = ttk.Frame(self.root); top.pack(fill="x", padx=6, pady=4)
        ttk.Button(top, text="Load Config",  command=self.load_config).pack(side="left", padx=2)
        ttk.Button(top, text="Save Config",  command=self.save_config).pack(side="left", padx=2)
        ttk.Button(top, text="Restore Defaults", command=self.restore_defaults).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Preview Selected", command=self.preview_selected).pack(side="left", padx=2)
        ttk.Button(top, text="Generate ALL",     command=self.generate_all).pack(side="left", padx=2)
        ttk.Button(top, text="Generate + Run Flow",
                   command=self.generate_and_run).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Tool Doctor", command=self._doctor).pack(side="left", padx=2)

        # ---- notebook ----
        nb = ttk.Notebook(self.root); nb.pack(fill="both", expand=True, padx=6, pady=4)
        self.nb = nb

        self._tab_project(nb)
        self._tab_pdk(nb)
        self._tab_synth_sdc(nb)
        self._tab_floorplan(nb)
        self._tab_padframe(nb)
        self._tab_placement(nb)
        self._tab_cts_route(nb)
        self._tab_reports_runtime(nb)
        self._tab_verify(nb)
        self._tab_output(nb)
        self._tab_run(nb)

        # subprocess handle for live execution
        self.current_proc = None

        # ---- log ----
        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="x", padx=6, pady=4)
        self.log = scrolledtext.ScrolledText(logf, height=8, font=("Consolas", 11))
        self.log.pack(fill="x")

        # ---- Warn if running from Windows ----
        if sys.platform.startswith("win"):
            self._log("Windows detected. Shell commands will be routed through WSL "
                      "(wsl.exe). For best results, run this tool from inside WSL:")
            self._log("    bcdc        (after install_bcdc.sh)")
            self._log("Windows paths (C:\\Users\\...) auto-translate to /mnt/c/... "
                      "-- but avoid spaces in folder names.")
        self._log(f"IRIS {MARK} online.  Toolchain mode: {self.cfg['runtime']['toolchain_mode']}  "
                  f"(Runtime tab).  Click 'Tool Doctor' to check every tool.")

    # ============ TABS ============
    def _tab_project(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="1. Project")

        top = ttk.LabelFrame(f, text="Top Module & Directories")
        top.pack(fill="x", padx=6, pady=4)
        self._bind(top, "project", "top_module", 0, width=50)
        self._picker(top, "project", "rtl_dir",   1, is_dir=True)
        self._picker(top, "project", "work_dir",  2, is_dir=True)
        self._picker(top, "project", "output_dir",3, is_dir=True, label="script dir")

        # RTL file list
        rtlf = ttk.LabelFrame(f, text="RTL Verilog Files (order matters)")
        rtlf.pack(fill="both", expand=True, padx=6, pady=4)
        self.rtl_list = tk.Listbox(rtlf, height=14)
        self.rtl_list.pack(side="left", fill="both", expand=True)
        for x in self.cfg["project"]["rtl_files"]: self.rtl_list.insert("end", x)
        btns = ttk.Frame(rtlf); btns.pack(side="right", fill="y", padx=4)
        ttk.Button(btns, text="Add Files", command=lambda: self._add_files(self.rtl_list)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Add Directory", command=self._add_rtl_dir).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove Selected", command=lambda: self._remove_sel(self.rtl_list)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Up",   command=lambda: self._move_in(self.rtl_list, -1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Down", command=lambda: self._move_in(self.rtl_list, +1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear All", command=lambda: self.rtl_list.delete(0, "end")).pack(fill="x", pady=2)

    def _add_files(self, listbox):
        files = filedialog.askopenfilenames(
            filetypes=[("Verilog", "*.v *.sv *.vh"), ("All", "*.*")])
        for f in files: listbox.insert("end", f)

    def _add_rtl_dir(self):
        d = filedialog.askdirectory()
        if not d: return
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                if fn.endswith((".v", ".sv", ".vh")):
                    self.rtl_list.insert("end", os.path.join(root, fn))

    def _move_in(self, listbox, delta):
        sel = listbox.curselection()
        if not sel: return
        i = sel[0]; j = i + delta
        if 0 <= j < listbox.size():
            v = listbox.get(i)
            listbox.delete(i)
            listbox.insert(j, v)
            listbox.selection_set(j)

    def _tab_pdk(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="2. PDK")

        # ---- Profile picker (auto-populates every field below) ----
        prof = ttk.LabelFrame(f, text="PDK Profile (pick preset -> auto-fills)")
        prof.pack(fill="x", padx=6, pady=4)
        ttk.Label(prof, text="Profile:").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        self.profile_var = tk.StringVar(value=self.cfg["pdk"].get(
            "active_profile", list(PDK_PROFILES.keys())[0]))
        self.vars["pdk.active_profile"] = (self.profile_var, str, "pdk", "active_profile")
        cb = ttk.Combobox(prof, textvariable=self.profile_var, state="readonly",
                          values=list(PDK_PROFILES.keys()), width=40)
        cb.grid(row=0, column=1, padx=6, pady=6, sticky="w")
        ttk.Button(prof, text="Load Profile Defaults",
                   command=self._apply_pdk_profile).grid(row=0, column=2, padx=6)
        ttk.Label(prof, text="(Then adjust individual fields below if needed)",
                  foreground="grey").grid(row=0, column=3, padx=6, sticky="w")

        top = ttk.LabelFrame(f, text="PDK Root & Files   ($HOME and ~ are expanded)")
        top.pack(fill="x", padx=6, pady=4)
        self._picker(top, "pdk", "pdk_root", 0, is_dir=True)
        self._bind(top, "pdk", "lib_file",   1, width=60)
        ttk.Button(top, text="Browse", command=self._browse_lib).grid(row=1, column=2, padx=4, pady=2)
        self._bind(top, "pdk", "site_name",  2, width=20)
        self._bind(top, "pdk", "clock_buffer",3, width=20)
        self._bind(top, "pdk", "buffer_cell", 4, width=20)

        # LEF list
        lefs = ttk.LabelFrame(f, text="LEF files -- tech LEF first (relative to PDK root, or absolute)")
        lefs.pack(fill="both", expand=True, padx=6, pady=4)
        self.lef_list = tk.Listbox(lefs, height=6)
        self.lef_list.pack(side="left", fill="both", expand=True)
        for x in self.cfg["pdk"]["lef_files"]: self.lef_list.insert("end", x)
        lb = ttk.Frame(lefs); lb.pack(side="right", fill="y", padx=4)
        ttk.Button(lb, text="Browse...", command=lambda: self._browse_pdk_files(
            self.lef_list, "Select LEF / tech LEF files",
            [("LEF", "*.lef *.tlef *.LEF"), ("All", "*.*")])).pack(fill="x", pady=2)
        ttk.Button(lb, text="Type path", command=lambda: self._add_str(self.lef_list, "LEF Path")).pack(fill="x", pady=2)
        ttk.Button(lb, text="Remove", command=lambda: self._remove_sel(self.lef_list)).pack(fill="x", pady=2)
        ttk.Button(lb, text="Move Up",   command=lambda: self._move_in(self.lef_list, -1)).pack(fill="x", pady=2)
        ttk.Button(lb, text="Move Down", command=lambda: self._move_in(self.lef_list, +1)).pack(fill="x", pady=2)

        # GDS list
        gds = ttk.LabelFrame(f, text="GDS files (relative to PDK root, or absolute)")
        gds.pack(fill="both", expand=True, padx=6, pady=4)
        self.gds_list = tk.Listbox(gds, height=4)
        self.gds_list.pack(side="left", fill="both", expand=True)
        for x in self.cfg["pdk"]["gds_files"]: self.gds_list.insert("end", x)
        gb = ttk.Frame(gds); gb.pack(side="right", fill="y", padx=4)
        ttk.Button(gb, text="Browse...", command=lambda: self._browse_pdk_files(
            self.gds_list, "Select GDS / OASIS files",
            [("GDS", "*.gds *.gds2 *.gdsii *.GDS *.oas"), ("All", "*.*")])).pack(fill="x", pady=2)
        ttk.Button(gb, text="Type path", command=lambda: self._add_str(self.gds_list, "GDS Path")).pack(fill="x", pady=2)
        ttk.Button(gb, text="Remove", command=lambda: self._remove_sel(self.gds_list)).pack(fill="x", pady=2)

        # dont_use list
        du = ttk.LabelFrame(f, text="Don't-use cells (space-separated)")
        du.pack(fill="x", padx=6, pady=4)
        self.dont_use_var = tk.StringVar(value=" ".join(self.cfg["pdk"]["dont_use_cells"]))
        ttk.Entry(du, textvariable=self.dont_use_var, width=90).pack(padx=4, pady=4)

        # filler list
        fl = ttk.LabelFrame(f, text="Filler cells (space-separated)")
        fl.pack(fill="x", padx=6, pady=4)
        self.filler_var = tk.StringVar(value=" ".join(self.cfg["pdk"]["filler_cells"]))
        ttk.Entry(fl, textvariable=self.filler_var, width=90).pack(padx=4, pady=4)

        # ---- Cell name mapping (for dff_map.v generation) ----
        cellf = ttk.LabelFrame(f, text="Cell Name Mapping (used to generate techmap)")
        cellf.pack(fill="x", padx=6, pady=4)
        self.cell_vars = {}
        cell_specs = [
            ("dff_reset_low",  "DFF with async reset (active-low)"),
            ("dff_reset_high", "DFF with async preset (active-low)"),
            ("latch",          "D-latch"),
            ("tie_hi_cell",    "TIE-HIGH cell"),
            ("tie_lo_cell",    "TIE-LOW cell"),
        ]
        for i, (key, lab) in enumerate(cell_specs):
            r, c = divmod(i, 2)
            if key in ("tie_hi_cell", "tie_lo_cell"):
                v = tk.StringVar(value=self.cfg["pdk"].get(key, ""))
                self.vars[f"pdk.{key}"] = (v, str, "pdk", key)
            else:
                v = tk.StringVar(value=self.cfg["pdk"].get("cells", {}).get(key, ""))
                self.cell_vars[key] = v
            ttk.Label(cellf, text=lab + ":").grid(row=r, column=c*2, padx=4, pady=3, sticky="e")
            ttk.Entry(cellf, textvariable=v, width=28).grid(row=r, column=c*2+1,
                                                             padx=4, pady=3, sticky="w")

        # ---- Pad cell mapping ----
        padf = ttk.LabelFrame(f, text="Pad Cell Mapping (leave empty for digital-only PDKs)")
        padf.pack(fill="x", padx=6, pady=4)
        self.pad_cell_vars = {}
        pad_specs = [
            ("input",  "Input pad"),
            ("output", "Output pad"),
            ("vdd",    "VDD pad"),
            ("vss",    "VSS pad"),
            ("corner", "Corner cell"),
        ]
        for i, (key, lab) in enumerate(pad_specs):
            r, c = divmod(i, 2)
            v = tk.StringVar(value=self.cfg["pdk"].get("pads", {}).get(key, ""))
            self.pad_cell_vars[key] = v
            ttk.Label(padf, text=lab + ":").grid(row=r, column=c*2, padx=4, pady=3, sticky="e")
            ttk.Entry(padf, textvariable=v, width=28).grid(row=r, column=c*2+1,
                                                             padx=4, pady=3, sticky="w")
        cs_val = self.cfg["pdk"].get("pads", {}).get("corner_size_um", 0.0)
        self.corner_size_var = tk.DoubleVar(value=cs_val)
        self.pad_cell_vars["corner_size_um"] = self.corner_size_var
        ttk.Label(padf, text="Corner size (um):").grid(row=2, column=2, padx=4, pady=3, sticky="e")
        ttk.Entry(padf, textvariable=self.corner_size_var, width=12).grid(
            row=2, column=3, padx=4, pady=3, sticky="w")

        # ---- Metal layer stack table ----
        mlf = ttk.LabelFrame(f, text="Metal Layer Stack (name, x_pitch um, y_pitch um) -- drives make_tracks")
        mlf.pack(fill="both", expand=True, padx=6, pady=4)
        cols = ("name", "x_pitch", "y_pitch", "x_offset", "y_offset")
        self.metal_tree = ttk.Treeview(mlf, columns=cols, show="headings", height=6)
        for c in cols:
            self.metal_tree.heading(c, text=c.upper())
            self.metal_tree.column(c, width=110)
        self.metal_tree.pack(side="left", fill="both", expand=True)
        for L in self.cfg["pdk"].get("metal_layers", []):
            self.metal_tree.insert("", "end",
                values=self._metal_row(L))

        mb = ttk.Frame(mlf); mb.pack(side="right", fill="y", padx=4)
        ttk.Button(mb, text="Add Layer",   command=self._metal_add).pack(fill="x", pady=2)
        ttk.Button(mb, text="Edit Layer",  command=self._metal_edit).pack(fill="x", pady=2)
        ttk.Button(mb, text="Delete",      command=self._metal_del).pack(fill="x", pady=2)
        ttk.Button(mb, text="Move Up",     command=lambda: self._metal_move(-1)).pack(fill="x", pady=2)
        ttk.Button(mb, text="Move Down",   command=lambda: self._metal_move(+1)).pack(fill="x", pady=2)

    # -------- PDK file browsing --------
    def _pdk_root_abs(self):
        try:
            return xpath(self.vars["pdk.pdk_root"][0].get()).rstrip("/")
        except Exception:
            return ""

    def _rel_to_pdk(self, path):
        """Store files under pdk_root as relative paths (portable configs);
           anything else stays absolute."""
        root = self._pdk_root_abs()
        path = os.path.abspath(path)
        if root and os.path.isdir(root) and \
           os.path.commonpath([path, os.path.abspath(root)]) == os.path.abspath(root):
            return os.path.relpath(path, root)
        return path

    def _browse_dir_start(self, subdir_hint=""):
        root = self._pdk_root_abs()
        for d in (os.path.join(root, subdir_hint) if subdir_hint else "", root):
            if d and os.path.isdir(d):
                return d
        return os.path.expanduser("~")

    def _browse_pdk_files(self, listbox, title, types):
        files = filedialog.askopenfilenames(title=title, filetypes=types,
                                            initialdir=self._browse_dir_start("libs.ref"))
        existing = set(listbox.get(0, "end"))
        added = 0
        for fp in files:
            v = self._rel_to_pdk(fp)
            if v not in existing:
                listbox.insert("end", v); existing.add(v); added += 1
        if added:
            self._log(f"Added {added} file(s)")

    def _browse_lib(self):
        fp = filedialog.askopenfilename(
            title="Select Liberty timing library",
            filetypes=[("Liberty", "*.lib *.lib.gz *.liberty"), ("All", "*.*")],
            initialdir=self._browse_dir_start("libs.ref"))
        if fp:
            self.vars["pdk.lib_file"][0].set(self._rel_to_pdk(fp))
            self._log(f"Liberty: {fp}")

    # -------- PDK profile helpers --------
    def _apply_pdk_profile(self):
        name = self.profile_var.get()
        if name not in PDK_PROFILES:
            messagebox.showerror("Unknown profile", name); return
        if not messagebox.askyesno("Apply Profile",
            f"Load defaults from profile:\n\n  {name}\n\n"
            "This overwrites the current PDK settings (root, LEFs, cells, "
            "pads, metal layers, routing layers). RTL / floorplan / padframe "
            "fields are NOT touched. Continue?"): return

        p = PDK_PROFILES[name]
        self.cfg["pdk"]["active_profile"] = name
        for key in ("pdk_root", "lib_file", "site_name", "clock_buffer",
                    "buffer_cell", "tie_hi_cell", "tie_lo_cell"):
            if key in p:
                self.cfg["pdk"][key] = p[key]
                if f"pdk.{key}" in self.vars:
                    self.vars[f"pdk.{key}"][0].set(p[key])
        for key in ("lef_files", "gds_files", "dont_use_cells", "filler_cells"):
            if key in p:
                self.cfg["pdk"][key] = list(p[key])
        self.cfg["pdk"]["cells"] = dict(p.get("cells", {}))
        self.cfg["pdk"]["pads"]  = dict(p.get("pads",  {}))
        self.cfg["pdk"]["metal_layers"] = [dict(L) for L in p.get("metal_layers", [])]
        for key in ("ff_mapping", "tie_hi_pin", "tie_lo_pin", "io_hor_layer", "io_ver_layer", "tapcell", "tap_distance", "layer_map_file"):
            if key in p:
                self.cfg["pdk"][key] = p[key]
        self.cfg["pdk"]["pdn"] = copy.deepcopy(p.get("pdn", {"enable": False}))
        self.cfg["pdk"]["cell_pins"] = {**DEFAULT_CELL_PINS, **(p.get("cell_pins") or {})}
        if "routing_bottom" in p:
            self.cfg["routing"]["bottom_layer"] = p["routing_bottom"]
            if "routing.bottom_layer" in self.vars:
                self.vars["routing.bottom_layer"][0].set(p["routing_bottom"])
        if "routing_top" in p:
            self.cfg["routing"]["top_layer"] = p["routing_top"]
            if "routing.top_layer" in self.vars:
                self.vars["routing.top_layer"][0].set(p["routing_top"])
        self.lef_list.delete(0, "end")
        for x in self.cfg["pdk"]["lef_files"]:  self.lef_list.insert("end", x)
        self.gds_list.delete(0, "end")
        for x in self.cfg["pdk"]["gds_files"]:  self.gds_list.insert("end", x)
        self.dont_use_var.set(" ".join(self.cfg["pdk"]["dont_use_cells"]))
        self.filler_var.set(" ".join(self.cfg["pdk"]["filler_cells"]))
        for k, v in self.cell_vars.items():
            v.set(self.cfg["pdk"]["cells"].get(k, ""))
        for k, v in self.pad_cell_vars.items():
            if k == "corner_size_um":
                v.set(self.cfg["pdk"]["pads"].get(k, 0.0))
            else:
                v.set(self.cfg["pdk"]["pads"].get(k, ""))
        for iid in self.metal_tree.get_children(): self.metal_tree.delete(iid)
        for L in self.cfg["pdk"]["metal_layers"]:
            self.metal_tree.insert("", "end",
                values=self._metal_row(L))
        self._log(f"Applied PDK profile: {name}")

    @staticmethod
    def _metal_row(L):
        return (L["name"], L["x_pitch"], L["y_pitch"],
                L.get("x_offset", 0), L.get("y_offset", 0))

    def _metal_edit_dialog(self, values=None):
        d = tk.Toplevel(self.root); d.title("Metal Layer"); d.geometry("360x250")
        defaults = list(values or ["metal1", 1.0, 1.0, 0, 0]) + [0, 0]
        vs = [tk.StringVar(value=str(v)) for v in defaults[:5]]
        for i, lab in enumerate(("Name", "X pitch (um)", "Y pitch (um)",
                                 "X offset (um)", "Y offset (um)")):
            ttk.Label(d, text=lab + ":").grid(row=i, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(d, textvariable=vs[i], width=22).grid(row=i, column=1, padx=6, pady=4)
        res = {}
        def ok():
            try:
                res["v"] = (vs[0].get(), float(vs[1].get()), float(vs[2].get()),
                            float(vs[3].get()), float(vs[4].get()))
                d.destroy()
            except Exception as e:
                messagebox.showerror("Bad input", str(e))
        ttk.Button(d, text="OK", command=ok).grid(row=5, column=0, columnspan=2, pady=8)
        d.wait_window()
        return res.get("v")

    def _metal_add(self):
        v = self._metal_edit_dialog()
        if v: self.metal_tree.insert("", "end", values=v)
    def _metal_edit(self):
        sel = self.metal_tree.selection()
        if not sel: return
        cur = self.metal_tree.item(sel[0], "values")
        v = self._metal_edit_dialog(list(cur))
        if v: self.metal_tree.item(sel[0], values=v)
    def _metal_del(self):
        for s in self.metal_tree.selection(): self.metal_tree.delete(s)
    def _metal_move(self, delta):
        sel = self.metal_tree.selection()
        if not sel: return
        idx = self.metal_tree.index(sel[0])
        self.metal_tree.move(sel[0], "", idx + delta)

    def _add_str(self, listbox, prompt):
        d = tk.Toplevel(self.root); d.title(prompt); d.geometry("500x100")
        v = tk.StringVar()
        ttk.Label(d, text=prompt + ":").pack(padx=8, pady=6)
        ttk.Entry(d, textvariable=v, width=70).pack(padx=8)
        def ok(): listbox.insert("end", v.get()); d.destroy()
        ttk.Button(d, text="OK", command=ok).pack(pady=6)

    def _remove_sel(self, listbox):
        for i in reversed(listbox.curselection()): listbox.delete(i)

    def _tab_synth_sdc(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="3. Synth + SDC")

        syn = ttk.LabelFrame(f, text="Synthesis (Yosys)")
        syn.pack(fill="x", padx=6, pady=4)
        self._bind(syn, "synthesis", "abc_D", 0)
        self._bind(syn, "synthesis", "target_delay_ps", 1)
        self._bind(syn, "synthesis", "flatten", 0, col=1)
        self._bind(syn, "synthesis", "share_wreduce", 1, col=1)

        sdc = ttk.LabelFrame(f, text="SDC / Timing")
        sdc.pack(fill="x", padx=6, pady=4)
        self._bind(sdc, "sdc", "clock_port", 0, width=20)
        self._bind(sdc, "sdc", "clock_period_ns", 1)
        self._bind(sdc, "sdc", "clock_uncertainty_ns", 2)
        self._bind(sdc, "sdc", "hold_uncertainty_ns", 3)
        self._bind(sdc, "sdc", "reset_port", 0, col=1, width=20)
        self._bind(sdc, "sdc", "io_delay_ns", 1, col=1)
        self._bind(sdc, "sdc", "output_load_pf", 2, col=1)
        self._bind(sdc, "sdc", "max_fanout", 3, col=1)
        self._bind(sdc, "sdc", "max_transition_ns", 4, col=0)
        self._bind(sdc, "sdc", "reset_is_async", 4, col=1)

        p = ttk.LabelFrame(f, text="Synchronous input ports (space-separated, or ALL = every input except the clock)")
        p.pack(fill="x", padx=6, pady=4)
        self.sync_in_var = tk.StringVar(value=" ".join(self.cfg["sdc"]["sync_input_ports"]))
        ttk.Entry(p, textvariable=self.sync_in_var, width=90).pack(padx=4, pady=4)

        p2 = ttk.LabelFrame(f, text="Output ports (space-separated, or ALL)")
        p2.pack(fill="x", padx=6, pady=4)
        self.out_var = tk.StringVar(value=" ".join(self.cfg["sdc"]["output_ports"]))
        ttk.Entry(p2, textvariable=self.out_var, width=90).pack(padx=4, pady=4)

    def _tab_floorplan(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="4. Floorplan")
        fp = ttk.LabelFrame(f, text="Die & Core")
        fp.pack(fill="x", padx=6, pady=4)
        self._bind(fp, "floorplan", "die_size_um", 0)
        self._bind(fp, "floorplan", "core_margin_um", 1)
        ttk.Label(f, text=(
            "Note: die_size is one edge in micrometers (7000 = 7mm).\n"
            "Core = die minus margins on all sides.\n"
            "Routing tracks are generated from the Metal Layer Stack on the PDK tab.")
        ).pack(padx=6, pady=6)

    def _tab_padframe(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="5. Padframe")

        top = ttk.LabelFrame(f, text="Padframe Config")
        top.pack(fill="x", padx=6, pady=4)
        self._bind(top, "padframe", "enable",              0)
        self._bind(top, "padframe", "corner_cell",         1, width=30)
        self._bind(top, "padframe", "corner_size_um",      2)
        self._bind(top, "padframe", "default_pad_width_um",3)
        self._bind(top, "padframe", "default_signal_pad",  0, col=1, width=15)
        self._bind(top, "padframe", "default_output_pad",  1, col=1, width=15)
        self._bind(top, "padframe", "default_vdd_pad",     2, col=1, width=15)
        self._bind(top, "padframe", "default_vss_pad",     3, col=1, width=15)
        self._bind(top, "padframe", "auto_power_pairs",    4, col=1)

        cnt = ttk.LabelFrame(f, text="Per-Side Pad Count (auto-spacing)")
        cnt.pack(fill="x", padx=6, pady=4)

        self.pad_side_vars = {}
        for i, side in enumerate(("south", "east", "north", "west")):
            key = f"pads_{side}"
            var = tk.IntVar(value=self.cfg["padframe"][key])
            self.vars[f"padframe.{key}"] = (var, int, "padframe", key)
            self.pad_side_vars[side] = var
            ttk.Label(cnt, text=f"{side.upper()}:").grid(row=0, column=i*2, padx=(8,2), pady=6, sticky="e")
            sp = ttk.Spinbox(cnt, from_=0, to=64, textvariable=var, width=6,
                              command=self._update_pitch_preview)
            sp.grid(row=0, column=i*2+1, padx=(0,8), pady=6, sticky="w")
            var.trace_add("write", lambda *a: self._update_pitch_preview())

        ttk.Label(cnt, text="TOTAL:").grid(row=0, column=8, padx=(12,2), sticky="e")
        self.pad_total_lbl = ttk.Label(cnt, text="16", font=("TkDefaultFont", 12, "bold"))
        self.pad_total_lbl.grid(row=0, column=9, padx=(0,8), sticky="w")

        ttk.Label(cnt, text="Set total:").grid(row=1, column=0, padx=(8,2), pady=4, sticky="e")
        self.bulk_total_var = tk.IntVar(value=16)
        ttk.Spinbox(cnt, from_=4, to=256, textvariable=self.bulk_total_var,
                    width=6).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Button(cnt, text="Distribute Evenly",
                   command=self._distribute_total).grid(row=1, column=2, columnspan=2, padx=6)
        ttk.Button(cnt, text="Regenerate Default Pad List",
                   command=self._regen_pad_list).grid(row=1, column=4, columnspan=4, padx=6)

        prev = ttk.LabelFrame(f, text="Computed Spacing Preview (updates when die size or counts change)")
        prev.pack(fill="x", padx=6, pady=4)
        self.pitch_preview_lbl = ttk.Label(prev, text="(preview)", font=("Consolas", 12),
                                            justify="left", anchor="w")
        self.pitch_preview_lbl.pack(fill="x", padx=8, pady=6)

        tbl = ttk.LabelFrame(f, text="Pad List (order 1..N is placed counter-clockwise: S -> E -> N -> W)")
        tbl.pack(fill="both", expand=True, padx=6, pady=4)
        cols = ("order", "master", "instance", "port", "power")
        self.pad_tree = ttk.Treeview(tbl, columns=cols, show="headings", height=12)
        for c in cols:
            self.pad_tree.heading(c, text=c.upper())
            self.pad_tree.column(c, width=140)
        self.pad_tree.pack(side="left", fill="both", expand=True)
        for row in self.cfg["padframe"]["pad_list"]:
            self.pad_tree.insert("", "end", values=row)

        pb = ttk.Frame(tbl); pb.pack(side="right", fill="y", padx=4)
        ttk.Button(pb, text="Add Pad",   command=self._pad_add).pack(fill="x", pady=2)
        ttk.Button(pb, text="Edit Pad",  command=self._pad_edit).pack(fill="x", pady=2)
        ttk.Button(pb, text="Delete",    command=self._pad_del).pack(fill="x", pady=2)
        ttk.Button(pb, text="Move Up",   command=lambda: self._pad_move(-1)).pack(fill="x", pady=2)
        ttk.Button(pb, text="Move Down", command=lambda: self._pad_move(+1)).pack(fill="x", pady=2)
        ttk.Button(pb, text="Renumber",  command=self._pad_renumber).pack(fill="x", pady=6)

        for path in ("floorplan.die_size_um", "padframe.corner_size_um",
                     "padframe.default_pad_width_um"):
            if path in self.vars:
                self.vars[path][0].trace_add("write", lambda *a: self._update_pitch_preview())

        self._update_pitch_preview()

    # -------- Padframe helpers --------
    def _update_pitch_preview(self):
        try:
            die = float(self.vars["floorplan.die_size_um"][0].get())
            corner = float(self.vars["padframe.corner_size_um"][0].get())
            pad_w = float(self.vars["padframe.default_pad_width_um"][0].get())
            counts = {s: self.pad_side_vars[s].get() for s in ("south", "east", "north", "west")}
        except Exception:
            return
        total = sum(counts.values())
        self.pad_total_lbl.config(text=str(total))

        lines = []
        avail = die - 2.0 * corner
        for side, n in counts.items():
            if n <= 0:
                lines.append(f"  {side.upper():5s}: 0 pads  (empty side)")
                continue
            pitch = avail / n
            slack = pitch - pad_w
            warn = "  <-- TIGHT!" if slack < 0 else ""
            lines.append(
                f"  {side.upper():5s}: {n} pads   "
                f"pitch = {pitch:8.2f} um   "
                f"pad_width = {pad_w:.1f}   "
                f"free space between pads = {slack:+8.2f} um{warn}"
            )
        lines.insert(0, f"  Die edge = {die:.1f} um   corners = {corner:.1f} um   available edge = {avail:.1f} um")
        lines.append(f"  TOTAL PADS = {total}   ( + 4 corner cells )")
        self.pitch_preview_lbl.config(text="\n".join(lines))

    def _distribute_total(self):
        n = self.bulk_total_var.get()
        base, rem = divmod(n, 4)
        for i, side in enumerate(("south", "east", "north", "west")):
            self.pad_side_vars[side].set(base + (1 if i < rem else 0))
        self._update_pitch_preview()

    def _regen_pad_list(self):
        if not messagebox.askyesno("Regenerate Pad List",
            "This will REPLACE the current pad list with defaults from your per-side counts.\n\n"
            "Continue?"): return

        sig  = self.vars["padframe.default_signal_pad"][0].get()
        out  = self.vars["padframe.default_output_pad"][0].get()
        vdd  = self.vars["padframe.default_vdd_pad"][0].get()
        vss  = self.vars["padframe.default_vss_pad"][0].get()
        auto = self.vars["padframe.auto_power_pairs"][0].get()

        counts = {s: self.pad_side_vars[s].get() for s in ("south", "east", "north", "west")}

        pads = []
        order = 1
        for side in ("south", "east", "north", "west"):
            n = counts[side]
            side_pads = []
            reserve_pwr = 2 if (auto and n >= 3) else 0
            n_sig = n - reserve_pwr
            for i in range(n_sig):
                master = out if (side == "north" and i == 0) else sig
                inst   = f"u_pad_{side[0]}{i+1}"
                port   = f"pad_{side[0]}{i+1}"
                side_pads.append([0, master, inst, port, False])
            if reserve_pwr:
                side_pads.append([0, vdd, f"u_vdd_{side[0]}", "VDD", True])
                side_pads.append([0, vss, f"u_vss_{side[0]}", "VSS", True])
            for row in side_pads:
                row[0] = order
                pads.append(row)
                order += 1

        for iid in self.pad_tree.get_children():
            self.pad_tree.delete(iid)
        for row in pads:
            self.pad_tree.insert("", "end", values=row)
        self._log(f"Regenerated pad list: {len(pads)} pads (S={counts['south']}, "
                  f"E={counts['east']}, N={counts['north']}, W={counts['west']})")

    def _pad_renumber(self):
        for i, iid in enumerate(self.pad_tree.get_children(), start=1):
            v = list(self.pad_tree.item(iid, "values"))
            v[0] = i
            self.pad_tree.item(iid, values=v)
        self._log("Pads renumbered 1..N")

    def _pad_edit_dialog(self, values=None):
        d = tk.Toplevel(self.root); d.title("Edit Pad"); d.geometry("380x260")
        vs = [tk.StringVar(value=str(v)) for v in (values or ["", "", "u_pad", "port", False])]
        labs = ["Order", "Master", "Instance", "Port", "Power (True/False)"]
        for i, (lab, v) in enumerate(zip(labs, vs)):
            ttk.Label(d, text=lab).grid(row=i, column=0, padx=6, pady=4, sticky="e")
            ttk.Entry(d, textvariable=v, width=28).grid(row=i, column=1, padx=6, pady=4)
        result = {}
        def ok():
            try:
                result["v"] = [int(vs[0].get()), vs[1].get(), vs[2].get(),
                               vs[3].get(), vs[4].get().lower() in ("true", "1", "yes")]
                d.destroy()
            except Exception as e:
                messagebox.showerror("Bad input", str(e))
        ttk.Button(d, text="OK", command=ok).grid(row=len(labs), column=0, columnspan=2, pady=8)
        d.wait_window()
        return result.get("v")

    def _pad_add(self):
        v = self._pad_edit_dialog()
        if v: self.pad_tree.insert("", "end", values=v)

    def _pad_edit(self):
        sel = self.pad_tree.selection()
        if not sel: return
        cur = self.pad_tree.item(sel[0], "values")
        v = self._pad_edit_dialog(list(cur))
        if v: self.pad_tree.item(sel[0], values=v)

    def _pad_del(self):
        for s in self.pad_tree.selection(): self.pad_tree.delete(s)

    def _pad_move(self, delta):
        sel = self.pad_tree.selection()
        if not sel: return
        idx = self.pad_tree.index(sel[0])
        self.pad_tree.move(sel[0], "", idx + delta)

    def _tab_placement(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="6. Placement")
        p = ttk.LabelFrame(f, text="Global Placement")
        p.pack(fill="x", padx=6, pady=4)
        self._bind(p, "placement", "density", 0)
        self._bind(p, "placement", "init_density_penalty", 1)
        self._bind(p, "placement", "init_wirelength_coef", 2)
        self._bind(p, "placement", "overflow", 3)
        self._bind(p, "placement", "min_phi_coef", 0, col=1)
        self._bind(p, "placement", "max_phi_coef", 1, col=1)
        self._bind(p, "placement", "pad_left", 2, col=1)
        self._bind(p, "placement", "pad_right", 3, col=1)
        self._bind(p, "placement", "routability_driven", 4, col=0)
        d = ttk.LabelFrame(f, text="Detailed Placement")
        d.pack(fill="x", padx=6, pady=4)
        self._bind(d, "placement", "detailed_max_displacement", 0)

    def _tab_cts_route(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="7. CTS + Route")
        c = ttk.LabelFrame(f, text="Clock Tree Synthesis")
        c.pack(fill="x", padx=6, pady=4)
        self._bind(c, "cts", "wire_unit", 0)
        self._bind(c, "cts", "distance_between_buffers", 1)
        self._bind(c, "cts", "sink_clustering_size", 2)
        self._bind(c, "cts", "sink_clustering_max_diameter", 3)
        self._bind(c, "cts", "num_static_layers", 4)
        r = ttk.LabelFrame(f, text="Detailed Routing")
        r.pack(fill="x", padx=6, pady=4)
        self._bind(r, "routing", "bottom_layer", 0, width=20)
        self._bind(r, "routing", "top_layer", 1, width=20)
        self._bind(r, "routing", "iterations", 2)
        self._bind(r, "routing", "verbose", 3)
        self._bind(r, "routing", "clean_patches", 0, col=1)
        self._bind(r, "routing", "save_guide_updates", 1, col=1)

    def _tab_reports_runtime(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="8. Reports + Runtime")
        r = ttk.LabelFrame(f, text="Power Report Assumptions")
        r.pack(fill="x", padx=6, pady=4)
        self._bind(r, "reports", "activity", 0)
        self._bind(r, "reports", "duty", 1)

        # ---- Toolchain selection ----
        tc = ttk.LabelFrame(f, text="Toolchain")
        tc.pack(fill="x", padx=6, pady=4)
        ttk.Label(tc, text="toolchain mode:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        mode_var = tk.StringVar(value=self.cfg["runtime"]["toolchain_mode"])
        self.vars["runtime.toolchain_mode"] = (mode_var, str, "runtime", "toolchain_mode")
        ttk.Combobox(tc, textvariable=mode_var, state="readonly", width=12,
                     values=["bcdc", "legacy"]).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(tc, text="Run Tool Doctor", command=self._doctor).grid(row=0, column=2, padx=8)
        self._bind(tc, "runtime", "bcdc_env", 1, width=45, label="BCDC env script")
        ttk.Label(tc, foreground="grey", justify="left", text=(
            "bcdc   = pinned toolchain from install_bcdc.sh (every stage sources the env script)\n"
            "legacy = per-tool activation below (OSS CAD Suite + OpenLane2 nix-shell)")
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=4)

        rt = ttk.LabelFrame(f, text="Tool Binaries")
        rt.pack(fill="x", padx=6, pady=4)
        self._bind(rt, "runtime", "openroad_bin", 0, width=30)
        self._bind(rt, "runtime", "yosys_bin", 1, width=30)
        self._bind(rt, "runtime", "klayout_bin", 2, width=30)
        self._bind(rt, "runtime", "verilator_bin", 0, col=1, width=20)
        self._bind(rt, "runtime", "iverilog_bin", 1, col=1, width=20)
        self._bind(rt, "runtime", "threads", 3)

        env = ttk.LabelFrame(f, text="Legacy-mode Environment Activation "
                                     "(ignored in bcdc mode)")
        env.pack(fill="x", padx=6, pady=4)
        self._bind(env, "runtime", "yosys_env_activate",   0, width=55,
                   label="Yosys activate (source ...)")
        self._bind(env, "runtime", "openroad_use_nix",     1,
                   label="OpenROAD in nix-shell?")
        self._bind(env, "runtime", "openlane_nix_dir",     2, width=55,
                   label="OpenLane nix-shell dir")
        self._bind(env, "runtime", "klayout_env_activate", 3, width=55,
                   label="KLayout activate")

    def _tab_verify(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="9. Verify + Signoff")

        lint = ttk.LabelFrame(f, text="Lint (Verilator --lint-only on the RTL list)")
        lint.pack(fill="x", padx=6, pady=4)
        self._bind(lint, "verify", "lint_enable", 0)
        self._bind(lint, "verify", "verilator_flags", 1, width=50)

        sim = ttk.LabelFrame(f, text="RTL Simulation (Icarus Verilog: RTL + testbenches -> vvp)")
        sim.pack(fill="both", expand=True, padx=6, pady=4)
        opts = ttk.Frame(sim); opts.pack(fill="x")
        self._bind(opts, "verify", "sim_enable", 0)
        self._bind(opts, "verify", "tb_top", 1, width=30, label="testbench top (-s)")
        self._bind(opts, "verify", "iverilog_flags", 2, width=40)
        self._bind(opts, "verify", "sim_fail_regex", 3, width=40,
                   label="fail if sim.log matches")
        tbf = ttk.Frame(sim); tbf.pack(fill="both", expand=True, pady=4)
        self.tb_list = tk.Listbox(tbf, height=6)
        self.tb_list.pack(side="left", fill="both", expand=True)
        for x in self.cfg["verify"]["testbench_files"]: self.tb_list.insert("end", x)
        bb = ttk.Frame(tbf); bb.pack(side="right", fill="y", padx=4)
        ttk.Button(bb, text="Add Testbench", command=lambda: self._add_files(self.tb_list)).pack(fill="x", pady=2)
        ttk.Button(bb, text="Remove", command=lambda: self._remove_sel(self.tb_list)).pack(fill="x", pady=2)
        ttk.Label(sim, foreground="grey", text=(
            "Tip: $dumpfile(\"tb.vcd\"); $dumpvars; in the testbench -> 'Waves (GTKWave)' on the Run tab.")
        ).pack(anchor="w", padx=6)

        so = ttk.LabelFrame(f, text="Physical Signoff (KLayout DRC)")
        so.pack(fill="x", padx=6, pady=4)
        self._picker(so, "signoff", "drc_deck", 0, label="DRC deck (.lydrc/.drc)")
        ttk.Label(so, text="extra args:").grid(row=1, column=0, sticky="e", padx=4)
        v = tk.StringVar(value=self.cfg["signoff"]["drc_extra_args"])
        self.vars["signoff.drc_extra_args"] = (v, str, "signoff", "drc_extra_args")
        ttk.Entry(so, textvariable=v, width=60).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Label(so, foreground="grey", justify="left", text=(
            "Run as: klayout -b -r DECK -rd input=<final.gds> -rd topcell=TOP -rd report=drc.lyrdb\n"
            "Many commercial-foundry decks are Calibre-only; those tape-outs still need Calibre sign-off.")
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=4)

    def _tab_output(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="10. Output / Preview")
        self.tab_output = f

        top = ttk.Frame(f); top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Preview file:").pack(side="left", padx=4)
        names = list(ScriptGenerator(self.cfg).all_files().keys())
        self.preview_choice = ttk.Combobox(top, state="readonly", width=25, values=names)
        self.preview_choice.current(0)
        self.preview_choice.pack(side="left", padx=4)
        self.preview_choice.bind("<<ComboboxSelected>>", lambda e: self.preview_selected())
        ttk.Button(top, text="Refresh", command=self.preview_selected).pack(side="left", padx=4)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(top, text="Save Edits  (Ctrl+S)", command=self._preview_save).pack(side="left", padx=4)
        ttk.Button(top, text="Revert to Generated", command=self._preview_revert).pack(side="left", padx=4)
        ttk.Button(top, text="Export As...", command=self._preview_export).pack(side="left", padx=4)
        self.preview_state = ttk.Label(top, text="", font=("TkDefaultFont", 10, "bold"))
        self.preview_state.pack(side="left", padx=10)

        self.preview = scrolledtext.ScrolledText(f, font=("Consolas", 12), wrap="none",
                                                 undo=True, autoseparators=True, maxundo=-1)
        self.preview.pack(fill="both", expand=True, padx=6, pady=4)
        self.preview.bind("<Control-s>", lambda e: (self._preview_save(), "break")[1])
        self.preview.bind("<Control-S>", lambda e: (self._preview_save(), "break")[1])
        self.preview.bind("<<Modified>>", self._preview_modified)
        ttk.Label(f, foreground="grey", text=(
            "Edit freely. 'Save Edits' writes the file to the script dir AND stores it in the "
            "config, so Generate ALL keeps your version. 'Revert to Generated' drops the edit.")
        ).pack(anchor="w", padx=8, pady=(0, 4))

    # ============ RUN FLOW TAB ============
    def _tab_run(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="11. Run Flow")
        self.tab_run = f

        top = ttk.LabelFrame(f, text="Full Flow")
        top.pack(fill="x", padx=6, pady=4)
        ttk.Button(top, text="Generate + Run ALL",
                   command=self.generate_and_run, width=22).pack(side="left", padx=4, pady=4)
        ttk.Button(top, text="Run ALL (skip regen)",
                   command=lambda: self._run_all_stages(regen=False), width=22).pack(side="left", padx=4)
        ttk.Button(top, text="Stop",
                   command=self._stop_running, width=10).pack(side="left", padx=4)
        ttk.Button(top, text="Clear Log",
                   command=lambda: self.run_log.delete("1.0", "end"), width=10).pack(side="left", padx=4)
        ttk.Button(top, text="Copy All",
                   command=self._copy_log_all, width=10).pack(side="left", padx=4)
        ttk.Button(top, text="Copy Selection",
                   command=self._copy_log_sel, width=14).pack(side="left", padx=4)
        ttk.Button(top, text="Save Log ...",
                   command=self._save_log, width=12).pack(side="left", padx=4)
        self.run_status = ttk.Label(top, text="Idle", foreground="grey",
                                     font=("TkDefaultFont", 12, "bold"))
        self.run_status.pack(side="right", padx=8)

        st = ttk.LabelFrame(f, text="Run Individual Stages")
        st.pack(fill="x", padx=6, pady=4)
        for i, (key, label) in enumerate(STAGES):
            r, c = divmod(i, 5)
            ttk.Button(st, text=label, command=lambda k=key: self._run_single_stage(k)
                       ).grid(row=r, column=c, sticky="we", padx=4, pady=4)
            st.grid_columnconfigure(c, weight=1)

        vw = ttk.LabelFrame(f, text="Viewers (open in a separate window)")
        vw.pack(fill="x", padx=6, pady=4)
        ttk.Button(vw, text="Final GDS (KLayout)",
                   command=lambda: self._view("gds")).pack(side="left", padx=4, pady=4)
        ttk.Button(vw, text="Layout DB (OpenROAD GUI)",
                   command=lambda: self._view("openroad")).pack(side="left", padx=4)
        ttk.Button(vw, text="Waves (GTKWave)",
                   command=lambda: self._view("wave")).pack(side="left", padx=4)

        logf = ttk.LabelFrame(f, text="Live Tool Output  (right-click for menu · "
                                       "Ctrl+A select all · Ctrl+C copy)")
        logf.pack(fill="both", expand=True, padx=6, pady=4)
        self.run_log = scrolledtext.ScrolledText(logf, font=("Consolas", 11),
                                                  bg="#111", fg="#ddd", insertbackground="#fff",
                                                  selectbackground="#4477bb",
                                                  selectforeground="#fff",
                                                  exportselection=True)
        self.run_log.pack(fill="both", expand=True)
        self.run_log.tag_config("green",  foreground="#4c8")
        self.run_log.tag_config("red",    foreground="#e55")
        self.run_log.tag_config("yellow", foreground="#dc4")
        self.run_log.tag_config("cyan",   foreground="#4cc")

        self.run_log_menu = tk.Menu(self.run_log, tearoff=0)
        self.run_log_menu.add_command(label="Copy Selection",
                                       accelerator="Ctrl+C", command=self._copy_log_sel)
        self.run_log_menu.add_command(label="Copy All",
                                       accelerator="Ctrl+Shift+C", command=self._copy_log_all)
        self.run_log_menu.add_separator()
        self.run_log_menu.add_command(label="Select All",
                                       accelerator="Ctrl+A", command=self._select_all_log)
        self.run_log_menu.add_command(label="Clear Log",
                                       command=lambda: self.run_log.delete("1.0", "end"))
        self.run_log_menu.add_separator()
        self.run_log_menu.add_command(label="Save Log to File ...",
                                       command=self._save_log)

        def _popup(event):
            try: self.run_log_menu.tk_popup(event.x_root, event.y_root)
            finally: self.run_log_menu.grab_release()

        self.run_log.bind("<Button-3>", _popup)
        self.run_log.bind("<Control-a>", lambda e: (self._select_all_log(), "break"))
        self.run_log.bind("<Control-A>", lambda e: (self._select_all_log(), "break"))
        self.run_log.bind("<Control-c>", lambda e: (self._copy_log_sel(), "break"))
        self.run_log.bind("<Control-Shift-C>", lambda e: (self._copy_log_all(), "break"))

    def _append_run_log(self, text, tag=None):
        self.run_log.insert("end", text, tag or ())
        try:
            line_count = int(self.run_log.index("end-1c").split(".")[0])
            if line_count > 12000:
                self.run_log.delete("1.0", f"{line_count - 10000}.0")
        except Exception:
            pass
        try:
            sel = self.run_log.tag_ranges("sel")
        except Exception:
            sel = ()
        if not sel:
            self.run_log.see("end")

    def _select_all_log(self):
        self.run_log.tag_add("sel", "1.0", "end-1c")
        self.run_log.focus_set()
        return "break"

    def _safe_clipboard_set(self, txt):
        """Cross-platform clipboard set that won't crash tkinter on WSL/Linux."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            self.root.update_idletasks()
            return True
        except Exception as e:
            self._append_run_log(f"\n[clipboard tk failed: {e}]\n", "red")
        for tool in [["xclip", "-selection", "clipboard"],
                     ["xsel", "--clipboard", "--input"],
                     ["clip.exe"]]:
            try:
                p = subprocess.Popen(tool, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.communicate(input=txt.encode("utf-8", errors="replace"), timeout=5)
                if p.returncode == 0:
                    self._append_run_log(f"\n[clipboard via {tool[0]}]\n", "yellow")
                    return True
            except Exception:
                continue
        self._append_run_log("\n[clipboard: no working method. Install xclip: sudo apt install xclip]\n", "red")
        return False

    def _copy_log_sel(self):
        try:
            txt = self.run_log.get("sel.first", "sel.last")
        except tk.TclError:
            txt = ""
        if not txt:
            txt = self.run_log.get("1.0", "end-1c")
        if len(txt) > 1_000_000:
            self._append_run_log(f"\n[selection too large ({len(txt)} chars) -- truncated to 1MB]\n", "yellow")
            txt = txt[:1_000_000]
        if self._safe_clipboard_set(txt):
            self._append_run_log(f"\n[copied {len(txt)} chars]\n", "yellow")

    def _copy_log_all(self):
        txt = self.run_log.get("1.0", "end-1c")
        if len(txt) > 1_000_000:
            self._append_run_log(f"\n[log too large ({len(txt)} chars) -- truncated to last 1MB]\n", "yellow")
            txt = txt[-1_000_000:]
        if self._safe_clipboard_set(txt):
            self._append_run_log(f"\n[copied ALL {len(txt)} chars]\n", "yellow")

    def _save_log(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All", "*.*")],
            initialfile=f"bcdc_run_{datetime.now():%Y%m%d_%H%M%S}.log")
        if not p: return
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.run_log.get("1.0", "end-1c"))
        self._append_run_log(f"\n[saved log -> {p}]\n", "green")

    def _set_status(self, text, color="grey"):
        self.run_status.config(text=text, foreground=color)

    def _shell_argv(self, cmd):
        """bash argv for cmd; routes through WSL on Windows. pipefail so that
           `tool | tee log` reports the TOOL's exit code, not tee's."""
        cmd = "set -o pipefail; " + cmd
        if sys.platform.startswith("win"):
            return ["wsl.exe", "bash", "-lc", self._to_wsl_command(cmd)]
        return ["/bin/bash", "-c", cmd]

    def _run_shell(self, cmd, on_done=None):
        """Run a bash command in the background, streaming output to run_log."""
        if self.current_proc:
            messagebox.showwarning("Busy", "A flow is already running. Stop it first.")
            return
        self._append_run_log(f"\n{'='*70}\n$ {cmd}\n{'='*70}\n", "cyan")
        self._set_status("Running...", "orange")

        is_windows = sys.platform.startswith("win")
        argv = self._shell_argv(cmd)
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
        )
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid
        elif is_windows:
            popen_kwargs["creationflags"] = getattr(subprocess,
                                                     "CREATE_NEW_PROCESS_GROUP", 0)

        def worker():
            try:
                proc = subprocess.Popen(argv, **popen_kwargs)
                self.current_proc = proc
                for line in proc.stdout:
                    self.root.after(0, self._append_run_log, line)
                proc.wait()
                rc = proc.returncode
                self.current_proc = None
                tag  = "green" if rc == 0 else "red"
                stat = "OK" if rc == 0 else f"FAILED (exit {rc})"
                self.root.after(0, self._append_run_log, f"\n[exit {rc}]\n", tag)
                self.root.after(0, self._set_status, stat,
                                "#4c8" if rc == 0 else "#e55")
                if on_done:
                    self.root.after(0, on_done, rc)
            except FileNotFoundError as e:
                self.current_proc = None
                msg = f"\n[ERROR] {e}\n"
                if is_windows and "wsl" in str(e).lower():
                    msg += ("\nWSL is not installed or not on PATH.\n"
                            "Run this tool from WSL Ubuntu instead:  bcdc\n")
                self.root.after(0, self._append_run_log, msg, "red")
                self.root.after(0, self._set_status, "Error", "#e55")
            except Exception as e:
                self.current_proc = None
                self.root.after(0, self._append_run_log,
                                f"\n[ERROR] {e}\n", "red")
                self.root.after(0, self._set_status, "Error", "#e55")

        threading.Thread(target=worker, daemon=True).start()

    def _launch_detached(self, cmd):
        """Start a GUI viewer without blocking the flow runner."""
        try:
            kw = {"start_new_session": True} if hasattr(os, "setsid") else {}
            subprocess.Popen(self._shell_argv(cmd), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw)
            self._append_run_log(f"\n[viewer] $ {cmd}\n", "cyan")
        except Exception as e:
            self._append_run_log(f"\n[viewer] failed: {e}\n", "red")

    def _to_wsl_command(self, cmd):
        """Convert 'C:/Users/Foo Bar/x' -> '/mnt/c/Users/Foo Bar/x' inside cmd."""
        import re
        def repl(m):
            drive = m.group(1).lower()
            rest  = m.group(2).replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
        return re.sub(r"\b([A-Za-z]):[/\\]([^\s\"']*)", repl, cmd)

    def _stop_running(self):
        p = self.current_proc
        if not p:
            self._append_run_log("(nothing to stop)\n", "yellow"); return
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            else:
                p.terminate()
                try:
                    subprocess.run(["wsl.exe", "pkill", "-TERM", "-P", str(p.pid)],
                                   timeout=5, capture_output=True)
                except Exception:
                    pass
            self._append_run_log("\n[stop] terminate signal sent\n", "yellow")
        except Exception as e:
            self._append_run_log(f"[stop] {e}\n", "red")

    # ---- toolchain doctor + viewers ----
    def _doctor(self):
        self._harvest()
        gen = ScriptGenerator(self.cfg)
        if gen.mode() == "bcdc":
            cmd = gen._env_prefix() + '"$BCDC_HOME/bin/bcdc" doctor'
        else:
            rt = self.cfg["runtime"]
            cmd = ("for t in yosys openroad klayout verilator iverilog magic netgen; do "
                   "printf '%-10s ' $t; command -v $t || echo '(not on PATH)'; done; "
                   f"echo; echo 'legacy mode: yosys via: {rt.get('yosys_env_activate')}'; "
                   f"echo 'openroad via nix-shell in: {rt.get('openlane_nix_dir')}'")
        self.nb.select(self.tab_run)
        self._run_shell(cmd)

    def _view(self, what):
        script_dir, work_dir = self._ensure_output_and_scripts()
        if not script_dir: return
        if what == "openroad" and not Path(script_dir, "10_view.tcl").exists():
            self._do_generate(script_dir)
        cmds = ScriptGenerator(self.cfg).view_commands(script_dir, work_dir)
        self._launch_detached(cmds[what])

    def _ensure_output_and_scripts(self):
        """Confirm output_dir + work_dir; return (script_dir, work_dir) or (None, None)."""
        self._harvest()
        script_dir = self.cfg["project"]["output_dir"]
        if not script_dir:
            script_dir = filedialog.askdirectory(title="Choose output directory for scripts")
            if not script_dir: return None, None
            self.cfg["project"]["output_dir"] = script_dir
            self.vars["project.output_dir"][0].set(script_dir)
        script_dir = xpath(script_dir)
        work_dir = xpath(self.cfg["project"]["work_dir"]) or f"{script_dir}/work"
        if not self.cfg["project"]["work_dir"]:
            self.cfg["project"]["work_dir"] = work_dir
            self.vars["project.work_dir"][0].set(work_dir)
        return script_dir, work_dir

    def _do_generate(self, out):
        written = write_scripts(self.cfg, out)
        self._append_run_log(f"[gen] Wrote {len(written)} files to {out}\n", "green")

    def _run_single_stage(self, key):
        script_dir, work_dir = self._ensure_output_and_scripts()
        if not script_dir: return
        need = list(ScriptGenerator(self.cfg).all_files().keys())
        missing = [n for n in need if not Path(script_dir, n).exists()]
        if missing:
            if messagebox.askyesno("Generate first?",
                f"{len(missing)} script(s) missing.\nGenerate them now?"):
                self._do_generate(script_dir)
            else:
                return
        cmds = ScriptGenerator(self.cfg).stage_commands(script_dir, work_dir)
        self.nb.select(self.tab_run)
        self._append_run_log(f"\n--- {STAGE_LABEL[key]} ---\n", "cyan")
        self._run_shell(cmds[key])

    def _run_all_stages(self, regen=True):
        script_dir, work_dir = self._ensure_output_and_scripts()
        if not script_dir: return
        if regen:
            self._do_generate(script_dir)

        gen  = ScriptGenerator(self.cfg)
        cmds = gen.stage_commands(script_dir, work_dir)
        outs = gen.stage_outputs(work_dir)

        def run_next(idx):
            if idx >= len(STAGE_KEYS):
                top = self.cfg["project"]["top_module"]
                self._append_run_log(
                    "\n" + "=" * 70 + "\n"
                    f" IRIS: FLOW COMPLETE. GDS: {work_dir}/gds/{top}_final.gds\n"
                    + "=" * 70 + "\n", "green")
                self._set_status("Flow complete", "#4c8")
                return
            key = STAGE_KEYS[idx]
            self._append_run_log(f"\n>>> STAGE {idx+1}/{len(STAGE_KEYS)}: {STAGE_LABEL[key]}\n", "cyan")
            stamp = _touch_stamp(work_dir)

            def on_done(rc):
                if rc == 0:
                    run_next(idx + 1)
                    return
                # Continue only if the stage's artefact was written DURING this
                # run (a stale file from an old run must not mask a failure).
                out = outs.get(key)
                if _written_since(out, stamp):
                    self._append_run_log(
                        f"\n[flow] Stage '{key}' exited {rc}, but {out} was written "
                        f"-- continuing.\n", "yellow")
                    run_next(idx + 1)
                else:
                    self._append_run_log(
                        f"\n[flow] Stage '{key}' failed (exit {rc}). Halting.\n", "red")
                    self._set_status("Halted at " + STAGE_LABEL[key], "#e55")

            self._run_shell(cmds[key], on_done=on_done)

        self.nb.select(self.tab_run)
        self._set_status("Starting...", "orange")
        run_next(0)

    def generate_and_run(self):
        script_dir, _ = self._ensure_output_and_scripts()
        if not script_dir: return
        self._run_all_stages(regen=True)

    # ============ SYNC UI <-> CONFIG ============
    def _harvest(self):
        """Pull every UI value into self.cfg."""
        for path, (var, vtype, section, key) in self.vars.items():
            try:
                v = var.get()
                if vtype is int:   v = int(v)
                if vtype is float: v = float(v)
                self.cfg[section][key] = v
            except Exception as e:
                self._log(f"WARN {path}: {e}")

        self.cfg["project"]["rtl_files"] = list(self.rtl_list.get(0, "end"))
        self.cfg["verify"]["testbench_files"] = list(self.tb_list.get(0, "end"))
        self.cfg["pdk"]["lef_files"]     = list(self.lef_list.get(0, "end"))
        self.cfg["pdk"]["gds_files"]     = list(self.gds_list.get(0, "end"))
        self.cfg["pdk"]["dont_use_cells"]= self.dont_use_var.get().split()
        self.cfg["pdk"]["filler_cells"]  = self.filler_var.get().split()
        self.cfg["sdc"]["sync_input_ports"] = self.sync_in_var.get().split()
        self.cfg["sdc"]["output_ports"]     = self.out_var.get().split()

        self.cfg["pdk"].setdefault("cells", {})
        for k, v in self.cell_vars.items():
            self.cfg["pdk"]["cells"][k] = v.get()
        self.cfg["pdk"].setdefault("pads", {})
        for k, v in self.pad_cell_vars.items():
            self.cfg["pdk"]["pads"][k] = v.get()
        metals = []
        for iid in self.metal_tree.get_children():
            v = self.metal_tree.item(iid, "values")
            try:
                metals.append({"name": v[0], "x_pitch": float(v[1]), "y_pitch": float(v[2]),
                               "x_offset": float(v[3]) if len(v) > 3 else 0.0,
                               "y_offset": float(v[4]) if len(v) > 4 else 0.0})
            except Exception:
                pass
        if metals:
            self.cfg["pdk"]["metal_layers"] = metals

        pads = []
        for iid in self.pad_tree.get_children():
            v = self.pad_tree.item(iid, "values")
            pads.append([int(v[0]), v[1], v[2], v[3],
                         str(v[4]).lower() in ("true", "1", "yes")])
        self.cfg["padframe"]["pad_list"] = pads

    def _push(self):
        """Push self.cfg values back into UI widgets (used by Load)."""
        for path, (var, vtype, section, key) in self.vars.items():
            var.set(self.cfg[section][key])
        def fill(lb, items):
            lb.delete(0, "end")
            for x in items: lb.insert("end", x)
        fill(self.rtl_list, self.cfg["project"]["rtl_files"])
        fill(self.tb_list,  self.cfg["verify"]["testbench_files"])
        fill(self.lef_list, self.cfg["pdk"]["lef_files"])
        fill(self.gds_list, self.cfg["pdk"]["gds_files"])
        self.dont_use_var.set(" ".join(self.cfg["pdk"]["dont_use_cells"]))
        self.filler_var.set(" ".join(self.cfg["pdk"]["filler_cells"]))
        self.sync_in_var.set(" ".join(self.cfg["sdc"]["sync_input_ports"]))
        self.out_var.set(" ".join(self.cfg["sdc"]["output_ports"]))
        for iid in self.pad_tree.get_children(): self.pad_tree.delete(iid)
        for row in self.cfg["padframe"]["pad_list"]:
            self.pad_tree.insert("", "end", values=row)

        cells = self.cfg["pdk"].get("cells", {})
        for k, var in self.cell_vars.items():
            var.set(cells.get(k, ""))
        pads = self.cfg["pdk"].get("pads", {})
        for k, var in self.pad_cell_vars.items():
            var.set(pads.get(k, 0.0 if k == "corner_size_um" else ""))
        for iid in self.metal_tree.get_children(): self.metal_tree.delete(iid)
        for L in self.cfg["pdk"].get("metal_layers", []):
            self.metal_tree.insert("", "end", values=self._metal_row(L))
        self.profile_var.set(self.cfg["pdk"].get("active_profile",
                                                   list(PDK_PROFILES.keys())[0]))

    # ============ ACTIONS ============
    def _log(self, msg):
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        self.log.see("end")

    def save_config(self):
        self._harvest()
        p = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="flow_config.json")
        if not p: return
        with open(p, "w") as f: json.dump(self.cfg, f, indent=2)
        self._log(f"Saved config -> {p}")

    def load_config(self, p=None):
        p = p or filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not p: return
        try:
            self.cfg = load_config_file(p)      # merged with DEFAULTS -> old configs OK
            self._push()
            self._log(f"Loaded config <- {p}")
        except json.JSONDecodeError as e:
            messagebox.showerror("Bad JSON",
                f"{p}\n\nJSON parse error:\n  line {e.lineno}, col {e.colno}:\n  {e.msg}\n\n"
                f"Make sure you picked a valid *.json config file (not a .tcl or .v).")
            self._log(f"[ERR] JSON parse failed at line {e.lineno}: {e.msg}")
        except Exception as e:
            messagebox.showerror("Load Failed", f"{p}\n\n{type(e).__name__}: {e}")
            self._log(f"[ERR] Load failed: {e}")

    def restore_defaults(self):
        if messagebox.askyesno("Restore Defaults", "Discard current settings?"):
            self.cfg = copy.deepcopy(DEFAULTS)
            self._push()
            self._log("Restored defaults")

    def preview_selected(self):
        self._harvest()
        name = self.preview_choice.get()
        files = ScriptGenerator(self.cfg).all_files()
        ovr = self.cfg.setdefault("script_overrides", {})
        if name in files:
            text = ovr.get(name, files[name])
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", text)
            self.preview.edit_reset()
            self.preview.edit_modified(False)
            self._preview_status()
            self.nb.select(self.tab_output)
            self._log(f"Previewed {name}" + ("  (your edited version)" if name in ovr else ""))

    def _preview_status(self, dirty=False):
        name = self.preview_choice.get()
        edited = name in self.cfg.get("script_overrides", {})
        if dirty:
            txt, col = "* unsaved changes", "#d08000"
        elif edited:
            txt, col = "EDITED -- locked (Generate ALL keeps it)", "#2a7"
        else:
            txt, col = "generated from config", "grey"
        self.preview_state.config(text=txt, foreground=col)

    def _preview_modified(self, _e=None):
        if self.preview.edit_modified():
            self._preview_status(dirty=True)

    def _preview_save(self):
        name = self.preview_choice.get()
        text = self.preview.get("1.0", "end-1c") + "\n"
        self.cfg.setdefault("script_overrides", {})[name] = text
        out = xpath(self.cfg["project"].get("output_dir") or "")
        if not out:
            out = filedialog.askdirectory(title="Script directory to save into")
            if not out:
                self._log(f"Edit to {name} kept in config only (no script dir chosen)")
                self._preview_status(); return
            self.cfg["project"]["output_dir"] = out
            self.vars["project.output_dir"][0].set(out)
        Path(out).mkdir(parents=True, exist_ok=True)
        dest = Path(out) / name
        dest.write_text(text)
        if name.endswith(".sh"):
            try: dest.chmod(0o755)
            except Exception: pass
        self.preview.edit_modified(False)
        self._preview_status()
        self._log(f"Saved edited {name} -> {dest}   (Save Config to keep the edit in your .json)")

    def _preview_revert(self):
        name = self.preview_choice.get()
        ovr = self.cfg.setdefault("script_overrides", {})
        if name in ovr and not messagebox.askyesno(
                "Revert", f"Discard your edits to {name} and use the generated version?"):
            return
        ovr.pop(name, None)
        self.preview_selected()
        out = xpath(self.cfg["project"].get("output_dir") or "")
        if out and Path(out).is_dir():
            (Path(out) / name).write_text(self.preview.get("1.0", "end-1c") + "\n")
        self._log(f"Reverted {name} to generated version")

    def _preview_export(self):
        name = self.preview_choice.get()
        p = filedialog.asksaveasfilename(initialfile=name, title="Export script as")
        if p:
            Path(p).write_text(self.preview.get("1.0", "end-1c") + "\n")
            self._log(f"Exported {name} -> {p}")

    def generate_all(self):
        self._harvest()
        out = self.cfg["project"]["output_dir"]
        if not out:
            out = filedialog.askdirectory(title="Select output directory for scripts")
            if not out: return
            self.cfg["project"]["output_dir"] = out
            self.vars["project.output_dir"][0].set(out)
        out = xpath(out)
        written = write_scripts(self.cfg, out)
        for p in written: self._log(f"wrote {p}")
        messagebox.showinfo("Done",
            f"Generated {len(written)} files in:\n{out}\n\n"
            f"To run outside the GUI:\n  bash {out}/run_flow.sh\n  bash {out}/run_flow.sh synth floorplan")


# =============================================================================
#                                   MAIN
# =============================================================================
# =============================================================================
#                      BCDC LOGO (embedded PNG, base64)
# =============================================================================
LOGO_SPLASH_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAXwAAADYCAMAAADfw6i0AAABIFBMVEX///////7+///+//7//v///v7+/v/+/v7///3//v3+"
    "/v3+/vz9//79/v/9/v79/v39/vz8/v7//f7+/f/+/f7+/f39/f/9/f79/f39/fz9/P39/Pr9+/r8/f38/P38/Pz8/Pr8+/r7"
    "/Pz7+/z7+/v7+/r6+/v7+vv6+vr7+vf5+fn4+Pj59/P39/f29vb28uzx8fHs6OLn3cva29zhza3Ozczbu4a7uLXSoVLBoG/B"
    "jkS6hDmho6WVko6dgl2teDGkdDKebSyXZiZ9bltiZmtKS0wtMTglKzQjKTIgKDIgJi8eJS8eJCwcIywbIywcISkaISoYISoX"
    "HygWHicVHCQSGyQRGCERFh4PFx8OFR0NFBwNExoMEhoLEhoLERgHDBIRfrFiAACEZUlEQVR42q29CVvbSNY2XNZW0+ppe4ZI"
    "LdkGbIMBL2AwYNwkuJP0Y5YQshNCNvj//+I7W5VKhp6Z97o+zTQBL1ruOnX2RWmdw6HTPM21XkrDUOMrWqsgUErBH4EK4KdW"
    "Kogi+k2H9H858DNKDj5Xzi/a91XpszqES4T8fUX/p28EQcTnwb+TGH7SJ/LinyBYWlqC35Mg0L6Pr8JPPjncVl6r5eZqnr0C"
    "/ZkWJ6rmeZrq0FMhvYGv5bp0u1oexDwLHKncouJbzvlU8iZcDT4fBkGS4MsJftz5DPzIisvnCdxpGqdpnmXwlIniS8uJ8EaV"
    "zvmuq3FMd4Ynxg/B01nwq4Qi/KjR9/PFRygdcOoqY6BCXa3q0pMCgkFgEMzloVO4vyrdBP6apvbTga+TJJE1UD7cW0AXgBeD"
    "wOfFxR9ARB7TkPLMDShaD1h7emD8WprLjcutM9lUGWd8D77s0RopBbRRg0PLIvDTerDOIWGmEwbNV/ahnSXKDREGYYCw4SJU"
    "00SVVl27yGRCFwEdhhadM8dw8wm/kLrgW9oXApCPEKHhP7lzS3xyJhvnEklafDF09wAvlM5yui18aPyha4A+LWCC65fzcvE3"
    "ZJcZzAqCyEsUrcznq+5Nw763+CQJXYRIQj94Lj6Pwn2dF3jmZSrUKkrsh/NENjpdXSm7VeGVLIsMS1CET+nWEXsAHwku8DxL"
    "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"
    "+SLY5LkPDitbSQD6Qa/Tym0zucToSMI/nGoyVaE+CPVGo4FbXJl+ZLCBK8RvpApKlztyWr4dFy8Kg1ImeUWpwmwMiv4ztq92"
    "EFY4sqwL1zUTe80UjiQFj03rzax9VIC/3V+ue9oUFupyuJK6IDJeHov+tMwthHQjcS3xOlPurFQ+AtuveKBoAC6N/H88Mp03"
    "uxubvUGv26oX9l9iuhLZTuSaO4zAWi7V68uYmowXIS9DikVYabrEGX5YrfLkSWZuATd3xAUPyOeYl9reC7xOgdPqR/uu0ySg"
    "/yjLkAxRNGjD8BdexKjwDqXMLZLE6n6Y49+ILPgvjn7tE9sRYkiKrn8GfExR8EDbWUpDxxDLdF44zAxPRR8PLnqcmJg3kZHX"
    "6jaoIx/l9paPxU4GFWz2lrYGw53hAMFfsbyhlueuJ4+Qz9YHOzs78MGtrV4Ptgr+09vYwqOHx6C3mlfC1nBHjtHOw2MIV4GV"
    "ahkrNy96BUtdWWDqx3zyyPKb1VZrbW3F0AvTVsse5NT1A+ot7Dasj+sdZT0Mzw9VnwuWdKlXkflRwa4posCmC1GpXLPx9qCh"
    "Tir5ILaDad7d2xv/r8ceHju5WhvuDLqt3GkUW/R4pN9Xu901VIR0b2cExx4fIz7k952uSaj9+0O3NmiTPVJF6heJxCbTuqLz"
    "1nq31+22ysd6d6PHR7cFK8nF1gF1SzB6rIfg9wV8zODfV8vLddNPJc9LQdRAOR35QpYBEs50GsHkxtQNjNfctP/hpMK8t7J3"
    "Mj/9fzrm85PpwaYe9Fq5O+6gcK/Ar63BQKmdg8n0ZDb/+2OqMnVw8h+O6XRysKfUOoCWO+GSitLlppCFk2dtMMD66daIVhq2"
    "znC4M7IH7FjYrzG7hlEeR5KZCNgnIJkc8I93q81mPSmyKiulCh0PqaZiFXcTZuBc18Q4UozsCSTYzVlipIUgga7rk/O3r169"
    "ev369Rs+3j483n+A4/37929f48c+XF9fns6ne5V6K3dYYE73l600gC21VLw3nc1Pz95+/PT5883t7devX29v8Li9vf327Scd"
    "t/cztaKmN5/pwLfg/c/moE9/vjo7nc+mB72kBWttPYaRm/hfpHf0Bpvw9+jgP684HAf47NRE2iQ9k8iMmw74R7806ikYCRVD"
    "r57ObLiAiyNi7s9CthXHnh1njB+UO34vdozLWus9tTd/dVY+zp1fzh8c9NLlp88X80lHr1UXZk0kja3ecCcaTWan5x8/vH51"
    "Lme4uDg3v1xc8nFx/mamcjUxr7/C48L8Dh9Acvh4ffP168ez+XScq25uNn/h4bM7D5h8r6fWx5PZ/Oziw6fP15/guL6+/vz5"
    "GlcfFv/rt2/fkASuv55PBfzIdrUl3SUtwAeW/1s9jh1fWMjVDBZI7jIC4Ke5sbiK/HBVjjkFpU6RAv5qb6AOThH88/90XDgH"
    "/vHq8tXFxdX1+WxPrRS5VuQdajS2drqj6fzi5tNr/i7vKsCSwAVEr2h/vX//5vIjgf+aXr/ETfWaPgW/w2fewU578+bqCl67"
    "+nh9dXpyAOynZZrqFa5GJsVWtzfwcMlf0ZLDdc8vnPvnteTbOL88nai8xeD7flAJqVg/CZO0vuwZ8J+Oa03EPsJ21RjLoNok"
    "XVSGo+kRieariwxGmlogZjYXqifsErQRKU9yw1sE/vnjJP4o9gz+K4T//NWn+Vjv9DKHAQD0O9WD+dufH18z2Iz45aX7O+AP"
    "4F69fvV2plbV5M2b1/a45FW4evNWwH+DH4SLvfrw4fRkT+VdAk2Mi4LprG0NOrDk57efXl/QeuMqXxYrDhdkBgrnu/x4Nqm0"
    "mUEk1IiZJHX0JG80V/51ZLgOaPm/kqf7SVTNUtCqwvCJ2KroqlZY5lw1tkP6SG9GzcWHlu0EUuFoXFoG/L8l/Qsiob85zs+v"
    "5weD4bodMKFXezu9wcnZz49Xb9+9KeC/dNC/FHSBpl+9P1EtAP89Qyyk/5rXRo6rK9kP5+dvX88nSnVzXUp1oKMFAnkyf/0N"
    "lpzJ+9Ku5uWlLPeVyLP3V9enk3qPml7UmAVTc5Yoy5vNbPvoGZepHzb7nRjdyUtRVKQ2hlgRkieR25BMazvzI3SEr+Qiub7l"
    "Cp+KwI/06qPgX1z8B8ovw3893xsMutzDL0K1Mh/NPnx+/eY9kBhhRo9O/78swCdu/ub1+7MJgv/6w5t3ALLQ/6XBnn4i9rJ0"
    "cFOvz2YjhWq6cpuxwT+osp19vX73/sNbu43wy1d2PQl9IH/cTjfzMdx0V3IelYCPsaxlb/eYwAddJ+k3/4ENGYOQqn/S3EaG"
    "xbQNjT8zLfJBCld3rcbBc05x8jmWyRFeXoIaU/7FA/CFsC/+A/byzoeT1dFwsLHRbq2vgsWgxvNrQPX9O8KSie5SMDU84JJ5"
    "+purTzM96AL4H9/TWtlDiBR/OOSL8L96PR8r0HAle1/2W3enMYYlv3r/8eP7t7KLrt4420nAvyLw33x6dTIYDYF/8XiLCs5b"
    "IhcnupTHTxn8w7hRb/IwnkSmJ9QkNyXQUSjDDzi4FYZOsYAkvIvXSGYF2ZAWhl+fcMAOpNRQTUDg/hc+X8bcYg/M9fz96QGw"
    "GrRgNsFkBew/X7wyJEuoOfzcIX6Qp1efT8eD0UBNXgHFvjf8pcx03pgX5asXrz7OD/IhXGx9fXWVoK+2uoN/Hcw/f7h6j7ow"
    "g391ZXbPVcHMZD+8gd0Kt9wySqFE3Wo6b/T7/iGBD+JW19PENKU2vdECp9PgUugHUakg0PYHAPlgg6ClhArMmJAgIYG/E05O"
    "X/9XKVui93O7L/CfTzM0KNF7MBitjOefzkW+XRnsCQnDuQE/EtcAwqfzaT4aDoPJ+fW7j+8tzIZoDfjCs0UNunwFDHsN9lqv"
    "t75KkfQc6Odg/unVJRsoRlLICj6kgcvPpwdBb902B7KlCwR+80jEbb3fTIKlMPAZfJC40hHQZLUugaIf8fJ4nP2aFg3/U/Hp"
    "+NrW/1CydCWItKmkaAD4tcnp1fnF/6RkPqr8vDk9UN1uG6THsLc3v0U11KF2sdxQpF5dooLK+hIA+Qak7WgwHDYOzm7eI7tY"
    "wP5KCNdIzEuRBlc3pwfd0WAA4MsYKthuH85fLawa8i3iZm+s1CUd9tWHs2lvp8OVH7b3Co9haXSakr7wdNzsL2MDp0gKCKW9"
    "aS4tvUIa/CXjWKz/PZWhfswQTQP/vOjQTZUjrPpkSaPVHerJ2fXVO3paS2aWR7oMADU+QJY1d2YCtCBvpqo3aGkwa9PZZxd2"
    "xuwKMKHTX75yl/jVh9kqINgbDsfzr7A4rBrJVingenVRiOkrsg7ev/s8H/dGw95qVlGq1e2Clfj2nKwy1ieR+aDAwF/es6Jq"
    "j9ewRjNY8RYZPhJjs37OesfbF1XnH83lZhDZ5oilKgdL4CY7xTZvTa2tjdFGJ/vMOEZc8AH9QXjw/+TZObt4e/nKSkCE88N8"
    "0NvZBPDV9NWb18JkrwTB1x9ubm6+fLn9cgMm58cPhcPiaj7MgXrXe6Pe9OybeBXws+hjAKsUDjRRP7xBMUvgs7h8/xEExGy0"
    "Y8Af9oYzIAj6yJUgTvbBu48gft+/w0V3DI7XH+d7MdkKmDJdBj+ud/wjcetE9XoaoBgtGkzXTBNlUbOy2ICfF3pXKAEVDjZK"
    "DWfudkg1WqsPQmS1MRofOMd4XPrzwTE5mV8AxK8RW+Ii529P93qoPNT2Tj++srsG9ztA/xFdM48dJ3u6lTeSBBjf3kReK7lf"
    "5K/Ts0vA/xXxFIMscI54ACoP4rKqpmevzl+JHIGFAcjffbiGBbym4xNwNKYH+PqHj2AWtoetjDqlG8cQ2bfV9Nd6Mxauc9js"
    "1CnYaPKL89wM4MilUztRdEXZgra86PmLCVXsZ6g+mHSUZLEblgA7u7ECivpqrv+Lc1eOvdn5WxGgxBdenx200VuoZiBsLdPC"
    "/f7xfH5yMN75m/Pw1m+sg8CUI1npdrvra2srKxk12Imy3t4B+mregDgV5EkpBRVLoZsTVH7QlD+cW7fFezSL0Rcxn5V9o9Op"
    "/DZuDHo5dVEOfL80Sapuw+dYNNfMtMk2clrp2IkVrAbZ/GpM5arKfJ+lAnw3nzh3h2ApJVUfra0BcMHhEO0OOta7f3/0Rns7"
    "g/mHS2sQIVOfro12RsHk7K2xX5HOLt9+mE8B18EOeXYfBEm6moZVZutA+w9CKcMh/RgOASg1ms4vP7wxjtU3tOVmG71hN+8O"
    "t4azt8ZlROC/e3+NXrhB8jdLXunKsAIsanLAp9qgXymM9WzfW16uZ5y7xbFiX4oBKf8Ao2++0/yZlyPl3jpciSrzFuPMNHWs"
    "1bjFF8UgeAqobau43mp38hXp61HN7ARf8pNi/yB6qdFa3xwMRwphtvzl3ZuP0y1QGXvzm8srK2pfX364mO2p1lZvsNnb3Oyu"
    "rReBjS78vsIeXfQGtdZBaoLmvknRriGFyCQWstalcBdstrPPwE+QiSP4sNtOJ4rAz+heXhnz4N2b91fz6chfW+sJRfV6bWw7"
    "Aj9anfX1dY7/ON0Ti2BAva/HlLpw1I7r9Zh6C5txWInJnKGhxdTFRfO6OOP5yuS+UDtumpGwo4fHK/Cstxy5jvSzjzARDzlc"
    "tlBuT4YB7IqeIuZO+gc+77vrk95oJ5lcfEL5JtrFxduzqVob9fCk/17JZQhMKGlNUsOlyD0FF68jAdQ3BkO0FkAHL3VcXhuO"
    "utn0lND/AFyHVNar2QCXsbEHGr5Vra6uLt+dwZLvjAbrQCktWHHWO0qtC2wsMCm6WVIMscEZU8e7aT2u2AQELiOkYYNFTYnJ"
    "GpRkwSrWRQD2aVWXStHd4S3JkzBJYx5nVFHYjw/xR6lMzhlTdexZDcA3w7srJiWhsd5dU/zErPsB+B+nw709NftKlPmG2ML5"
    "6/MpqoE5Jo/42s1LMJN+qIidEv4tR/jHP7aAVLu2oIHBBwtiZxRNTm/eAvbih7j8AFwfFoo2IXuPSPd9fTpdHYx6HFhorGK/"
    "uixbmB+jfDtex9Q9wOXS9JcKidun+5WN5ZS6I9Qkuy3jHKyAipMwMSpw0yUUl9ulPANdaf3Y1Bpt03Ul3ulL/qXmcdBS+iNe"
    "oWqa6nJTL1qPqNUb6PHpp8vCAfP+YgKCYHx68xE0QzKWXr969eFE7QxIhEc2Hy7JbdN+zD/AJkg41nFCspFVoIM1AJ9b5Zmy"
    "GZ2vb/V2RuvTs09vxIKFJXh/edIFUTKYfRDvMZkdl6fTBJT4jFzECTZwiHQpZVUFS0USZC71tRgWyVrNJhP+YSMHpqN4uClT"
    "C43OSbh1upNE4WszaUA7HfI9TljOF/vicwaMKoYvMyfL3L1ZShLhzKKMX0aVKM96o5GaXry/IlsGLJq3V59Ox6NxOAW6JNJH"
    "9F99nu90R8OEEtPseLZEkmWwsI/KYZTutiors6+gkX76fPP127fb04OVQUsmPiiJgSIDAZncPQGaRxcCMf+PYGnBkh+cfn71"
    "2ijx56/OTvRwmFtkqpSrVXMK00LTJVQVnTJyTD6O231KzgemozGG4puatkDZBtImQ7piJ0UatkOVcp6pg9TFwHcZgIaZFSZh"
    "p8gAIvDXUMNZx/+7xzq+yId9CX7d2auP5x+v3pvj7eXn+Wi0l4Nxi+vxgXxbHy4n6HukKT3Sl+H3359IbSrWfHGTTZV3uwD+"
    "xzMwUF+j8frhZqYG1KgbTCCqSG8klDbS6vU0M7u3b2GNP11/ujnD/TY9//DKOuwuXs1GGhVJzHtFlRpI35kAgX5jB3xnajO1"
    "hOlgWcrzp/vVfic1A6+or0TEsBklH5cVaDWWjo3SXiHkiWlmrkNqxUpkOE0YxuU2KX6pA50OecoEJyQWOTtoZiNfWiLG3J7M"
    "3wIG7wj6D+/fXd3M8hFwHQD/LRv37998m68P1/NI2wQzab5oJ/OCvMF+HQC+vzL7DHo6mECgSF5dz3uRqZHlpv4JZVLVG9mK"
    "mp6DYvOWTKVPn67fz3b29tifYRn+RLXyfKEcyg2kBqEZ3VAoOswS4mU1PgYt81DVm3VxuDNLwl7JODhPxg7wlpGReexi1qXo"
    "TmmuipvnVnRjlOks+UquxtMJHdP/6TiZnTEBot6NfOb6DESfOjj78JqRB2w+fp6ujAYZZ7z4xC/Ra57Yfp7s29Cq2u3qLoD/"
    "mr/74e2n+Y7SpnpE21RzMARXe2vjOS0ws50Pn+d7awent/AKaTsI/wzEtdR65nloBg0YUGSgVqgXGyrqIMUBFUeoZXYadRl/"
    "5hhGnnzDccS5vdzt8CbU722zfM4xXSwIcobghmugMJycUYYBUF8porFwGM/Uh88fULdDsAh9oPKRytX0w1sC5gNC+A0dDoOG"
    "dTgF9P+YEwolhzjDOCiAn3dnt684vAcwfoSTlfKTEPwaqsIgc3snHz+8/fCera0316DvTM4+wwtvGf+3Z5PKaOCO+XI1nYRa"
    "VKrIgG+z0jEqmTb76f7T58+Ptpv9JqkIFSkrM31sKGDiETllYtSanrOUOoJ971Lb7tEm+Lp+NVttKI0xVXcI4J9TfgFs+4+S"
    "nGOOD2xU0g/zN9OZofL3b6/PJys7LTW7eYu44Cvvr37OWoNhr8G7MSsSV4r+D0sEB4K/1pt/e/3+LV9UwM+0KtsnIOx7w1HP"
    "YA3r+/b1h7OJmr76+J5uju5kPm5JjCQ0s0BzLeWLMhlJR2Gh7Be5y2mzvn38HP1p2CUiyqv2Mym3GzHj4XynjYzdAOITNTK2"
    "6lB+0b1MquNsa6IF8JmiWJl77SZP0QtXQu3vZSHeyxufZ5ujva18/hV2AX0dxO3HqRr01qQ0LWPlVarDTN694gBEd7M3nP98"
    "+5Gv/PrVB6F8nTi3jF9sdQc73fHpN1p/+uxbsCRmn94T78P/Pn2cgVUrPF95RRtkmRVI6R5JpMLEGZGLcrgSp/9qq8PnL5/u"
    "R81YRnhg4Yqk9FSl6tmgK/NMPWbtiYBflTZHzix3d7KVBd95pYts5/z1K3L5EdkT+iZPzWg0CKkwGtkN/LnXlzfzvXw06u6c"
    "/vyEPuCPxDvOMLxiktmqpbRpnggWSdt4BH8HwP+E6IOO+krYTl4GH4UT2Bfdvfk3Yve4UrDuJ5vzz28JfFR/Pl/AknedanAD"
    "Azbvo2EuPucfOH5dZB1UkOLtPwXsaxvLdQpvVypYrOl5ti+EstWGXDCMfduokVWyFDhDGFW5odSDbgHluVTrBP7b128kI9Cw"
    "HZacDtYsEPlJPwgfAu7z+fQgHw531vdOf15/Jgfuh7evPp/uqUaraPNAd2A7DVBHVan4QvBH87sPDP5bAT/LTX/yYsSEzru9"
    "7mB2gz5+voW3n2Z7c7B6UfUB5efTLbChTUmbZtMuM3PGtUx7Z2lrFG0Tf9Vpp7l9/PLZYQO7qmojJxK85VAXA42qRe9FO/Sc"
    "uRMapHat3d6TUkGEAk9GNjspjnl3OIxOXn2w0Fu65r8/Gqw/FH/Kawz+/EDtDAfD1vjs5+cbQf/VzZy93ZRtkZvZY55c1BTv"
    "F5SP4H9E8N++/oTgU84zJV1gZFXYlW4BHzv5+OkTB1ngHm7mY2B2n+T4CAYap8+pQo8uGiwmZF5IXrzRgFltXKq39eGL54e/"
    "dpZTLpeQEjzlti8jCqqy+4ZawJg6e9+CvxDvWnApi6Jtu74j+KPs5LXF1wG6wLr8Nz85E+pHdKtvDgbD7OD85w3FLwD811/n"
    "Knf6QjozUhh8U6gRGPDfGmJ+CwYbVVYmUj2SF0Og0Xc/fY/Jlxwluf42Pzj9+dGsxXtggKYBqrRw8MvFxpTCH5jyOxrFhxnl"
    "dez0++z5YZjX65rmL3NmfeS2dSi6iqRsKqCPzHBShJ6sI4ffO42XEsxZIMuhZpsMYykziLHR2snba4D0Y4GwLIJD5iXoBSna"
    "H6cTsD0Hw98PLr4Zyv/4+huBny20N9TFxDQZWAw2IwncO/ZMIPrIsXKplaqZ3hC+z+DX1PTN9adriTde380np3csaXDffCbw"
    "M+10Qxd+Y/OFWbmnXyo27lrv13ePXxw2N9qx5iGDwVKJgs3sZ2p2xTFd6ShOvWw5Y01a9hXT47l3HTmEltjULdg9NtjOGl1Q"
    "4DZPPghqH0v44m+kRJjXP36yW5xp/8PH67uzKQZL9MH57bWJu77/OVfrcdWoNgWTY63DNBcIQWsG8LsD0nZklQF8vSIdC6lb"
    "Vm67FbVaWk3ffvpEScdfvny5YfBlH7rgk//MZuT4gRSdaeoyWkGAMTlQHIrx8kbz6OVRs91voljinhTEyIs5qyY+uxQy9hyy"
    "Slmc1IpJ5kUHgvJ0VF2y96hYNs3rK70BmC6fbhA4i/ina8tWi9WgV6/L7yDV3Z2jPzkYn322n/p4P1/ptaQ/kRunM/5pshXJ"
    "w5qAUgTgv7ab7OZ0L28Znwu72w2jarWqYMrhFQj8rwD+wemd3bMCPiua0tTMaXvujEJSns/ark8unQ3QMo+aLVAycUhZhexY"
    "Sa0xiJoJzYXNiG2ufWL9Rb+6xSxZtZiZX3T3xz3U3h6C6XJyfYtJ7NcGfIOyZUT2pWu7RPK5z7D5zya9kdo7+/zBvnQPBm7P"
    "Nn8sXBo8kEgV449A5nRbPaR8WWSQmnsZr5tv2sxKixLd7erqyaePDP4NUf7B/CfvTLxNkL+qaPkMy5YVoaRCd+IZlzaNaamO"
    "TX6P6o1Oqj3f8Th4ruvA8hLPumZkjp8dImP4jSrmOSn/QWNAbvus4npdNff3hzujwcn118+YufGJGMv1zc3tl9vbW0z1+MQg"
    "33Bpwe0XFKmfZD0QaCwd+fhzvldTo9PP5GxEWG7uzkD/JD3f7W4VBE7PHJxtuUTgNwR83jhfTzGpweiXztxQMIYH+WB2+9Hy"
    "/JuvoO38/CBfvYavHoCq6Yq6qu3znNcs9wiM74eUzLihxsdHv9SoaziOMjIDewJnMhX1Dy61j6I4Oc7mwxpiB3zbwMZ0frF1"
    "rBKyqWDD00reUduHh7st9JgI+MSzP1+/Q8fV+3fvrkwu7CUGajE+eIXFJBefPqPw/WTqdsC6mSrVO735QNgDM/7y7ZoCrI5J"
    "odzWaE5/VAO+cKzP198I/IUWnmxkgUK7N//5yRQNwb3OhjMM4PBqXH8DPX+wxhbuI+0mTDPXwJamKy9pdrzd48Pm8nI95MZH"
    "pi0sLIRp46woGO5xoppOydePQ6mAsT3B5lxalYdGS1QSBXK1lritJaS1edrpRLtHx2NMLVg7+Xh7bR/p29lsyqkWf3fMTq8+"
    "f/h0LdjffP7wDZhtdX7LryH4N/cYxmpp21y+KOIppofy/ux2800CX+4AwI9bef7QJmyBUrwFPN7cKaz55Yk6uS6qt76+P4El"
    "77ptrMSFE+hi9JSm2HRILX/8tKl3jw7rGTp0hES4PQyDny+0lAMlJ+beMIhnKgZj0WzPGrhszJFbCH2pgTPnR3txu98eH/85"
    "Vp3BcKd28uGmeIZ72LzZf8nbOQHpaurVkFt9AtKffftkEs6+XN/PdzCQulDSWarlCWgOUtbt1jfByJLLf77+CaomdgBUSaKD"
    "4lvoXcA9+unHzS0nt336eIOOtatbvg9Y8Zuvs53eoEufD+wAPR9x5B5lSUjznvXvEWXdRzqOo+3D/TrAiE6HhPP9pANixamq"
    "tU64KosxQ80VD+vxCzWHf2CPGO7PaCsRK85oh3qnne8f/7mv0sbWYKhO3nwmJgKM/uvt/dlkiLl4eGxink6PEjqkhnWzu7mz"
    "t6VOLoHwDPifP97NlZresKxAFD7fXhzURgO9gL3rXJdbYfDvP93IgZSvaGKBtiWY4qfpgkY7v6eaRloq4vGTs5+w3Pzd66+n"
    "4wpdtWLMCSeB0nRE8GVEXZKngH1/PP7lSZpiyx1Uv6oipqp2ULI7ocBzNZ6QC3eThUnQxnu1VOpqhwlakpCmm83m/p8vD+v9"
    "ZtpF8F8zId9+/f79K4C/TgkEcqyu5kVnAEyl6Q129GB+y+AzBwDtRk0uf35BmYzYf/50N1ODll6Ybq7dhkEW/BaA/xkAvBXw"
    "R4ryjMgD5LuVnr21g9P7L9+/3vInP3+dj3rj+T0mgtIr15/Pp3qEEdyKWoxlIRfwqSUf9lCKPDP2ZXs31r+mOIXaN/NvK+Wx"
    "vY7uqBTNmozEOy4lQKEle55vQxOCozDRtdh2nZVxxsqr4ljxwz//Oup3+k2NpcoEPmL/7fsPBD/pdVdiX5max7hG6hSB3yVh"
    "2FVTK2/xuLuYRGME5tvXL0SXn27Oxl6rVY8q1KDR6QsgQb4kt/pjdwgAfvuKu+7L7eefDH4u7SDN8G+NUjg++YbVnLDEsMa3"
    "X69nO3uj2Y8vX6R4FzbjfK/by50gmNtbH6tms2okDlURBzUVY3gzNhVU3CIHU0AKL5hNKZBz2mZZ3Oe5otzugqnJ61GuU5mT"
    "lGCjrGz0+4fYOS9u1rUSyifsEXxgOweqg+k2aAdlGbXvCSKRMbAJMIVhoCYXt1K1DKB9ufsy3VqZI1V+/SKc6OcMvdWrWa0W"
    "m3blJSrKJXWCfDv3t98RU1iBGwLfmX5tnLXrcJ9A+Df4KSyn/f717mKKAfTLnzeiqX36+PlsqmB3lrQbG8PhQoYIh1QL5Xoy"
    "4QyIPeL5L76wHO10DCvAtz2NzVuhsv3buYG98DjJx0+qJV96iIPCOh3A/s9xHXu3Rd0h8vwbAf/bt5s74KSt7gr2ck+S1RXs"
    "8RooSW7DZAJMExuqyfnXz1RM/gW/dXd3slmZfvlBNebMkG/eTdVouL5KeYG5Tdh1nE8M/jqY2Ag+oP+tAD8zihIlyWE+V3c0"
    "HMzgmnhB4I4/7r7fn45HeyOj/7D59xnsrCEWR7q0KkYR5eH41vKhHqmUVfFE+gknD3qq23utLnRYUu5sBK2KzpKMPgrzSC82"
    "4k2yZr9TB+xf7KsNYDrAc4cjDarmzZevt0R6sO8PVN5thFGUJVmDUi8CZy54gpmWQzX9+BWYNG5+2C7f7+5PouEe0OWt5USf"
    "wfDNd4aDra2thjP0xtAP9j0gh02XwP9694No/yus/ahw0Bjw14A3gS34+pb2GoJ/BwcmLwy7s1uj9ZORMt8bjDCPcK3F1gJd"
    "sFbjnmA+pQPRmDBMLiGmnSVPfiPws7RmB/qY6ZzuJCTMy6H+RpTiim0plzCloyoCtzS9xa36t3IrrTfrTcD+5WEcYeNZdCnv"
    "rJ1c/5TCeGDYSPkr2OIja3CuaqN0vnQVfXGN2R1uf1wu4ADfftydqJGa3YkUQF50ewPCY200Gm5tEeXmK47lSR2riPWjZ29v"
    "fg975wed7/aO3AtuCCtprW3ujHYGU4wY8MmBPf6A0/sjShf8+qlo1QCCAGvdqLdI0WSFux7RiBnqSinhlqhCnAcHEJemLxcD"
    "Zx1mXmW/Zpqy5UothWnYs+/Kdgn4GlXV6MsU3BG6f3m8W12mYCW6lHsnN3ffgfQQ/FsEXw9Y1Rzw0SsfoPFVJ2f3QPDEKuC4"
    "/fllqob64IxYEdEmLMmX+/OTPezLAzi0Ftvtyq1idvJOCXxQnFpdiz7wuiheH4xGg72Ti/vbr7d8oHC6n2NZVg+NXgH+Fvfh"
    "3fVsvDaEHddpFDEpR+eVembWSQxJcmsLSo5LbIFpaXae+G0ofGUSzyRLEM5RLXuBODBcDt6CotTpoKx9CdaVTKRA8AcnX+5h"
    "F/+w4IOo7MG2RW5db4GeSd2B6nXKGM9i+MZkjtseyM/wCsxP22HSJ65AfOH7/df5ZG/QbtP+adRL3apaKx0AHtPBGfyfP1jk"
    "AviD4cBtl5N3er3Rwfz6/sePn7Q76QCupnZ6cC+t6fnPm690fKMLf5lPhi1jmlCe3ULfHep0FNqUPd/QbGKaXFm/iHWE2I77"
    "1EowLRItQ+k+83By4ELDr7jZaRPdvzgEO4un3efETBl85jt3Z9O/KQsy3Y4ms9M7Yrr4JfzWDer5vVH1ACyer99+/Lgzx/39"
    "xXx6sNd7zEyu1hst3QD4h8jzCXpEDykfu4ehJR/HNHmzBVc8u7/n633nu7z9OusNhuvZ+s4A1+77D4D9xw/8Cac4mx3s6N7Q"
    "VFkMqciCDnphgDwpz3gW+6JFkC8gaNqcsImVOvnhpBAbyRSG7gi1RzxLAH6yD9j/dbydN2IZqkts5ws9F3P9b5/O/ltF3Ne7"
    "W4D4x8+fgv7n+7kH1j8YDADfnXN8/3J3f302nxUVOvKbKcuaTvZ6CP4tMR3QYe7uTydyHBxIHt1sfnZ3L0v6E66LWhGqBb1e"
    "HVtHrAAP/E53wuCD5nt7OoM1J9a/udZCU/ZfDaq/GFC1RLsN7KqRcJcjCZFL5tMDOzAv6yyUffxgfAHX35oeUpyQzSsgkzNB"
    "2Lb9/T8B+z/369udquLpzAD++snNz2/4TN9QfH77eV8cRL0Lxx0o1rfwSQD/Bz0zcPdZtNODLQ0wlsD/8fXm8+2dEC1CR8ed"
    "PevdFTAPdBrckvKOb91/WWz98+nu/ufX2x/OaX9+/fnuRK3i3CYFViLILLwsgY+P8OXm9u7uzen88To8ykMfcQFGwrOl8tSy"
    "FfWg8fFiYVVuZm8W5gC1x1WLhW9O+DhKl/ut8TFg//IQB4q3eFJVBbSd/OT62y13Avr2zezd799IZ2en1S36DW7RXUnKJEtU"
    "+CihD7z96mB91NWtncbB2f3dfYH9N3IFsC124xwkMnkJpmpnRBaugH9XXnpA+hsyGbiau6O+gUYpmeCtHVCX7r6z+GcZhHde"
    "JiFZdPnz5hQ0gcF6xrYq9QYuBjzlCwlPemFEZDHLzU67Uou8q+K2H+NSu3z3GOe1HY83NjogxxJyy4GF66GeT3gi8TjQfRM5"
    "hm98N4e8ii8KFX+/n+8Md3p5YzBSLD6EQH8Y+fiV/zPS8quDJYC/Y8Dnq/PKG8FKH5erGUECvOmgutMT8Klzx/2t3NVXOTCq"
    "QPdLRrc54FVc9vvrE9ip7EnQCwMeCoeGNV3zUpzKNdczzinhPsZOusbCjOd6098+4jHTzf4WKX8p6rvkXvh4+5mpyz4hybbv"
    "hhN9I+yZqf6gV/EHYP8DPv7j/npSHQ57jdZ6b2dtdm/Zyw/WHr/efpXzfLWdtlzwewMCn69iWDt+Ww7RZ39YlnV3fzaNht0W"
    "U1wNFFY1Ob0zG1J2sNkK33+YlfzyhTYvLgxoCGPdMuVV6iF7cefSloxlC75nVsoLC1GR5+XEfwnY4gS0Q5q/dby90e9g8VuM"
    "48QjAN83lO88HyNgASDkC0TwT/wDHu8ngD9DDWKrhcXkw+H8/u7rt0cOs2+Euf38YcBv9eb3n/HqdGakfufC7iHr/f3+YroG"
    "xhuITN7x+QAbp4AFRuaFc5i/CP+vtARf0fPK+vTCuFmnCawNdT5SWGim0pCAjeWjmBpuOLyTIGJOVO/U95+9fPHi5Z/7ChTO"
    "LMM5VDjLHXi+zzy/IK6fVjwyEdvHxzd+kmgTykYLC3gAaExYv7K21hvGw/n3nze3C/zqh7OM33+ggSrgf5+qnMC/xcvLKv8Q"
    "9vfTEdRmB3z9Brrw6gD7KzUSqpxU8VpvEE1P776QunQnl7ErTnJYmNEtRacL8GO3L5QySo/lGG7ThLKH2mo3FJGnToNBqdjR"
    "NqVXcRPLLmjq+1Fdx6nm0ebAeBpbKHBvfn61xO3QPmLv0DuS5B0ypp8/f1i2gsEXNRp0V1dxvKzWXb01u/opItkeDvglyv9h"
    "waeNx2ztR0n08I0Q+Aje15+nYNB1V9H6QvAjMnJ6wxXgPI718Rj4JHe4s4OAX8udjuk2EFjMQXAD5oab5LYeq2AtxMDdsgnT"
    "xBRtuGa/gwwfCX+coGnLndayJEXwu6Dn/5Cb/uEAjQgI9N9/PESEFBGwNKeq3ZKcASxw62k1PQNtkLA0xP7NZeA/vheG2A8L"
    "fsEjCLC7km7J3wTcvr2ZHwCjWs2kiO9JRMlPeXdLHcxvUBbfs8Ho8LofRk0gvo/e/2vYrRoMgGqSLaqSyowhsMG2YgNYZ7i0"
    "VOMh0BTyFK9FNXdiZiIbmp0mMHwk/MMcJx8bL0eWtbYwY+2Lw+cRdbhhQv1nwX5FhpbA/wEcH7DvtV13a95qYeuhe4PeT6LC"
    "7y4zMHpNAf6N2R1GSi6sMhwI38+vpycjtYXxIawSq2F1Pk8Pz1sDvYd2MEB/T4Ywy3crBBw1CNgOqktrNMUrdoOaj1VyPUh6"
    "dTqPmC9yD6RYm+QpzHrzecfUG02F1tXLFy+fjlWnkxrwoyQD+36nZSm/oDOi8x8/SxzfpX2xv77MJ6rby6VAxqdyjRbIv+HJ"
    "6RejqP8oGE8BPv3Aq8K+GaCF+4M4/A+RkiXwcenx0vc/z2YHCvtdUE8MKvrJOPUKY2zDXm8yvxTKJ7BJtaHT/fheUkJZ20mx"
    "UVs1jh+bfFfy5zvuTrMHpNRTOmCktqlaInWInKPuqV+W+23S8F8ix/cwE7eoSk+7gx3v5Jq5JaP147vDLF3V4ZthpLgGaK18"
    "wT6Xwy739cRoC7evyVq9TTU+Ob0ii+Y7cosbdkWyyPxSqPDfvk7V+pDBv2MdX5bK3V+oU8HVzuaTddVdZ4cKzcTxfTNHqwEy"
    "ZzCI9qboioCLSufgm9svD0hfVE2VU4eQ8gCqB/3xjRwW376ZCcolmzXNM9fwTio4QsLm7JtsWBwP3EJ3GhxP91UrBgIthork"
    "rc0e2Cg/HnoR/stxd302PznwVkDlY1PbC3n0l6YuJjsr2d50dnr+lfwRP79ZA9NaASzaAfxVAP/bol1rLV35FnqIJiMVd1ur"
    "EQ2aCCQlE2tja9i4YXW11R10f9ub8FXvvn1lW1q8TT8KzfP79y8EPsnFYkBGMa1cPSw4LNlWlKhfLfoic0tsnVrwlQFfx8t6"
    "/CdhTzp+jafA1KQsMG/kKp88aDT0X4/ZyWRP1QaDrUYjl5kpFRkxA1y/20OTNxpPTrBZ0VmR+IY5b0W/trNzUJXywfz66hOm"
    "HpYSQvmXjx/fnWO3JLhaCPyMGwNQN4egAN80yF/J8+GgkezBVU/PLugMN18k7HVngMffDfgaE2yKiXJ/485ZUDqLnl6k9QSw"
    "7WOa34hF0V5FFSmOKm40dRNNW+D4Lw5Vv5NzW0G42yQX708VDJVq1tp0Yyf0q/Q+33twjDbhGgP0paGyzYOCilHTsKQtajcI"
    "mk9vb3ww+Q/HCE508B/rficH470qaJPYjaS1uopJnj4l7FPvDAIfx19yswO4MPYRH42xdbz1nj48pqOi61YtT5JiuJLD9HN3"
    "S5RzxSnfNqCRRlSNmDxJUldks1itd/oobV8g+H+Og36nHvGAokYjzXFEHfDOtNvrALXC0cHQgzO+o1arYlN5qgBvdDroEuKI"
    "PVDhoJvjGATLMrlenMoaNM9owLERW+r/h2NpMCKvaatFc+P5IX2fKR+uypHOakJR1hUw2jscBwPtY2V9kwipt7m5tkYNfagF"
    "Tysx3i+meUSf2k8US1DLbd7motaD3MUjMvMwWVnZsTeFUUC58Y3+xvYxM52XR01N430s+DjRjduCcMxIjhX5L19ZWVldLVoU"
    "YcLauhl7UZjRvgUfTTdgQzTfFIBaXc1WW2vkRP+7Y7CqVK1b/sTOTvkjAFVLwlGrdHbsIcXDqnHMKy15XMpE17Duw6HtWSVh"
    "FBNV2aH+VYHUbBX5+zR9rJTlQlYAVnlIZoN1+TDnyRJsYJqmyoHBlBRgwD2qrXn7LG2R62Asj5NVMtpqmVRXU2OPPFlI5K/a"
    "6gSR9DR5JEET2Q6fU7aiOuGhu9TTxU5uyWSch+li4sQRKacE9ewalfkhtltb6+ur65ubqytuxJFVyQYfcJFazWR7aqcvOqhw"
    "2EyDS661zpwL0S5cI+qCx1xfp9ZCnj0Dt8aICj+mzIUtsi9DM7Lcjvdg704txZ5TqSpSs6wXH/ZGXG/nQPg0lvyvP8dqmbTa"
    "CrfgEKssiphgGznNYsVpWDSTApYOOFnCQzhlLCiXqUktH3zID8PIYyJ0nK81K55iXeoZZWLJspqpK7xyQRcbFK1m5bkQ0twv"
    "LypEWcLU8iKvhRKLyHap4oOFquiJk2SYA5OhuQmWTSPjxoqehG+DBbUmrupSeYSJGZpJ81iaRRPPQ5O6Qx1lQ+WUrfj4ofpy"
    "v77/4i8+jndby4XbTcCveFibRZOaMpwACdD4iA7ojfB9BD/GWq/UjB7KEwdNHJlFTfoYo9oKD/VxRsPli9kDxAtxtgeoWXGp"
    "awTQNk7DJfQluoqjK53Gl7IEsu1IUmqjcXJWV4SmTvYbK++e05EIB5/xP6sNzmng+y9aEqhKaPWakMlTUR6ypHrmJlcTE0Yo"
    "BB/SnDd62TYaqRWZJHm73xfCh+NwGfvBM9GKxAqkhRugCg/NtyVdhnhDoh6RxTV3/rtSuihvCpRU1aawSjU3q66gWRPegcP3"
    "IhyHHlW5bbeMYM5MyZOmeYVE+WbmXuDMbs/LBZYCPavageJ2dJqYd5XzjOn7ZsZ0DBeChV3J4TmpJi10ssGKyBNqsTyOPAxs"
    "uxoqJjBbgvUqj/LzPeWkLkQMDGuT2Uqr3xn/Kdi/2Ffb7ZjoyQuU9LNRpn9eTnSXW/B9zB+lLGVMDdWlQlqespkJ+Obeq9Ws"
    "pB/b1mCgL7GtWMEU4UBEVcK8DK4Rx5IY62EWK40qw5l0bLMFRZaydqz88ii2yOnGAnImjrHBV9UTXuQJ6QMgq8TWGsjVPD+K"
    "AqeJWV7Uhie6VE1lwoQJF0EvOZ1gcORKaMCPoqUlIctaHK+stjbah39Zlq/7BH5Et4S70mONIQG1BrWUzFI+/FepVLOVFRra"
    "VoAfmCFYBvyCdLxqtZrohSQlm9Yr4IcWfPvROC7ARzZDejs+l1chKrC72Kk6c/I96SKRqTllVQIQwkfx+CH5LhD0jMCHg5TV"
    "KIpUyamjFkeD63L/AQM+8yCcfwobGZ1MRtxG0sELNYJq3lhf2yWuQ+btbs7gq4pf2m6c/SrdBbGtFAgD/InpUkD3mK/Jd6tD"
    "nzUbn8RjqVzP+pP4tHaOpRYdCUy6pFIU5FKZIfUwylt1M4MAiYiS+TgP7AHFa6O8sNeFx9eZAj8l2gva9Jij1UiM/1C4Ee1j"
    "t+8lVcOyjz63g8dK9YNxOYbC5mlqq50C04FXCt6LqQUgJnFQ9v6fgv3Lo3q9g+B7qigLJJogHQ/HKGgzvJtEomj4XBhBkzJ4"
    "e3K7uFaxXvz+KvyXmABOyT3YIgNuvdXIkoXS+Bwv0RVeglpIoI2k0/A6Oj9YvXe1xm53rbtu4n6YkWmFS1Wif+tkFORAJiut"
    "lvv2Qup43lrD7nItejq5Vf0fDlPLWzSocv3KFTN1FT4FXKDZ7qhDgZ60/E4bk3oqdm+QWk7ZRN0W50dWOCSvyEGzgZ3t8DFW"
    "Vho82KgIM7Ram5vdbsvBBBR3YNY+plNEps9bnkdoP5NVuYUmUgMHFLUaLfbGUb9CfnC4BuJYIV+9Ryu2Bh/HRKfuOrXHNBo7"
    "vNRabeVcLgBq+yoWuKy24WbW8Kxxix8IlgVJYr0VAyjm7RL4eCZyp+RURQdqXRe3IupgLWyy4XQZeQT8XOoopH5Mc5GElB5j"
    "J7p+u3/sgF9vNqrYxS4w8+7xWg1YfLiFTckXUuj3pK5ae6PeoDfYAVC7Su30zPtVqvyoVrs9esQu03W356nhiDhAhMad6eiB"
    "/w16w8Fab9jr4TzKTt5aXVkhP61R0Pn3FY7xkIaPnX53RlqRN6CHIyI0NdxmXPPGaAifgV2jFI4AbCGf6W3m8PTYXaa11h3k"
    "K+iiaqiVUUvhEDt4e8VXShI7JN4Ej41mb2+42uut4rwhpTbhUbvrmbfZXcGa0jisPoiqBNLO176YcbOj8sByFXa29a5xLQj4"
    "qEYtoUWesBcgx4c54DSuBP4YqPHJdLSBJa+z2d6gdTCb6Ba8Np9NxxqQVqOT2Z5qD9bU9GSvNyAKI+xz+Mz8ZAIPMJmN/Z46"
    "maou/LqX6zGfvTUkl91AkQt1MlBdrQ7YydXt1fem8O9YddPJyRgXA882m4xGO+MTuM7qwYxirgA+0MnBbP5/JwewhSrjEzrz"
    "zqgnj7CSqL0T+m24s9Pbm8zgvvc2R/qAXpx0ccqW3oNTqrWhkr7x071oAifp9hSdbhzTPR5sbg7gER/pjMzt24rscaAcZ1ia"
    "jHxXv3S2lSiaDH672STwE+ODoVzNYXTy7vz07N18XNvZ2UlPvp8d9Eaj1YPL+5ORml7OlB7Nr09Pr2YtoBI1eXc3VaPRQM3P"
    "DjB4BJTf6XaHycH83enp5emBUrMvk3zUOD9VQzW/P2ipCWYAfj8dd3fQwbWjZvfnZ1cXsCyr6uQOEwJnvdZodgnfnqlBe3aB"
    "w5sOTq/pO3ujydmXk0Z0cn9CTWRWYL2np+/Ozq7nY6UO5lenp+9OJ4O9tem7d/AIpxN48ezL2fmX04PhcG92js81G+/p6cX1"
    "6dkVNn6ER5h++QKPsKemp2dfvp9h6+XZu2kLCG1OMByswQv3p+No7/R0rBcS0arc8SUk2ZdLAYWyIS3Z8AR+v7r/0phYLw7/"
    "2Wk2yLYRFzhtFmrzdXYyPphfT/PRINmbX12eDHYA/DPMS5ien4RqcjU/ODiZZMNhQ8/Pr+Y7o70hgo9VCMR2hju9+d3sYDyd"
    "jQl8tVO5mKsd9X/3BzmAPz+YzAC9EXq1RvBg04PJ/B7n8sFDY/YzbAEA8wA2S0fPzicq25tfz+A7s729nQndxskdg99a0+PT"
    "dyf0ntqbw28HJ1en49ybXszw11kXwJ9PJrN3s0Hv5N18Ale6PsEKCnyC8/lQ7+Tr87Or+dZob3XvYHJ6Nj0Yjwj8fG9+Aaeb"
    "vfu/Ed7j/WwA4B88KCmohDp1eq2BFOc0Ta/UyQz0aeD5078K8Ov9ZhNMKXLlg4hAias8Bn8G4J9Nt4Cgp2czWPAegH8OxLQH"
    "4AMjOT+dHozUys5IHVzO5+eTHlP+5nCDRe5QT97NUaXeAp4/+w7PcHCF4M8Z/JmiH3sGfNgee6dnewofenxwAHz2AIh2nCHv"
    "RMpX0+uZCkZjIPze5OziYr43/U7gd3stNX03xyTBQQ+vOdArAzgJfOPsBIl+jv0953jH85XxKawj4b4HP+AexqfzkRrB3p0h"
    "4Yxag7XB/Gz8G+yB2dW0qqZXMwQPHm+gZnenV1MEvxxiSciwCpeKUXwxBVcSW3RouyTrZqd/5IDf7nc6jSQx4LNxTuBfnZ2e"
    "Au0N9rotuK/ZxXR91AD4Zl9m+FB6Z3b+DuipR7c5gZsHGcqUv0nKZgZb+ftMY+BlCODfY475vUv5k+mcKX8wIPAHcKIvyKHo"
    "owDRaPbu4nR+8M/NldnlBDfPVOMgl5P1LcBydj2bfiHwe4MWLNiJ3oMLDddOrmd6b28ARI/gz09O5lcnAYB/Oj2BPYG/wIV2"
    "9uZUK32Kb8/y1p6eX0xwcYf5aG8PHmJvj8BX6uR6iokvsJawJb+cnJ7BvihTPo/VVth1R5rfC/soWkUEMnoYa50Lx85fLw9b"
    "Fvycmx0GBvyL+Qyo4QQoG4j8ZHY33xuuAfhwb/Cq2lneAskFbHRvdHB2NZtdwz0NfoP7Bgh4UN4AwQc2OmqvZUD5mJtN4M/u"
    "x8LzcSzYcCCUD0xazd8Rh8KPjtWw0ZvOToHH/3sTVldV4dmBoeMC9gD88fwCeIcBHyBXe6Ph1nD95Av8Nu4x+BfnZ1/egSAB"
    "nL+jHBlXDwC7tb3eGDYyvHh59uV6hq0mDs7PZzO41OrqcDTCh0DwLxl81cLFna4gaQCvPVtkO2g9SUm7uDxrTl9N08iQTLW4"
    "1SiBXyPwTRGvdICiUtCzKe7Z073WyuzLxdnZ3cVkfQ0YDGzU6+sTf20POENrfjUZtU7uQNhd3Z/kPR/uW9XJRtSNYW98dnag"
    "9R5GBWfXE7UxeCfgNwD8U1Qp1oaDgWU7vcm70wFskssDuol47wCn8t5P/rmD4G9Or+ej9uYUWFcXwF+fnN4B22ljjBDAvQTm"
    "AaDvVIGcDzrD8fwMGdXZbDI/n6oekg9GfAeDvfmXk5Ue7g9kO/PJ/BIWsNea3V+dnb27P8lASRoa8M+nFTzxHsJwdjCEZzjw"
    "Zu/uTw9KRldeiymsZ5qWspEVFOBHGMoVR2zTAR8s3LjT6WDwKnF7CbeGO5WTK9izs8v5DjJMbCvyfTZaAcrfhJu+P9Fqcgqv"
    "np1NNsenFyfTyfQCRdz8EqtMxnoLTKbhaAW45MmUcACBu7M3esdsB4Qpsvte3tvhqRAI/uxkdoZkppCvnZxM1/kKp1cHa6P/"
    "ezeBLTT/PgfecTfTm3BHw5WTL8C1BhSKQTGL781BG5l9QWbybjYCtnc+Q95ysOYBhoNkFSdhwS3DlS5PJyMPeT7AOlnVsB/g"
    "EU5AnGnQ2YDnw70qYLRxDxYLYDj9MkOBe32AxHC2AH6eFuBzRxEaeGt959zPirsfOeCjb6ff6nRSHn5Dbm5yLK/0hrWTMyD3"
    "8/lE6xOUW0DwwCbhqbspvuCr8fz8/PISCGgKDzoaRTMAyJ9foZo4VcPB1jpwlL3Z2eXFJSqAoK+sjXog9pCAUCFEjrRDgfmt"
    "Xg8e7Pv5+dXpdNhbUyfv8By46HC2i/OT0c7ODFM/ewfzy7MLUFYGmwen8+E6ErEatMB2BoMPVIOzc1AQ13vj2eXVFXAYOOn0"
    "9EQPZ1czxPn/RsMdMKi3BtPT66t3p1MQVZPTmW6BMrSHshyshxYwflCY1+anIOhgh55O4uFg/H+XV+9ABwbuS1sSFnO8YOGS"
    "kl9lN0YNU5OXMLDm9P6SdmJKZ51O/9hi//J4rPudNLDhF/bB5q3NTcoemIwpj2CcA3cAS6e1N500UjWaIGMYTWgnbx2c4C7t"
    "wadAO6TUgnFlEx4TrfSdyclsslfBN/bStpqSuj4dYIOYgxUpL+zCJzfoizwvic8xWdEKjazJaLAznEzHIIbW93BHjMEshttY"
    "7zXGJwfRJtrBYCSpvcnJyWSUg5VBvw39wUDvTQ+UHk8no0340duE7dhotVbBtjoZY6MOentvOh2FYE/t8SOA/VyZTEHp6eIt"
    "69bA5xODWqAmYIbBnp2OsrJjjbN0UjMIQkXUwEdx81+noV0UZZ2N7rQA/9m+19/Is6qJtJMPnLI8WhyJ6rKVBhCBxbq5hg0J"
    "uUwJJzlESQ9srHWa5bOmWm6f3QZOBBoOWiu56so53HEzOsg3N8nPCHhsDQabKytgLfGcMDuSJldZq8XlanS0lLeC5Va99Syj"
    "CT8t09Y978lttgY7AzT3u73M3Pj6OmywbJVzHODB0nwN0wjX6Jtr+HY+4EdoqdzJy+GBVb1uurbWwsJhdG9o8o3oB+DTdBTi"
    "O7EZGMp+Wi2ACvfvDP89eWnBB0V/gAHqnP06HP+Bb6221jfX2A2o/FaWtdYBod5Kq9sg/xo5+9aJuIGGcIzSBphWKGSA+6FT"
    "S1cB/W6vtwMPhWPv8hV8GPKC5i3KIG5hcBFNO2wYw8kMg24d698xTaWR+6jBD7D8Ft/sbeEu6aIrqLG8hr/X1zAfNIxYSuX1"
    "Fvp1VijPCD6FN17J2+uYL4juOyzz5M6F2EwIzreZaft2zo8A/K9V8m8CDi1KotihT7F3s7VoYsl0oJQGWJmJfY5Ph935oAhV"
    "o7CzHdk4FhZj7bYAfdNDKwj8kL3zpk6bz5GR87CRldxJ6D5kP1p3k6L/RTeICg2dz8nn1ZWTR0WSXVb0eyEf6vr65uZmz/gy"
    "izocrE7e2tqimmUKFcDiSr6CMcYDD4N7GFegXha4ZVuyqtgWaHV9cx2nYUns3IuI+IHcVwF883ZefgTXfmqs4nQSdP6t6/xR"
    "l7LpZGqjAhVqP8u+HZ8izoF4GpDy891C1/zrxX69vyH4SL5HYGusdeh7HDyjUHa5SQul73TWzZixVXS7Bu7oD2kOQlOpdFQp"
    "yimzbHW1Lq0uKQjWwhKWrTWqCqnYXIwkW20ZzsShEl96lLlBK8+vaGWnD9lsCUrqwpwHujnD8io8/WudA8K2ZKeCt2QywuKi"
    "bsqN1bTIg00w+jbplepJCPyq6eWOOTcog1nV5LC3TNzSurPR7hYm7l9/HW33+00T3/Z97qxqI2gVAh/9yQ3OQ3OeMQOruCVt"
    "ixqrRQS1oiInXogB31UKB/AkIKezuY3prvJBg6eoC4V0QzEtfJ0QfMRmTe52ng6KDrVOv1K8w4zziujTPO2TlyTLbOt6D7v4"
    "cqQRFqwhZUEL+SwceAPGFXlK+ucrvFVsGVOTeqB0yQzEqhHlm3FnJj0WK2bqzc4/beIIHH/udzBdkIknrPjlqGVk4szEhgIb"
    "mEtsa0iLcW7nqkU2/sbUliGfiQ0ry63fTxHOpUuZqLZNxvGLKGPuZJ/ohzX65WSUiM9Dbiu3XzJRDebkVSo8hRabXXJQt5pz"
    "giemeZvUEVuEKAnIOnCHKuEZOAClXD5kRnBRBhp+PXryBF38kY7T9prLd14C6S/HGFwFKMLSVMbYKqkJt1DivCkW37UCHEl1"
    "yEG7dQrHDPg8cUtLzgM/vlTsCfiVheCERBW4D5EkHpuReSXwC1W7AJ92AeaMAI4rK5nB3oTSecAX7sFKGETkZefYsIe3mSYm"
    "ZYVVlMhmLMgYNUy+LxamEpkebE7L2RT2B3Huwv/GVJchTu3BxmGBPebn1+JfiumExQrmnK9E6SMcT6Qu/zwtqZaYRHQzo4/S"
    "0iwBUlVapSKeJkPNblo7vY9rXjHt47mS0ojLJGHAIr6gaCGc+eFcNHcnzFGzJM/DPKDfUbRklhVzGiDXrdj8FiJ6mReEmSWc"
    "QEWsbmlJW/AZbZzolWClVWQbUOGacS8vnqeK/UdwCDpH2X35csJuf4Sh0e5vOPrOixfHu6qznJqmbIFM/s4x0wLvBL+TmH7D"
    "nk0fkOwkZHdB0SHFadisdORzuhyjYBZAMoE4wEnPXcGGYXadnFUiIg6i0mBmSrTwPMl3iHlqYC23HYkVdUXDqSx05G6nriL8"
    "CifAGiInM4HN0IxvMJPH88JAglBKsvazjMHHIX9J0ZHaC8OF6JZyErUpfQJzbJOssdGvH740sawXL54fNtsd7FxbMWPX4WFw"
    "niKChsI8K7gvP3S15oAfRJTLZdLXi5aHlcivBDQ8OFvhWZACPhZby2DLMETi97iAgNkuijc+B5NkqBbBB94YYQ2Q5vgDTZVl"
    "8IW1U0INY1/q/0rnIDpEjheEoVoAPxG+D1+kx4sqTkojV5hkv5NCkJXA95QLvsl4lEqrJAlNOyXMAWwo8u8I9s+fHlbiWP9S"
    "MU2oEpl6m1OCgDBVFrShMzjBKhy+H9qGTcTZpQRUSU0Qgc9QsFqDWiNJOc/zHNO3YjOTy3Np+JorzK4LMCjZlW90ZSH1tkJm"
    "hig6tCKR6DnU1LLUVLXQTDF3M04K2S8ULwqVhyYQZzwroWW9ZDMXTN1VqVW71PnY3EjFTXZ8Lgoi7AH9fVVfbv6elVgo7ayM"
    "Wa8kJlLSk8x+Ep0C7wEZBmY6V+J6Wq8vA3jtTiOLcZ5diP0662iNoYbTht8aW1utFR3Xm+12sw638itmdsBfTVTx61iPscEp"
    "K/CZOrydxnU4dYqtqTpbnU4z1XQtynBZaW9stFGfqTeoC5JO8awNtgbIkQPqK70BJ2u0OxuYKNSAP+nq+BRtKu4A8zEjCzKS"
    "kjWr0pn8fsn+hYUgzw19UNmhlKV5uNw1I7FTL42/nxOQlfoVK1Ne/GXBf3683+wPwOyzXWJtV0NSOgLpMK0N+DG37sT0LMxk"
    "I5+Hr+ta1fOaBw/diNuNWGEngQZI1CYAF9GCASBbed6ppylNBlUgl7y4opsrcOIGvLQUb/S2Vjc6nYbxTakc20mnrby91ani"
    "50P4z6/HPhgxK+028KD2SpZGWE+p4jRRcayiOtolq6sNP1tdqcYpvIGvAm9dAfu3weU7HhifsCJJGMOSdGhjipL8AHwxdsOQ"
    "wbd6ceRSdXm2rip6XgQyZiQyffqBDPSvh2X0+73BxpbbdI4oJo7jMiujlxB9fsOq4f3dfqy3dwPd3G5lene3094edzudtD/e"
    "Hzfb6/C2Up3d9dXeyu5uq9nWu+PxeB9e297V8FdnvL+Lp9kebwy2x9tbG+gFind7Kt5uwKmbjf7u9kYHvzPexna79fo2XKH9"
    "Dzj7Lj5Vf7te3d5dTrf39+EjnTpmlP+7vduhXdLfpha9nUZ7dxd2yDZeFc+0C1rh7v7+Lja7WV1dyaTBlDykyRilp6X/Svg6"
    "zMlYt2Dp1op2qTaYQrmAzoQKckV0uBQU4Yf/nh6CpdvfaLfy3DFXgCVXHimH4qxZz4ukfijFLg77dbV/vK/GR7sVdfjnvjo8"
    "HvY3do+Oj48P+53+0aGCVzZ7vebxcavfh9+Pj4/G8NqR6rfpU/tqmU9wPG71Oy3YRfAdOC18ZACf2PeX9/k79cOjTrx/NOrU"
    "5XtK7R8N4b/teHx8/PT4cLvdWV2peWP4bIzDhA/7KjikM8NN9A/38fNwpv02fh7ubmMDywASk+hZ1Gs8eHZOJvYWe9BzKCWU"
    "5msBjdVRuZOcXROvMQd542antW3Rx+PZ0W6n3+8A+nCWmLg4HMgc8eiYo91sNur8Vr2e/oZT03Te7CNs8Jgvjgf7x7sh/rJ7"
    "eDza/vXoz/3x4dP9tjo63m7Cs29H+8dP93UfaO/4aLfvwetqWx093R8f/TmueofPj7fHT8f5RidPfFggffjy8NdjWJ9d+G4T"
    "vnO8u91uHx73m4dwvubR8T5cBpfweBv+24W9AReDTbJRB0GEC1df7v8L3qzHcEfbR88R/KPDAM50dDze7S/DCfb34eOtVWwV"
    "A1hXmNTrdXhiIEZ89CYeILrpX/wTx9DrygL4WSFuJaTu1IzWGHzjN8kaQF3bh39a9In1LC/323VQB0BWtg/lOKLj8NFjv1b/"
    "NdR5q0PgtytAzocMPhLW8XZ/GzDrNI+P/M7u0/39p7uVfuX48PAY47wK4awT+P2n8Kld+OHh98Z/7CYdkKG4DwCjYzgzcB4A"
    "sqbU0ZFq9/uHT4/gdcy8AzDxHdgc2z6A399O4Tvt/haIdwD/aF81+wx+Hc98dMzgY1b80RE8fx9veAw4I/hJ8g/cEvLA//EY"
    "e/VfscaCp5uh3LVjJ/Oa8BilyXCrGX8K+yVAdJDzqg50vH9cYI8q524FFjcDNS2p7x/+92Nca9YxqXajj2TW/gfsbdjHu78A"
    "GPjL9gDBbyPpAnUfHR8FHbULwD3bVan6x/Fhpd8h8LefHQJtPyXGBBvieDftp7rez7fhdzphn8BPFUDmAz0ePkPw+0rAp0VD"
    "RgbgwxONN/pb7biC4MOeSJfhzW3fPzw6fHp4xOAncI6jIyCN/jbwn6Pderue/QbcAMAfH/4vx7jSjG2dFaniaegMQIykOMUt"
    "JfQ9351CrHSjUx8fvXjJyD9//uz5s6P9ZtrpxKzFwuaDgxs3NHnz5VXP9+TAnPeYhqC3OwR+HR6zs//sTwT/WB0+/3M4AGyJ"
    "7dT6ep8UWnjn6OjpEVDxrwDaYIPA94jtPB2DrDj29p8+3a3167oOqszhi6Nfj14AsyLwY5ACRx6wRkATaHboLSPvwDEjuO+A"
    "JfX7wzaA3+1vrOIjAovf3e1HClcGyGF3f/fIUD6CD0vV3z/E78fYQDoiy+QfsanNoIdv1+3xr3+B1kx/1379BxgSsWM3J0bX"
    "DI2T2tNKu700pYyLwecSgnqzvn14LHT/DIfcPz0a939pgp6Y5S1UmetG4pv/l4vxJBm83UEyi1HmAZq7OTxqC35p1bxtErjN"
    "ZWCUx0eVttc83u/AGg2Vqh8feoMtZCTbRuA2AKcV5N/xcopLr8Yot/8c+/0Ogl+P20eH0aDXPjxa8/aPejqS71UU/9LY6OX7"
    "R+PW5uZqI0sBfFho4knL8OOwo5qHBP5+sAy7B13pnUMSuPFy3Udng1GoRccxGfPwr/g0q9UavkWtZ2t5ERmy2IdGn5QUcRnU"
    "Spoiu59qPBkdm2H92sxToEyhfCB94PxH+9vEkrDwvFlH4o4fO7ArqFFLQZcGXdIHfa7f3R73G6BqgnI3Ru8NqpoaqMX3QbWL"
    "w+a4C88/7vjVfLy7sr6Cn2y3NaqIKo4iUDx1f7wC1nYUwA33x9vp9rgdN9IOqJh5owVqartVB40RLgV4ePC9XRqziSdIsT8f"
    "XH4FjWiwWODS+6iZ4s3Aj1THu9uR+mV3O29u9HfH/Y1+Ez/SV0lcURG7IvJ6ffFB04d/U6UslUBVKGzLqBsPLrslFbn6Fy2w"
    "xAw2C9AM/3Wj3oct/9w5nh7tgyLd6fc2Os16Gi96yrmtjQZTvCZ+X46O46yQCMzTbKWVe2BKpNj6oQ5SUjVT2GhpJU5DMJyw"
    "/7Kq5it5QNXd6InRyyiAm0R6oCsrtBHJm5aRiZWClVVTMW4x3yf3OL6ENFVHw6yNvgEw2kLqRRx72W9gMKWA5Qq76WMPNTe4"
    "AfjhY9Vm0mh0N1prW1udBt4dUjJ7xasLcYHIjoUPIuNQUJqZsvSRckMK7PrJ8/IcKqehuO/rRFZEPIDAzh/CDzrAdhyBFoMe"
    "AO0tarumFt261+D08MEahgO7W61V7MYap3XawQ1YQLL56mSIoDNgtYWBr0aaUf94DO6mDaC4sAL/4kewwIlHOSAZotbopQ3i"
    "b/V6Vcy9JNVhJYEd2qxTuQs8BQ3kJLIUh2aDA7+//suMwyZSwjOtrq2jPZ+DtdlskPs3dgtOxOkUmck2BnyaH1EDhcU4Ny36"
    "aQl8+KWihO69IAx/YX+AL/aWW7Vc7zSBExweu/g/++PocLzb9AJsSdWUDVBEiWKKrFEBXKRs0WMi0cAsMSJdLBOv8I9lv/+e"
    "5eyvQ8eiV9pWlry8IhCMYd4KWfVYQVW1VnVk/OnOHAUc6klhFG3a4sM5eMAF7VYShYjav3FxVjL+lIQ6uJCWIlfoxgmlEQtD"
    "uJTkVRvf9LHMvFp0X9A8DpfBt231GWOvkAWC01Ix8hx9vkg3Xm0X1K5nz63sffYUlfbd/i8emlpAv+jmitndkGAbd3JJNdIi"
    "WkNuezy4S4FHTw9akar4bm/JjD7L5rn0NuC4reeZkhpdrsUMIr+IWGG8jb7CnxFfNsZW6XLO9/maEteh5nF0V5xHEfMtk2+K"
    "7qYUlzeACRMxsVgT20aNPculKQxNYrUxtkzuM3e7LzvgU18IpUw2La0OqZVNxP+pRZ8W4OgQhFafikX9HIwwUBFY7exsYKrL"
    "Bhp8dZRJiD3fK/eZQF0UXbAYsCvlA/DzYCzv96gA30kuQSxqlqwdPmcm9prohHZmkBC84mlfjOpGkt4AUPOGqbDvBm45Krrh"
    "F+D7UmeCZfCZTIhLnCE/OCOU646XuBkm5+wI+CpIXJdyHJpO7ramncO7hbMS5AjogxHYOvuHsADPnhUr8PQPWAFYgvHudtMJ"
    "UKfoc8AuL7CrMcEhX1k1DUElhlEK95XmgHNNeJaZLh5IgewX4REamcuAgZyd5pSZFD9RPAtuIwStw8idiAMKsdN/jl00FVau"
    "Iw7nZUUws6AM6fwtQ6MkKy9erDvUnhMFqLk98VMa3YFkEiYm+8aorbY7ScS8MWSGJZKYJmhVKmEOFK3Q84ErQEvwzLChp09R"
    "a4ZFIL/h7u7u2Dl2d7e5HwvbARjzyE22utJ2mJ0lHXxGrjCXB4/N40eRM74tY8mQGTqKHJ5CyUicR/Ikiira1upmmTsFRz3S"
    "nfGhG1iV+3c9yIqQSEmSWFUeQ61OJo32wiUJFgdLKGlDQy9oGNjOMBF1PmPwE9Z+7AYNNXnL6J3mLqjAh2jGHz/Fg/bCc1wE"
    "WAU52OmDywG6cxX75MS5fjBKTT9oT1kC30yDqZmEBiww0wb8VAulqmJie5GGxylJTwozPoicwTJuOwAX94e3V9T7u1FHO2FM"
    "hwsRWiXDdAObd6Es+MiKUuA1KuGq4FymlPm8+0Cih0uhMi2rJA4ZVCLZUsCAgLETXn2icID3oadn3+yB7e0+ejpAxC/WBxez"
    "v1V58rmNkhfkCTe4hHyWOIeJuWEFbaVAq1LEME3DAMpgYWg4a69qM+WdXvfOvcjEjbxowZH/h16l1DqFjtyAlQj3th2VGHT6"
    "BOmEIaY/hUtFdwg2sozcipy2UTgNXOSvxz2N+F1Q09GDSmpmIk170nadlelyj6I6tqpKTUOS/EGD57w8S9Cd9uI+O5JXGv9t"
    "mX2RHeM7swBkep4ySZO5ASp3M4jyR7oDqoU++EV+7CMDjMxLldD0bykmaePSm53hhOQLWRfqdGFUivBO0FZ9JW0gqaNMIOCj"
    "DIUFoNlxW8uk6JOz6V/wfPWUE+hQ6anTH9R2v+rmAZba+S88kwN+1ek6YeNIqXa3OSrUlNHJfenLs0uovZHMa2Dis+A7uUz5"
    "4hjMvJzYmbt74FHwq6Z9I6U6Wx9ZbvRI49th8LEJDzIYBN+rYMc1TlN2ThxJTyqcjmb69wD4oZRnGT8CNid+hBI5yQkb6jAX"
    "TvJa1enU7/DXx8dhuY8vijJJXs90KxFOS9IqCgoq005n7oc3xnqd0/tMLRK9ehTf4rVSl94CfDlRVXMHGjeDVOwyp/w/SYqc"
    "bDuES1sjgVt/atNfPzAs2S/yEVkt923SrOd2OSmlR0S+g4PTp98Rb3lJpyjPZUdqTVJn4lFVUn9lpmNcK1rWScGBvUTyUHKG"
    "j131AfexGXrKxVzpRxsn5c4KhU6zU1KTc/ssVugFNnsBcw49OUEaG/CVAT8yUsABXxXgi2PazstMpFdeVZuueDL57DG9ZhGZ"
    "vAC/4E+M/hL2C/IYu6p0wfFE6QSpuiSz13gYWKUY0PYgX57Bz4u5ePZDpW8oRwVw+lMvimFVQl/Ar+nFM+bFAC1tqxHzpEQb"
    "OEPVL4g7EXFbCVPeWElgJRpOWbSX94XJyLxwrDXyC6DhjNXSIznsOn+oN9t9T6VMuaSXppgSIfmOLjdgfwYokRyr12TJYCO2"
    "VM4Rxk4HdTxhqkP9uDbpTOF9wPysJqRKRoKp0yj0YvlypeKeO3XPmWKep3IZlxdw2EsgDZy96+a6cZc1omrshpHwVSLyyCnh"
    "R0U7Lnt/6SN8PS/rxSX71n6m6vi7k8TNTw+NNQKaMweH2MOowkVmz3vEHRm5OFXDTrN6oINpvSiVnNk/SpX0srIWWvzqUa/q"
    "UNzEOXs4y5l9LvggWJcovZXYjNtjPJBWAaD3U1625pbamPlLSUNYX5eXh8RGYWhVjKq9+8U7LY9dcBAoGTBK+ZYN0xs1U0dp"
    "gHApJWWl2CVON4tcufxlkefLROEFiywv04Y1LBbAz8vDnNBlQOCjIOWoYqjKl+NcZWYsBU9haPmBsN0UzkknkVDBMY3klE7E"
    "oPfQoZgkJcWYnUSOfi5jW+zmfkjtJRlAamXxLEWyvlQ65SUpn7OowcRs0wM1LkaXuPI9Lxwb5R5pDysq9GMdCrVa6NvudF2z"
    "PNzcYuRhKSgJrEDYX6hcbcmm4jrszHXeZqXqRp9LWdihR/7UALN0jSOaPDfle1E2YpYu3KQqa/oLxAXoc7GYdrgx556nju6t"
    "jSXlhuVqbPx7C5z9cd63UL/y4PYeLM9ix/yyIGCGEdodVSveoY7WXBzhfoErPosJfUUufZq44LPOTz4yZLkCvqvjFZ6sqssC"
    "Q5yD/HBOdV7oE/lD55Zie7UAn0sNHPB1Mc3asKmgdIEHTPoRL5oqzaItWkI9eo7c+YK7t1QZfMsNs1rxVlhRhiWY8zliMjCz"
    "ERbTmak0QqvAtWFseXpgiq3dWIV7gqrVM6qmD4diL5mj5OXOgLvqgxVy6tDUQ2tJQnR/Z7Et+GRKdTDKnbD3N/tClUy/kgek"
    "lBn/ALgky0lVE180pehwS8YCyaAI0xTDLIPCS+FXFvyogRMrIqcSmVPY1qrE+aoPHiBltWTRGejWduvCe+nsBkqFJI7D0cQ8"
    "/xsvMCunjxiqdG+eERrhI3buAt/T+SOmWP6QderHZsOZ3hbslSJrqsI+T3b8cc9ZWYVkYQKRcsE3zqrQeCpKg0grbK4hK3LB"
    "12H6gNmapkthGX7lkLdx6y3qQ5yFG5ZGtwdmingJx7TUQt0h2tTpblzOm3dRd1WXEjfJH1ntQn9y7ODUFkElGYCfkuQqgS89"
    "YcQNx4VjDhv0bWahzuLUJoSHCzy7tClrJSerfEFZLHNr5dBw6lBV8wUPyoIgCx19SJVn6+i8mJ5RngdsXYUu+bMH2LxcfdzU"
    "KoHv8vQF38jfO1fFME6daZ9JUnavJNqd5acEfMef57qmKKRbQ2AfbKzEamxBYEg0c3rgoo5VM7xGSgoi7bpRiQnpRcdq4ayy"
    "873yQsd8MJfT8cE4gsC6q8xXQs2ReK6e8R54ypQrenW5ZEs91Efdou9FKrRGR8j+VYeFoMWbFMBxrcWC6y40gJVwCkvL7KT1"
    "s3228Px5Gnk6tjmLFD0OQke6MxN2wDeUXbVO5KpU0JfAfzzcxJMmkAmm1tx1Blm5h1eEblzwc8f0z0u+Phf8xem1rrfHGPUF"
    "+Oki+A7bKYWf2UXCikNqwK8VrXlC5TqmjPta/427rLbQao9Rx+gSR6MtGwklvmyfjcaBLZEZ7q445wHo1N36Srk8IzON9YU1"
    "KqcCubxVSjrKf3Bs/wdm4/TOzt2N4NJRaJ7dlEfgP4ZY7ZCbvByfpG6QaVLiVyrylIulLgKRjv7J81ASie6EJgRAFjEavDUL"
    "GEOeMpkaHhgST8YXpW2BtnWTBuW02Fn6kZnWudEHqq7DopQm4+KpZOOVvZVOHwHP83TZ/flQEpc1/4UsSlMbZ66QKRvsih96"
    "nbQXGnduYkwq7iBvAhckjRHnEsXYlTLNqRTFZHKumBe6Nx1nKFQfuuDLFzDczJsvxGUo5xCU5AM30cdidfdDlKnhApOa7GCr"
    "SBcuhtz4oosn8bzC0GINyyuTvXLAdzlO/jfgY4BFxkMYORw8Fuyxt+i+Jl7Oiqm3ZibKi+eIquJstaSoPJfJBXQ1X7nmnHXb"
    "5S7z4D1XtYqVCUKYWn1t3iwi756qlN2oKdv2apHNWHeD3WSPBKxU6ElHkYcBr8fDXPoh83I+Zh7amWqmOA6hEyaSUmg71YZy"
    "TN9ZWSjaAiaz1tcLVp6EvMQBWmL+zk8uAc8LnBbEpufaLF5Y2D0hm2xiPuV2eIZhMrkbqfK8ws5JH3Fn0GMV4QHpP2btaDeO"
    "qUtZcbrk63hkEXhPZbUiWCd6YE1WQps5WZrBV6UgTqp1edsmptHGI86/B67souqituA2TXjaQSjlSFz3yPOjHtgsxSmtjWx0"
    "fKuk8werBkTySIQlo1gCXFXrJS7IW5X0h0XwvTL4ZV3YccuWvAqutyMz91FLXJoU2lIm8zAMOeUlc2D1HvSrkaiuWR/fsnUX"
    "et8B30lgpJ+FphUWWlfqGMvlRxGezNNewgIt9N5UFzd2WvKMlbS/IhK8kJkYluzqYmh2KY2B36Ik3bITrbRc9vNp4RTNDSIY"
    "c+DoiCpJAp5aEETUGMYgT3PAQpvrYFTcoBiXTv41+dum6CJGfgDAEpQx9S1MyvZqINmCFV8HxnYKFzy5Jl8hNZInxB+G21fl"
    "LXilmjvSRoKEOqXN4SmtXQuWOjDglVI7kFyLSkW+dQt/KR/N026ikY1sWi5hwbe7A3dYYWPVatwaQWPepnIEAOaNCuUHgRgA"
    "vrLaW+i4pI2GzE1PBXzstmQm1XE5hdkV+JWKtWd14C86IPK/EV1Oo4k0XTSlCm0zf9RfZvfsUvgwI6RKpww5BFBWV4xgfuj7"
    "e+CzzG1RgethLcB3x9qaRyXwEwK/PLs4UnY5JFiu3GdlMbZkjHTzfm4bmbgTWGoFfw9LjGrRP2LHmBrkPe9R/cGQty46aJnt"
    "TJauVw40meb0pdYqYcmTGWrXPFBG3LiLbJamusjFrTB4PP1CKD/UoZsjQAFVJv1a7qSwUTDFDqRMCuNowVi11ufSUpxWzTsS"
    "ryrNSDNoqNAdwkt9gGtCNBLF5Dbwts9DWErESw2rIzdcaIY7FuqH0Trc3jI5L1ZVbsPJxSQLuVAeXGWk5KZku3NJS3lLqtVD"
    "R3O+oBDkupSCQg/jmZkQRPRi3xucbRs4QlWxVzB4xEtSdR1Tyqrij9ncgUR9C8OvWti4idgBVaNTqfLVcGagm4mXun4GtL7y"
    "Rd3PzdwsArFubBJ/MxJDllYiSmnhrFNiCjh0ni7Zey0FyZ38RSenJS8Nm7caimMFFwk87Kxn12XNgk/F6IG27VsWJnLpwqzn"
    "bkCml6KwdxqHbLeTa1wo4VCOoaXDxXwNnTthZjcGUpYP+aLrqDR6R+lHg0+hidyKo4cOYmWe66wTQhNfcxg+ZIHlZKeFtyWV"
    "MS0yXOSuIuchqRecG8OkO1EUrUU/I2XbxZxLKV7GwscTWa2aGEjNJqXlrPxrbCXF5jn2bzN5fJhOg4qWGMRh6MTGXMeMsDam"
    "8NBmnTI7cDNueCPSxx0R6Sk3+hNa343napuIq+cpJxaAOVVVZuPi/UxLc8oNyT/qL8wX8qxSExWzpCtLxuEP6nImMwqZj/yS"
    "kEvZ0R5rZrfkhRuEklzTBcaXiM6f1AzTwdnGopmGhiixBKNGp/Y8yzRq3OLPBo7M1neSdLh+Sf6uajdTT5kM5QJFr8hnkFxC"
    "T8KTNkWFk/jQN2YKc1Q5+VtZE8pTjyKtylroYsqCJI664HtueFi+6ttti3qMDREGJg/Ne8SLyk04xc7NF5Nh8d/M7mva7KGN"
    "XJI5G0RFjtmiHUv7xEntINpODQl74jlO01JQLCzN+3ICW6IWc3Lh4tU8R2p4D4hY6cXUhfyx8PCCSSs8viqCSpRas9moTiaX"
    "vOiFdUu1Mz6T+IP9kzXMGnF6OF/NduRZuBc/cOJfxFhCN8AszT9VEOLjKgBabHnj1BSnL2968XN6fHHSeZBEQhLJhofgiyoU"
    "FyMuTlokYQmxk3FkeH6p6RCZVUsSNyvQT42WthA0d3if6zvMF5QlE/ZhjSq1TN1ks8BvqLlVAlAZE7PjqkL5NoRqW9paThO4"
    "hh1HonL3torgY07DoOKCljDa64skD0xANV2oLQtDSuaz4Dv0yzkvzF5SJ/hLfC0ka98rGLVZfdn5qXHZFODbUxQmmlHcU12E"
    "nv4uZpKX4+kPauIoG8DG5RAUuzfl34oyZdMM1/8H8BFDIYyO/KAAAAAASUVORK5CYII="
)
LOGO_BANNER_B64 = (
    "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"
    "0wAAAABJRU5ErkJggg=="
)


def _logo_image(data):
    """tk.PhotoImage from embedded base64 PNG (None if Tk can't decode PNG)."""
    try:
        return tk.PhotoImage(data=data)
    except Exception:
        return None


def show_splash(root):
    """Modal splash screen -- IRIS / BCDC branding. Auto-dismisses after 3s."""
    sp = tk.Toplevel(root)
    sp.overrideredirect(True)
    LIGHT, INK, DIM = "#fefefe", NAVY, "#5d6b7d"   # matches the logo background
    sp.configure(bg=LIGHT)
    logo = _logo_image(LOGO_SPLASH_B64)
    sp._logo = logo                              # keep a reference (Tk GC)
    W, H = 720, (640 if logo else 520)
    sw, sh = sp.winfo_screenwidth(), sp.winfo_screenheight()
    sp.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    sp.attributes("-topmost", True)

    outer = tk.Frame(sp, bg=GOLD, padx=3, pady=3)
    outer.pack(fill="both", expand=True)
    inner = tk.Frame(outer, bg=LIGHT)
    inner.pack(fill="both", expand=True)

    if logo:
        tk.Label(inner, image=logo, bg=LIGHT, bd=0).pack(pady=(22, 2))
    else:
        tk.Label(inner, text="BCDC", bg=LIGHT, fg=INK,
                 font=("Helvetica", 40, "bold")).pack(pady=(30, 6))
    tk.Frame(inner, bg=GOLD, height=2, width=520).pack(pady=(6, 10))
    tk.Label(inner, text="I R I S", bg=LIGHT, fg=INK,
             font=("Consolas", 64, "bold")).pack(pady=(0, 2))
    tk.Label(inner, text=PRODUCT_L.upper(),
             bg=LIGHT, fg=INK,
             font=("Helvetica", 15, "bold")).pack(pady=(0, 2))
    tk.Label(inner, text=f"—————  {MARK}  —————",
             bg=LIGHT, fg=GOLD,
             font=("Helvetica", 12, "bold")).pack(pady=(0, 6))
    tk.Label(inner, text="RTL  -->  GDSII,  one tool",
             bg=LIGHT, fg="#2b5f8a",
             font=("Helvetica", 13, "italic")).pack(pady=(0, 2))
    tk.Label(inner, text="Verilator | Icarus | Yosys | OpenROAD | KLayout",
             bg=LIGHT, fg=DIM,
             font=("Helvetica", 10)).pack(pady=(0, 10))

    countdown = {"n": 3}
    status = tk.Label(inner, text="Launching in 3 ...", bg=LIGHT, fg="#a8832f",
                       font=("Consolas", 12))
    status.pack(pady=(6, 6))

    def close_splash():
        try: sp.destroy()
        except Exception: pass

    tk.Button(inner, text="  ENTER  ", bg=GOLD, fg=NAVY,
              activebackground="#e0b95c", activeforeground=NAVY,
              font=("Helvetica", 12, "bold"), relief="flat", cursor="hand2",
              command=close_splash, padx=20, pady=6).pack(pady=(0, 10))

    tk.Label(inner, text="by BCDC · Bansal Chip Design Corporation  ·  © Bhavya Bansal",
             bg=LIGHT, fg=DIM,
             font=("Helvetica", 9)).pack(side="bottom", pady=8)

    def tick():
        if not sp.winfo_exists(): return
        countdown["n"] -= 1
        if countdown["n"] <= 0:
            close_splash()
        else:
            status.config(text=f"Launching in {countdown['n']} ...")
            sp.after(1000, tick)
    sp.after(1000, tick)

    sp.bind("<Return>",   lambda e: close_splash())
    sp.bind("<Escape>",   lambda e: close_splash())
    sp.bind("<Button-1>", lambda e: close_splash())

    root.withdraw()
    sp.grab_set()
    sp.wait_window()
    root.deiconify()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="iris", description=f"{PRODUCT} {MARK} -- {PRODUCT_L} (RTL -> GDSII, GUI by default). By BCDC.")
    ap.add_argument("config", nargs="?", help="flow_config.json to load")
    ap.add_argument("--headless", action="store_true", help="run the flow without the GUI")
    ap.add_argument("--generate-only", action="store_true", help="only write the scripts")
    ap.add_argument("--stages", default="",
                    help=f"comma list (default all): {','.join(STAGE_KEYS)}")
    ap.add_argument("--out",  help="script directory (default: config project.output_dir)")
    ap.add_argument("--work", help="work directory (default: config project.work_dir or OUT/work)")
    ap.add_argument("--no-regen", action="store_true", help="headless: reuse existing scripts")
    ap.add_argument("--list-stages", action="store_true")
    ap.add_argument("--no-splash", action="store_true")
    return ap.parse_args(argv)


def main():
    args = parse_args()

    if args.list_stages:
        for k, label in STAGES: print(f"{k:10s} {label}")
        return 0

    if args.headless or args.generate_only:
        cfg = load_config_file(args.config) if args.config else copy.deepcopy(DEFAULTS)
        base = Path(args.config).resolve().parent if args.config else Path.cwd()
        out  = args.out  or cfg["project"]["output_dir"] or str(base / "scripts")
        work = args.work or cfg["project"]["work_dir"]   or f"{out}/work"
        out, work = os.path.abspath(xpath(out)), os.path.abspath(xpath(work))
        if args.generate_only:
            for p in write_scripts(cfg, out): print(f"[gen] {p}")
            print(f"\nRun with:  bash {out}/run_flow.sh   (BCDC_WORK={work})")
            return 0
        stages = [s.strip() for s in args.stages.split(",") if s.strip()] or STAGE_KEYS
        return run_headless(cfg, out, work, stages, regen=not args.no_regen)

    if not HAVE_TK:
        print("tkinter not available (sudo apt install python3-tk). "
              "Use --headless / --generate-only instead.", file=sys.stderr)
        return 1

    root = tk.Tk()
    try:
        from tkinter import font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont",
                     "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=f.cget("size") + 2)
            except Exception:
                pass
    except Exception:
        pass

    try:
        style = ttk.Style()
        for theme in ("clam", "vista", "default"):
            if theme in style.theme_names():
                style.theme_use(theme); break
    except Exception:
        pass

    if not args.no_splash:
        show_splash(root)

    gui = FlowGUI(root)
    if args.config:
        gui.load_config(args.config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
