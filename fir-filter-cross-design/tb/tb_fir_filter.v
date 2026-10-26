`timescale 1ns/1ps
module tb_fir_filter;
    localparam DATA_WIDTH = 8;
    localparam COEFF_WIDTH = 8;
    localparam TAPS = 8;
    localparam ACC_WIDTH = 24;

    reg clk = 0;
    reg rst = 1;
    reg valid_in = 0;
    reg signed [DATA_WIDTH-1:0] sample_in = 0;
    wire valid_out;
    wire signed [ACC_WIDTH-1:0] sample_out;

    fir_filter #(
        .DATA_WIDTH(DATA_WIDTH),
        .COEFF_WIDTH(COEFF_WIDTH),
        .TAPS(TAPS),
        .ACC_WIDTH(ACC_WIDTH)
    ) dut (
        .clk(clk), .rst(rst), .valid_in(valid_in), .sample_in(sample_in),
        .valid_out(valid_out), .sample_out(sample_out)
    );

    always #5 clk = ~clk;

    initial begin
        repeat (2) @(posedge clk);
        @(negedge clk);
        rst = 0;
        valid_in = 1;
        sample_in = 8'sd10;

        // The registered output uses the delay line values before this edge.
        @(posedge clk);
        #1;
        if (sample_out !== 0) begin
            $display("FAIL: first output expected 0, got %0d", sample_out);
            $fatal(1);
        end

        @(negedge clk);
        sample_in = 0;
        @(posedge clk);
        #1;
        if (sample_out !== 30) begin
            $display("FAIL: second output expected 30, got %0d", sample_out);
            $fatal(1);
        end

        @(negedge clk);
        valid_in = 0;
        $display("PASS: basic FIR test completed.");
        $finish;
    end
endmodule
