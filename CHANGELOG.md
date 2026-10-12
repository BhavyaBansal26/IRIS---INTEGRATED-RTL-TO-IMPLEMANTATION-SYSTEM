# Changelog

## Mark I — v1.4.0 (2026-10)

The first public release.

- **Installation**
  - One-file installer that builds a pinned Nix toolchain: Yosys, OpenROAD, KLayout, Magic, Netgen, Icarus, Verilator, GTKWave.
  - nixpkgs is pinned to an immutable releases.nixos.org tarball, so installs don't hit GitHub API rate limits.
- **Flow**
  - Stages: lint (Verilator), RTL sim (Icarus, fails on `FAIL|ERROR|MISMATCH`), synthesis, floorplan with tapcells + PDN, placement, CTS + hold repair, routing, post-route reports, GDS (KLayout), DRC (KLayout deck).
  - Flops are mapped with `dfflibmap` (any liberty) or with a manual techmap whose cell and pin names come from the profile.
  - Updated for OpenROAD 26Qx:
    - removed `place_pins -random_seed`, `-wire_unit`, `global_route -guide_file` and the per-command `-bottom/-top_routing_layer` options
    - every stage reloads the liberty after `read_db`
- **GUI**
  - One button per stage, viewers (KLayout, OpenROAD GUI, GTKWave) and Tool Doctor.
  - Editable script preview: saved edits are locked so Generate ALL keeps them.
  - Browse buttons for LEF, lib and GDS. Paths can be relative to the PDK root or absolute.
- **Headless:** `iris run / gen`; `run_flow.sh` runs exactly the same commands as the GUI.
- **Reliability**
  - Every stage runs with `pipefail`, so a crashed tool no longer looks like a pass.
  - After a non-zero exit, a stage continues only if its output file was written during that run.
- **PDKs:** profiles for sky130 and GF180. Private/NDA PDK profiles load from `~/.bcdc/pdk_profiles.local.json`.
