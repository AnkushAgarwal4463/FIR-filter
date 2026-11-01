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

    wire signed [DATA_WIDTH + COEFF_WIDTH - 1:0] p0, p1, p2, p3, p4, p5, p6, p7;
    wire signed [ACC_WIDTH-1:0] s0, s1, s2, s3, s4, s5, s6, s7;
    wire signed [ACC_WIDTH-1:0] sum_comb;

    integer i;

    assign p0 = delay_line[0] * coeff(0);
    assign p1 = delay_line[1] * coeff(1);
    assign p2 = delay_line[2] * coeff(2);
    assign p3 = delay_line[3] * coeff(3);
    assign p4 = delay_line[4] * coeff(4);
    assign p5 = delay_line[5] * coeff(5);
    assign p6 = delay_line[6] * coeff(6);
    assign p7 = delay_line[7] * coeff(7);

    assign s0 = $signed(p0);
    assign s1 = $signed(p1);
    assign s2 = $signed(p2);
    assign s3 = $signed(p3);
    assign s4 = $signed(p4);
    assign s5 = $signed(p5);
    assign s6 = $signed(p6);
    assign s7 = $signed(p7);

    assign sum_comb = s0 + s1 + s2 + s3 + s4 + s5 + s6 + s7;

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
