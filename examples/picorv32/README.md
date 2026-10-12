# Example: picorv32 on sky130 (no padframe)

| | |
|---|---|
| Design | [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32), default parameters (RV32I) |
| PDK | sky130A, `sky130_fd_sc_hd`, tt_025C_1v80 |
| Clock | `clk`, 20 ns. `resetn` is a **synchronous** reset, so it is timed |
| Die | 600 × 600 µm, about 25 % utilisation; the ~400 ports become die-edge pins (met3/met2) |
| Testbench | `tb_picorv32.v` runs a self-checking counter loop and prints PASS/FAIL |

```bash
git clone https://github.com/YosysHQ/picorv32 ~/designs/picorv32
mkdir -p ~/iris_runs/picorv32 && cp picorv32_sky130.json tb_picorv32.v ~/iris_runs/picorv32/
cd ~/iris_runs/picorv32
# set pdk.pdk_root in the json if your sky130A isn't at ~/.ciel/sky130A
iris run picorv32_sky130.json
```

Reference result (Mark I, OpenROAD 26Q2, Yosys 0.69):

- Synthesis: 6,042 cells, 78,588 µm²
- Routing: 0 DRC violations
- Setup slack: +7.35 ns at 20 ns

The 67 `GRT-0239 … does not have detailed route` warnings are for unused inputs (`irq`, `pcpi_*`) and can be ignored.
