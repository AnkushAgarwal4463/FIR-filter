
module fir_filter #(
    parameter integer DATA_WIDTH  = 8,
    parameter integer COEFF_WIDTH = 8,
    parameter integer TAPS        = 8,
    parameter integer ACC_WIDTH   = 24
) (
    input  wire clk,
    input  wire rst,
    input  wire valid_in,
    input  wire signed [DATA_WIDTH-1:0] sample_in,
    output reg valid_out,
    output reg signed [ACC_WIDTH-1:0] sample_out
);

    function automatic signed [COEFF_WIDTH-1:0] coeff;
        input integer idx;
        begin
            case (idx)
                0: coeff =  8'sd3;
                1: coeff = -8'sd2;
                2: coeff =  8'sd5;
                3: coeff =  8'sd1;
                4: coeff = -8'sd4;
                5: coeff =  8'sd6;
                6: coeff =  8'sd2;
                7: coeff = -8'sd1;
                default: coeff = '0;
            endcase
        end
    endfunction

    // This implementation is designed for exactly eight taps.
    reg signed [DATA_WIDTH-1:0] delay_line [0:7];

    wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] product [0:7];

    wire signed [ACC_WIDTH-1:0] extended_product [0:7];

    integer i;
    genvar g;

    // Generate eight signed multiplication results.
    generate
        for (g = 0; g < 8; g = g + 1) begin : GEN_PRODUCTS

            assign product[g] =
                delay_line[g] * coeff(g);

            assign extended_product[g] = {
                {(ACC_WIDTH-(DATA_WIDTH+COEFF_WIDTH)){
                    product[g][DATA_WIDTH+COEFF_WIDTH-1]
                }},
                product[g]
            };

        end
    endgenerate

    // Direct accumulator expression.
    wire signed [ACC_WIDTH-1:0] accumulator;

    assign accumulator =
        extended_product[0] +
        extended_product[1] +
        extended_product[2] +
        extended_product[3] +
        extended_product[4] +
        extended_product[5] +
        extended_product[6] +
        extended_product[7];

    // Sequential FIR operation.
    always @(posedge clk) begin
        if (rst) begin

            for (i = 0; i < 8; i = i + 1)
                delay_line[i] <= '0;

            valid_out  <= 1'b0;
            sample_out <= '0;

        end else begin

            valid_out <= valid_in;

            if (valid_in) begin

                for (i = 7; i > 0; i = i - 1)
                    delay_line[i] <= delay_line[i-1];

                delay_line[0] <= sample_in;

                sample_out <= accumulator;

            end
        end
    end

endmodule
