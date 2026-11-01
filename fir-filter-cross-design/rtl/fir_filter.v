// 8-tap signed FIR filter with registered output and sample history.
module fir_filter #(
    parameter integer DATA_WIDTH  = 8,
    parameter integer COEFF_WIDTH = 8,
    parameter integer TAPS        = 8,
    parameter integer ACC_WIDTH   = 24
) (
    input wire clk,
    input wire rst,
    input wire valid_in,
    input wire signed [DATA_WIDTH-1:0] sample_in,
    output reg valid_out,
    output reg signed [ACC_WIDTH-1:0] sample_out
);

    function automatic signed [COEFF_WIDTH-1:0] coeff;
        input integer idx;
        begin
            case (idx)
                0: coeff = 8'sd3;
                1: coeff = -8'sd2;
                2: coeff = 8'sd5;
                3: coeff = 8'sd1;
                4: coeff = -8'sd4;
                5: coeff = 8'sd6;
                6: coeff = 8'sd2;
                7: coeff = -8'sd1;
                default: coeff = '0;
            endcase
        end
    endfunction

    reg signed [DATA_WIDTH-1:0] delay_line [0:TAPS-1];
    wire signed [DATA_WIDTH + COEFF_WIDTH - 1:0] products [0:TAPS-1];
    wire signed [ACC_WIDTH-1:0] sum_comb;
    wire signed [ACC_WIDTH-1:0] tap_products [0:TAPS-1];
    integer i;
    integer k;

    generate
        for (genvar g = 0; g < TAPS; g = g + 1) begin : GEN_PRODUCTS
            assign products[g] = delay_line[g] * coeff(g);
            assign tap_products[g] = $signed(products[g]);
        end
    endgenerate

    // Explicit flat addition gives Yosys a complete driver set.
    assign sum_comb =
        tap_products[0] +
        tap_products[1] +
        tap_products[2] +
        tap_products[3] +
        tap_products[4] +
        tap_products[5] +
        tap_products[6] +
        tap_products[7];

    always @(posedge clk) begin
        if (rst) begin
            for (i = 0; i < TAPS; i = i + 1)
                delay_line[i] <= '0;

            valid_out  <= 1'b0;
            sample_out <= '0;
        end else begin
            if (valid_in) begin
                for (i = TAPS-1; i > 0; i = i - 1)
                    delay_line[i] <= delay_line[i-1];

                delay_line[0] <= sample_in;
            end

            valid_out <= valid_in;
            sample_out <= valid_in ? sum_comb : sample_out;
        end
    end

endmodule
