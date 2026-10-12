# Third-party tools

IRIS **does not include or redistribute** any of these tools. `install_iris.sh` builds them on your machine from [nixpkgs](https://github.com/NixOS/nixpkgs), and IRIS runs each one as a separate program. Each tool remains under its own license:

| Tool | Used for | License |
|---|---|---|
| [Yosys](https://github.com/YosysHQ/yosys) | synthesis | ISC |
| [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD) | floorplan, placement, CTS, routing, STA | BSD-3-Clause |
| [KLayout](https://www.klayout.de) | GDS merge, DRC, viewing | GPL-2.0-or-later |
| [Magic](http://opencircuitdesign.com/magic/) | layout / extraction | Historical Permission Notice and Disclaimer (BSD-style) |
| [Netgen](http://opencircuitdesign.com/netgen/) | LVS | GPL-1.0-or-later |
| [Icarus Verilog](https://github.com/steveicarus/iverilog) | RTL simulation | GPL-2.0-or-later |
| [Verilator](https://github.com/verilator/verilator) | lint | LGPL-3.0-only or Artistic-2.0 |
| [GTKWave](https://github.com/gtkwave/gtkwave) | waveforms | GPL-2.0-or-later |
| [Nix](https://github.com/NixOS/nix) | toolchain packaging | LGPL-2.1-or-later |

These licenses are stated as published by each project. Check the upstream project for the authoritative text.

**`iris pack` (offline bundle):** this does copy tool binaries. If you give such a bundle to anyone else, you must also meet the source-code obligations of the GPL/LGPL tools inside it.

**PDKs** (sky130, gf180mcu, …) are not part of this repository. Get them from their publishers (for example with [ciel](https://github.com/fossi-foundation/ciel)), under their own licenses.
