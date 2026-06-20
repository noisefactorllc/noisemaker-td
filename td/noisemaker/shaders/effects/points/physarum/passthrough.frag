// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
// Passthrough shader - copy input to output for 2D chain continuity



out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    fragColor = texelFetch(inputTex, coord, 0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
