// =============================================================================
//  Self-checking smoke test for picorv32 (for the BCDC "RTL Sim" stage)
//  Based on picorv32's testbench_ez.v (public domain, YosysHQ).
//
//  Program (hand-assembled, no RISC-V GCC needed):
//        li   x1, 1020        ; x1 = &counter
//        sw   x0, 0(x1)       ; counter = 0
//  loop: lw   x2, 0(x1)
//        addi x2, x2, 1
//        sw   x2, 0(x1)       ; counter++
//        j    loop
//
//  PASS if, after 2000 cycles, the core never trapped, the counter advanced,
//  and every store to the counter wrote exactly previous+1.
//  Prints "PASS" or "FAIL ..." (BCDC fails the stage on FAIL|ERROR|MISMATCH).
//  +vcd on the vvp command line dumps tb.vcd for GTKWave.
// =============================================================================
`timescale 1 ns / 1 ps

module tb;
    reg clk = 1;
    reg resetn = 0;
    wire trap;

    always #5 clk = ~clk;

    wire        mem_valid;
    wire        mem_instr;
    reg         mem_ready;
    wire [31:0] mem_addr;
    wire [31:0] mem_wdata;
    wire [3:0]  mem_wstrb;
    reg  [31:0] mem_rdata;

    picorv32 uut (
        .clk       (clk),
        .resetn    (resetn),
        .trap      (trap),
        .mem_valid (mem_valid),
        .mem_instr (mem_instr),
        .mem_ready (mem_ready),
        .mem_addr  (mem_addr),
        .mem_wdata (mem_wdata),
        .mem_wstrb (mem_wstrb),
        .mem_rdata (mem_rdata)
    );

    reg [31:0] memory [0:255];
    integer i;
    initial begin
        for (i = 0; i < 256; i = i + 1) memory[i] = 32'h0;
        memory[0] = 32'h3fc00093; //       li      x1,1020
        memory[1] = 32'h0000a023; //       sw      x0,0(x1)
        memory[2] = 32'h0000a103; // loop: lw      x2,0(x1)
        memory[3] = 32'h00110113; //       addi    x2,x2,1
        memory[4] = 32'h0020a023; //       sw      x2,0(x1)
        memory[5] = 32'hff5ff06f; //       j       <loop>
    end

    // ---- simple 1-wait-state memory ----
    always @(posedge clk) begin
        mem_ready <= 0;
        if (mem_valid && !mem_ready && mem_addr < 1024) begin
            mem_ready <= 1;
            mem_rdata <= memory[mem_addr >> 2];
            if (mem_wstrb[0]) memory[mem_addr >> 2][ 7: 0] <= mem_wdata[ 7: 0];
            if (mem_wstrb[1]) memory[mem_addr >> 2][15: 8] <= mem_wdata[15: 8];
            if (mem_wstrb[2]) memory[mem_addr >> 2][23:16] <= mem_wdata[23:16];
            if (mem_wstrb[3]) memory[mem_addr >> 2][31:24] <= mem_wdata[31:24];
        end
    end

    // ---- checker: every store to the counter must be previous+1 ----
    integer stores = 0, errors = 0;
    reg [31:0] exp_val = 0;
    always @(posedge clk) begin
        if (resetn && mem_valid && !mem_ready && mem_addr == 32'd1020 && mem_wstrb == 4'hf) begin
            if (mem_wdata !== exp_val) begin
                errors = errors + 1;
                $display("MISMATCH store #%0d: wrote %0d, expected %0d", stores, mem_wdata, exp_val);
            end
            exp_val = mem_wdata + 1;
            stores = stores + 1;
        end
    end

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb.vcd");
            $dumpvars(0, tb);
        end
        repeat (100) @(posedge clk);
        resetn <= 1;
        repeat (2000) @(posedge clk);
        if (trap)
            $display("FAIL: core trapped");
        else if (stores < 10 || errors != 0)
            $display("FAIL: %0d counter stores, %0d mismatches", stores, errors);
        else
            $display("PASS: %0d counter stores, counter = %0d, no trap", stores, memory[255]);
        $finish;
    end
endmodule
